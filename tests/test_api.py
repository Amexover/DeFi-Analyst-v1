import json
import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.providers import DefiLlama, CoinGecko


@pytest.fixture(scope='module')
def client():
    with TestClient(app) as client:
        yield client


def test_health_and_validation(client):
    assert client.get('/health').json()['data']['status'] == 'ok'
    assert client.get('/v1/protocols/search?q=a').status_code == 422
    assert client.get('/v1/protocols/aave/tvl?days=0').status_code == 422
    assert client.get('/v1/protocols/bad_slug/metrics').status_code == 422


def test_search_metrics_peers(client, monkeypatch):
    rows = [
        {'slug': 'aave', 'name': 'Aave', 'category': 'Lending', 'tvl': 100, 'gecko_id': 'aave'},
        {'slug': 'peer', 'name': 'Peer', 'category': 'Lending', 'tvl': 50},
        {'slug': 'dex', 'name': 'Dex', 'category': 'Dexes', 'tvl': 200},
    ]
    async def protocols(): return rows
    async def protocol(slug):
        return {'slug': slug, 'name': 'Aave', 'category': 'Lending',
                'tvl': [{'date': 1750000000, 'totalLiquidityUSD': 100}]}
    monkeypatch.setattr(app.state.llama, 'protocols', protocols)
    monkeypatch.setattr(app.state.llama, 'protocol', protocol)
    assert client.get('/v1/protocols/search?q=aave').json()['data']['items'][0]['slug'] == 'aave'
    assert client.get('/v1/protocols/aave/metrics').json()['data']['tvl_usd'] == 100
    assert client.get('/v1/protocols/aave/peers').json()['data']['peers'][0]['slug'] == 'peer'
    assert client.get('/v1/protocols/aave/tvl?days=1').json()['data']['points'] == []


def test_fees_and_token(client, monkeypatch):
    async def fees(slug, kind):
        return {'total24h': 10 if kind == 'dailyFees' else 3, 'totalDataChart': []}
    async def coin(coin_id):
        return {'id': coin_id, 'market_data': {'current_price': {'usd': 2},
                'market_cap': {'usd': 100}, 'fully_diluted_valuation': {'usd': 200}}}
    monkeypatch.setattr(app.state.llama, 'fees', fees)
    monkeypatch.setattr(app.state.gecko, 'coin', coin)
    payload = client.get('/v1/protocols/aave/fees-revenue').json()['data']
    assert payload['fees']['total_24h_usd'] == 10
    assert payload['revenue']['total_24h_usd'] == 3
    token = client.get('/v1/tokens/aave/market').json()['data']
    assert token['market_cap'] == 100
    assert token['fully_diluted_valuation'] == 200
    assert token['total_volume_24h'] is None


def test_mcp_discovery_and_tool_call(client, monkeypatch):
    headers = {'accept': 'application/json, text/event-stream',
               'content-type': 'application/json', 'mcp-protocol-version': '2025-06-18'}
    def rpc(method, params, request_id):
        return client.post('/mcp/', headers=headers, json={
            'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})

    init = rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                              'clientInfo': {'name': 'test', 'version': '1'}}, 1)
    assert init.status_code == 200
    tools = rpc('tools/list', {}, 2).json()['result']['tools']
    assert {tool['name'] for tool in tools} == {
        'search_protocol', 'get_protocol_metrics', 'get_historical_tvl',
        'get_fees_revenue', 'get_token_market_data', 'compare_peers'}

    async def protocols():
        return [{'slug': 'aave', 'name': 'Aave', 'category': 'Lending', 'tvl': 100}]
    monkeypatch.setattr(app.state.llama, 'protocols', protocols)
    result = rpc('tools/call', {'name': 'search_protocol',
                                'arguments': {'query': 'aave'}}, 3).json()['result']
    assert not result['isError']
    assert json.loads(result['content'][0]['text'])['data']['items'][0]['slug'] == 'aave'

def test_blank_search_is_rejected_before_upstream(client, monkeypatch):
    async def protocols():
        raise AssertionError("Blank query must not consume upstream quota")
    monkeypatch.setattr(app.state.llama, "protocols", protocols)
    assert client.get("/v1/protocols/search", params={"q": "  "}).status_code == 422
    assert client.get("/v1/protocols/search", params={"q": " a "}).status_code == 422


