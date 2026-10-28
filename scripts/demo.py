"""Run a clearly labelled offline demonstration of an actual REST response."""
import asyncio
import json

import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.providers import DefiLlama


def main():
    fixture = {
        "slug": "example-lending", "name": "Example Lending", "category": "Lending",
        "chains": ["Ethereum"],
        "tvl": [
            {"date": 1750003600, "totalLiquidityUSD": 1200000},
            {"date": 1750000000, "totalLiquidityUSD": 1000000},
        ],
    }
    def handler(request):
        return httpx.Response(200, json=fixture)
    # ASGI and MockTransport execute entirely in-process; no external HTTP calls.
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        original = app.state.llama
        provider = DefiLlama(mock_client)
        app.state.llama = provider
        try:
            response = client.get("/v1/protocols/example-lending/metrics")
            response.raise_for_status()
            payload = response.json()
        finally:
            client.portal.call(provider.aclose)
            app.state.llama = original
    asyncio.run(mock_client.aclose())
    print(json.dumps({
        "example_only": True,
        "notice": "Synthetic fixture, not current or historical market data",
        "response": payload,
    }, indent=2))


if __name__ == "__main__":
    main()
