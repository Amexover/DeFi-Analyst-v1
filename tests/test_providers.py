import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from app.providers import HTTPProvider, ProviderError, retry_after_seconds
from app.provenance import fetch_records, metadata


def run(coroutine):
    return asyncio.run(coroutine)


def test_cache_expiry_preserves_fetch_time_and_does_not_leak_mutations():
    async def scenario():
        calls, clock = [], [100.0]
        async def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"nested": {"value": len(calls)}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = HTTPProvider(client, "test", "https://example.test", ttl=30, clock=lambda: clock[0])
            token = fetch_records.set([])
            try:
                first = await provider.get("/data")
                original_time = fetch_records.get()[-1]["retrieved_at"]
                first["nested"]["value"] = 999
                clock[0] += 5
                second = await provider.get("/data")
                assert second["nested"]["value"] == 1
                assert len(calls) == 1
                record = fetch_records.get()[-1]
                assert record["retrieved_at"] == original_time
                assert record["cache_hit"] and record["age_seconds"] == 5
                assert metadata()["retrieved_at"] == original_time
                clock[0] += 30
                await provider.get("/data")
                assert len(calls) == 2
                assert not fetch_records.get()[-1]["cache_hit"]
            finally:
                fetch_records.reset(token)
                await provider.aclose()
    run(scenario())


def test_concurrent_misses_share_one_request():
    async def scenario():
        calls = []
        async def handler(request):
            calls.append(request)
            await asyncio.sleep(0)
            return httpx.Response(200, json={"ok": True})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = HTTPProvider(client, "test", "https://example.test")
            results = await asyncio.gather(*(provider.get("/data") for _ in range(8)))
            assert len(calls) == 1
            assert all(r == {"ok": True} for r in results)
            await provider.aclose()
    run(scenario())


def test_cache_is_bounded_and_separates_query_parameters():
    async def scenario():
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"ok": True})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = HTTPProvider(client, "test", "https://example.test", max_entries=2)
            for kind in ["fees", "revenue", "other", "fees"]:
                await provider.get("/data", {"kind": kind})
            assert len(calls) == 4
            assert len(provider._cache) == 2
            await provider.aclose()
    run(scenario())


@pytest.mark.parametrize("failure", ["timeout", "connection", 429, 500, 502, 503, 504])
def test_transient_failures_have_bounded_retries(failure):
    async def scenario():
        calls, sleeps = [], []
        def handler(request):
            calls.append(request)
            if failure == "timeout":
                raise httpx.ReadTimeout("test")
            if failure == "connection":
                raise httpx.ConnectError("test")
            return httpx.Response(failure, json={})
        async def sleep(seconds):
            sleeps.append(seconds)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = HTTPProvider(client, "test", "https://example.test", sleep=sleep)
            with pytest.raises(ProviderError) as caught:
                await provider.get("/data")
            assert len(calls) == 3
            assert len(sleeps) == 2
            assert caught.value.status == (504 if failure == "timeout" else 503 if failure == 429 else 502)
            assert not provider._cache
            await provider.aclose()
    run(scenario())


def test_retry_after_longer_than_wait_budget_is_propagated_not_shortened():
    async def scenario():
        calls, sleeps = [], []
        def handler(request):
            calls.append(request)
            return httpx.Response(429, headers={"Retry-After": "60"}, json={})
        async def sleep(seconds):
            sleeps.append(seconds)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = HTTPProvider(client, "test", "https://example.test", sleep=sleep)
            with pytest.raises(ProviderError) as caught:
                await provider.get("/data")
            assert len(calls) == 1
            assert caught.value.retry_after == 60
            assert sleeps == []
            await provider.aclose()
    run(scenario())


def test_retry_after_short_delay_then_success():
    async def scenario():
        calls, sleeps = [], []
        def handler(request):
            calls.append(request)
            return (httpx.Response(429, headers={"Retry-After": "1"}, json={})
                    if len(calls) == 1 else httpx.Response(200, json={"ok": True}))
        async def sleep(seconds):
            sleeps.append(seconds)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = HTTPProvider(client, "test", "https://example.test", sleep=sleep)
            assert await provider.get("/data") == {"ok": True}
            assert sleeps == [1]
            await provider.aclose()
    run(scenario())


@pytest.mark.parametrize("status,expected", [(401, 503), (403, 503), (404, 404), (400, 502), (302, 502)])
def test_permanent_errors_and_redirects_are_not_retried(status, expected):
    async def scenario():
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(status, json={})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = HTTPProvider(client, "test", "https://example.test")
            with pytest.raises(ProviderError) as caught:
                await provider.get("/data")
            assert caught.value.status == expected
            assert len(calls) == 1
            await provider.aclose()
    run(scenario())


def test_invalid_json_is_not_cached():
    async def scenario():
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, content=b"not json")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = HTTPProvider(client, "test", "https://example.test")
            for _ in range(2):
                with pytest.raises(ProviderError, match="invalid JSON"):
                    await provider.get("/data")
            assert len(calls) == 2
            assert not provider._cache
            await provider.aclose()
    run(scenario())


def test_request_budget_interrupts_slow_provider():
    async def scenario():
        async def handler(request):
            await asyncio.sleep(1)
            return httpx.Response(200, json={})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = HTTPProvider(client, "test", "https://example.test", total_timeout=.01)
            with pytest.raises(ProviderError) as caught:
                await provider.get("/data")
            assert caught.value.status == 504
            await provider.aclose()
    run(scenario())


def test_retry_after_accepts_http_date_and_rejects_invalid_values():
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60), usegmt=True)
    assert 58 <= retry_after_seconds(future) <= 60
    for value in ["bad", "nan", "inf", None]:
        assert retry_after_seconds(value) is None

@pytest.mark.parametrize("provider_name,path,bad,good", [
    ("llama", "/protocol/aave", {"tvl": {}}, {"tvl": []}),
    ("llama", "/summary/fees/aave", {"totalDataChart": {}}, {"totalDataChart": []}),
    ("llama", "/protocols", {"unexpected": "object"}, []),
    ("gecko", "/coins/aave", {"market_data": []}, {"market_data": {}}),
])
def test_malformed_containers_are_not_cached(provider_name, path, bad, good):
    from app.providers import CoinGecko, DefiLlama
    async def scenario():
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=bad if len(calls) == 1 else good)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = DefiLlama(client) if provider_name == "llama" else CoinGecko(client)
            with pytest.raises(ProviderError):
                await provider.get(path)
            assert not provider._cache
            assert await provider.get(path) == good
            assert len(calls) == 2
            await provider.aclose()
    run(scenario())
