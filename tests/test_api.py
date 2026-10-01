import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.providers import DefiLlama, CoinGecko


@pytest.fixture
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
