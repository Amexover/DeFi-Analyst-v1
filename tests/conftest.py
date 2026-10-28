"""All tests must run without contacting a live upstream API."""
import httpx
import pytest


@pytest.fixture(autouse=True)
def no_live_upstreams(monkeypatch):
    async def blocked(*args, **kwargs):
        raise AssertionError("Live network access is disabled in tests; use MockTransport")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)