def test_history_windows_gaps_and_methodology(client, monkeypatch):
    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = int((today - timedelta(days=1)).timestamp())
    async def protocol(slug):
        return {"tvl": [
            {"date": int(today.timestamp()), "totalLiquidityUSD": 300},
            {"date": yesterday + 3600, "totalLiquidityUSD": 120},
            {"date": yesterday, "totalLiquidityUSD": 100},
        ]}
    async def fees(slug, kind):
        return {"total30d": 999, "totalDataChart": [[yesterday, 10], [yesterday + 3600, 12]],
                "methodology": {"Fees": "Includes reserve income"}}
    monkeypatch.setattr(app.state.llama, "protocol", protocol)
    monkeypatch.setattr(app.state.llama, "fees", fees)
    tvl = client.get("/v1/protocols/aave/tvl?days=2").json()
    fee = client.get("/v1/protocols/aave/fees-revenue?days=2").json()
    assert tvl["data"]["window"] == fee["data"]["fees"]["window"]
    assert len(tvl["data"]["points"]) == 1
    assert tvl["data"]["points"][0]["tvl_usd"] == 120
    assert len(tvl["data"]["coverage"]["missing_days"]) == 1
    assert fee["data"]["fees"]["total_30d_usd"] == 999
    assert fee["data"]["fees"]["history"][0]["value_usd"] == 12
    assert fee["data"]["fees"]["provider_methodology"]["Fees"] == "Includes reserve income"
    assert not fee["data"]["scope"]["summaries_recomputed_from_history"]
    current = client.get("/v1/protocols/aave/tvl?days=2&include_current_day=true").json()["data"]
    assert len(current["points"]) == 2
    assert current["window"]["includes_partial_day"]


def test_metrics_pick_latest_valid_timestamp_not_last_row(client, monkeypatch):
    async def protocol(slug):
        return {"slug": slug, "tvl": [
            {"date": 1750003600, "totalLiquidityUSD": 120},
            {"date": 1750000000, "totalLiquidityUSD": 100},
            {"date": 1750007200, "totalLiquidityUSD": float("nan")},
        ]}
    monkeypatch.setattr(app.state.llama, "protocol", protocol)
    response = client.get("/v1/protocols/aave/metrics")
    assert response.status_code == 200
    assert response.json()["data"]["tvl_usd"] == 120
    assert not response.json()["data"]["scope"]["coverage_verified"]
    assert any("non-finite" in warning for warning in response.json()["meta"]["warnings"])


def test_fees_can_be_partially_unavailable(client, monkeypatch):
    from app.providers import ProviderError
    async def fees(slug, kind):
        if kind == "dailyRevenue":
            raise ProviderError("defillama", "Missing", 404)
        return {"total24h": 10, "totalDataChart": []}
    monkeypatch.setattr(app.state.llama, "fees", fees)
    body = client.get("/v1/protocols/aave/fees-revenue").json()
    assert body["data"]["fees"]["total_24h_usd"] == 10
    assert body["data"]["revenue"] is None
    assert any("revenue unavailable" in w for w in body["meta"]["warnings"])


@pytest.mark.parametrize("status", [404, 502, 503, 504])
def test_provider_errors_have_structured_rest_envelopes(client, monkeypatch, status):
    from app.providers import ProviderError
    async def protocol(slug):
        raise ProviderError("defillama", "Expected provider failure", status,
                            retry_after=60 if status == 503 else None)
    monkeypatch.setattr(app.state.llama, "protocol", protocol)
    response = client.get("/v1/protocols/aave/metrics")
    assert response.status_code == status
    assert response.json()["error"]["provider"] == "defillama"
    assert response.json()["error"]["code"] == "provider_error"
    if status == 503:
        assert response.headers["Retry-After"] == "60"


