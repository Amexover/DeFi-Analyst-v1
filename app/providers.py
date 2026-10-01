"""External data adapters. Add Dune and protocol adapters behind this boundary."""
from __future__ import annotations

import os
from typing import Any

import httpx


class ProviderError(Exception):
    def __init__(self, provider: str, message: str, status: int = 502):
        self.provider, self.message, self.status = provider, message, status
        super().__init__(message)


class HTTPProvider:
    def __init__(self, client: httpx.AsyncClient, name: str, base_url: str):
        self.client, self.name, self.base_url = client, name, base_url.rstrip('/')

    async def get(self, path: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
        try:
            response = await self.client.get(self.base_url + path, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderError(self.name, 'Upstream request timed out', 504) from exc
        except httpx.RequestError as exc:
            raise ProviderError(self.name, 'Upstream connection failed') from exc
        if response.status_code == 404:
            raise ProviderError(self.name, 'Resource not found', 404)
        if response.status_code == 429:
            raise ProviderError(self.name, 'Upstream rate limit reached', 503)
        if response.status_code in (401, 403):
            raise ProviderError(self.name, 'Upstream access denied', 503)
        if response.status_code >= 400:
            raise ProviderError(self.name, f'Upstream HTTP {response.status_code}')
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(self.name, 'Upstream returned invalid JSON') from exc


class DefiLlama(HTTPProvider):
    def __init__(self, client: httpx.AsyncClient):
        super().__init__(client, 'defillama', 'https://api.llama.fi')

    async def protocols(self):
        return await self.get('/protocols')

    async def protocol(self, slug: str):
        return await self.get(f'/protocol/{slug}')

    async def fees(self, slug: str, kind: str):
        return await self.get(f'/summary/fees/{slug}', {'dataType': kind})


class CoinGecko(HTTPProvider):
    def __init__(self, client: httpx.AsyncClient):
        super().__init__(client, 'coingecko', 'https://api.coingecko.com/api/v3')
        key = os.getenv('COINGECKO_DEMO_API_KEY')
        self.headers = {'x-cg-demo-api-key': key} if key else None

    async def coin(self, coin_id: str):
        return await self.get(f'/coins/{coin_id}', {
            'localization': 'false', 'tickers': 'false',
            'market_data': 'true', 'community_data': 'false', 'developer_data': 'false',
            'sparkline': 'false',
        }, self.headers)