def test_malformed_nested_payloads_return_provider_errors(client, monkeypatch):
    async def protocol(slug):
        return {"tvl": {"unexpected": "object"}}
    async def coin(coin_id):
        return {"market_data": ["unexpected", "list"]}
    monkeypatch.setattr(app.state.llama, "protocol", protocol)
    monkeypatch.setattr(app.state.gecko, "coin", coin)
    for path in ["/v1/protocols/aave/metrics", "/v1/protocols/aave/tvl",
                 "/v1/tokens/aave/market"]:
        response = client.get(path)
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "provider_error"


def test_non_finite_token_values_are_null_without_losing_valid_fields(client, monkeypatch):
    async def coin(coin_id):
        return {"market_data": {"current_price": {"usd": float("nan")},
                "market_cap": {"usd": 100}, "fully_diluted_valuation": {"usd": float("inf")},
                "total_supply": float("inf"), "circulating_supply": 0}}
    monkeypatch.setattr(app.state.gecko, "coin", coin)
    body = client.get("/v1/tokens/aave/market").json()["data"]
    assert body["price"] is None
    assert body["fully_diluted_valuation"] is None
    assert body["total_supply"] is None
    assert body["market_cap"] == 100
    assert body["circulating_supply"] == 0


def test_cache_provenance_reaches_rest_and_is_request_scoped(client, monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"name": "Aave", "tvl": []})
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = DefiLlama(upstream)
    monkeypatch.setitem(app.state._state, "llama", provider)
    first = client.get("/v1/protocols/aave/metrics").json()
    second = client.get("/v1/protocols/aave/metrics").json()
    assert len(calls) == 1
    assert len(first["meta"]["source_fetches"]) == 1
    assert len(second["meta"]["source_fetches"]) == 1
    assert not first["meta"]["source_fetches"][0]["cache_hit"]
    assert second["meta"]["source_fetches"][0]["cache_hit"]
    assert first["meta"]["retrieved_at"] == second["meta"]["retrieved_at"]
    assert client.get("/health").json()["meta"]["source_fetches"] == []


def test_peers_state_comparison_limits_and_keep_missing_tvl_last(client, monkeypatch):
    async def protocols():
        return [
            {"slug": "aave", "category": "Lending", "tvl": 100},
            {"slug": "unknown", "category": "Lending", "tvl": None},
            {"slug": "known", "category": "Lending", "tvl": 0, "parentProtocol": "parent#123"},
        ]
    monkeypatch.setattr(app.state.llama, "protocols", protocols)
    body = client.get("/v1/protocols/aave/peers").json()
    assert [p["slug"] for p in body["data"]["peers"]] == ["known", "unknown"]
    assert body["data"]["peers"][0]["parent_protocol"] == "parent#123"
    assert not body["data"]["comparability"]["coverage_verified"]
    assert "reserve_yield" in body["data"]["comparability"]["not_adjusted_for"]
    assert body["meta"]["warnings"]


def test_mcp_partial_day_and_structured_errors(client, monkeypatch):
    from datetime import datetime, timezone
    from app.providers import ProviderError
    async def protocol(slug):
        if slug == "missing":
            raise ProviderError("defillama", "Missing protocol", 404)
        return {"tvl": [{"date": datetime.now(timezone.utc).timestamp(), "totalLiquidityUSD": 10}]}
    monkeypatch.setattr(app.state.llama, "protocol", protocol)
    headers = {"accept": "application/json, text/event-stream",
               "content-type": "application/json", "mcp-protocol-version": "2025-06-18"}
    def call(slug):
        response = client.post("/mcp/", headers=headers, json={
            "jsonrpc": "2.0", "id": 50, "method": "tools/call",
            "params": {"name": "get_historical_tvl",
                       "arguments": {"slug": slug, "days": 1, "include_current_day": True}},
        })
        assert response.status_code == 200
        return response.json()["result"]
    result = call("aave")
    assert not result["isError"]
    body = json.loads(result["content"][0]["text"])
    assert body["data"]["window"]["includes_partial_day"]
    assert len(body["data"]["points"]) == 1
    failure = call("missing")
    assert failure["isError"]
    assert '"provider": "defillama"' in failure["content"][0]["text"]
    assert '"http_status": 404' in failure["content"][0]["text"]
