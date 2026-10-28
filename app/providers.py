"""Public API adapters with bounded retries and per-process TTL caching."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import os
import time
from typing import Any

import httpx

from .provenance import now, record_fetch


class ProviderError(Exception):
    def __init__(self, provider, message, status=502, *, retry_after=None):
        self.provider, self.message, self.status = provider, message, status
        self.retry_after = retry_after
        super().__init__(message)


@dataclass
class CacheEntry:
    body: Any
    fetched_at: str
    stored_at: float


def retry_after_seconds(value):
    if not value:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            seconds = (deadline - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


class HTTPProvider:
    def __init__(self, client, name, base_url, *, ttl=60, max_entries=128,
                 retries=2, max_retry_wait=2.0, total_timeout=20.0,
                 sleep=asyncio.sleep, clock=time.monotonic):
        self.client, self.name, self.base_url = client, name, base_url.rstrip("/")
        self.ttl, self.max_entries = max(0, ttl), max(0, max_entries)
        self.retries = max(0, retries)
        self.max_retry_wait, self.total_timeout = max_retry_wait, total_timeout
        self.sleep, self.clock = sleep, clock
        self._cache = OrderedDict()
        self._inflight = {}

    def validate(self, body, path):
        """Subclasses validate provider-specific containers before caching."""

    async def _request(self, path, params, headers):
        for attempt in range(self.retries + 1):
            delay = min(0.25 * 2 ** attempt, self.max_retry_wait)
            try:
                response = await self.client.get(self.base_url + path, params=params, headers=headers)
            except httpx.TimeoutException as exc:
                failure = ProviderError(self.name, "Upstream request timed out", 504)
                failure.__cause__ = exc
            except httpx.RequestError as exc:
                failure = ProviderError(self.name, "Upstream connection failed")
                failure.__cause__ = exc
            else:
                status = response.status_code
                retry_after = retry_after_seconds(response.headers.get("Retry-After"))
                if status == 404:
                    raise ProviderError(self.name, "Resource not found", 404)
                if status in (401, 403):
                    raise ProviderError(self.name, "Upstream access denied", 503)
                if status == 429:
                    failure = ProviderError(self.name, "Upstream rate limit reached", 503,
                                            retry_after=retry_after)
                elif status in (500, 502, 503, 504):
                    failure = ProviderError(self.name, f"Upstream HTTP {status}",
                                            retry_after=retry_after)
                elif status >= 300:
                    raise ProviderError(self.name, f"Unexpected upstream HTTP {status}")
                else:
                    try:
                        body = response.json()
                    except ValueError as exc:
                        raise ProviderError(self.name, "Upstream returned invalid JSON") from exc
                    if not isinstance(body, (dict, list)):
                        raise ProviderError(self.name, "Unexpected upstream response shape")
                    return CacheEntry(body, now(), self.clock())
                if retry_after is not None:
                    # Never retry earlier than requested by the provider.
                    if retry_after > self.max_retry_wait:
                        raise failure
                    delay = max(delay, retry_after)
            if attempt == self.retries:
                raise failure
            await self.sleep(delay)

    async def _load(self, key, path, params, headers, ttl):
        try:
            async with asyncio.timeout(self.total_timeout):
                entry = await self._request(path, params, headers)
                self.validate(entry.body, path)
        except TimeoutError as exc:
            raise ProviderError(self.name, "Upstream request budget exhausted", 504) from exc
        if ttl > 0 and self.max_entries:
            self._cache[key] = entry
            self._cache.move_to_end(key)
            while len(self._cache) > self.max_entries:
                self._cache.popitem(last=False)
        return entry

    async def get(self, path, params=None, headers=None, *, ttl=None):
        ttl = self.ttl if ttl is None else max(0, ttl)
        key = (path, tuple(sorted((k, str(v)) for k, v in (params or {}).items())))
        entry = self._cache.get(key)
        cache_hit = bool(entry and self.clock() - entry.stored_at < ttl)
        if cache_hit:
            self._cache.move_to_end(key)
        else:
            self._cache.pop(key, None)
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(self._load(key, path, params, headers, ttl))
                self._inflight[key] = task
                def finished(done):
                    self._inflight.pop(key, None)
                    if not done.cancelled():
                        done.exception()  # Consume failures if the requesting client disconnected.
                task.add_done_callback(finished)
            entry = await asyncio.shield(task)
        url = str(httpx.URL(self.base_url + path, params=params))
        record_fetch({
            "provider": self.name, "url": url,
            "retrieved_at": entry.fetched_at,
            "cache_hit": cache_hit,
            "age_seconds": round(max(0, self.clock() - entry.stored_at), 3),
            "cache_ttl_seconds": ttl,
        })
        return deepcopy(entry.body)

    async def aclose(self):
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()


class DefiLlama(HTTPProvider):
    def __init__(self, client):
        super().__init__(client, "defillama", "https://api.llama.fi")

    def validate(self, body, path):
        if path == "/protocols":
            if not isinstance(body, list):
                raise ProviderError(self.name, "Unexpected protocol-list response shape")
            return
        if not isinstance(body, dict):
            raise ProviderError(self.name, "Unexpected protocol response shape")
        field = "tvl" if path.startswith("/protocol/") else "totalDataChart"
        if body.get(field) is not None and not isinstance(body[field], list):
            raise ProviderError(self.name, "Unexpected history response shape")

    async def protocols(self):
        return await self.get("/protocols", ttl=300)

    async def protocol(self, slug):
        return await self.get(f"/protocol/{slug}")

    async def fees(self, slug, kind):
        return await self.get(f"/summary/fees/{slug}", {"dataType": kind})


class CoinGecko(HTTPProvider):
    def __init__(self, client):
        super().__init__(client, "coingecko", "https://api.coingecko.com/api/v3", ttl=30)
        key = os.getenv("COINGECKO_DEMO_API_KEY")
        self.headers = {"x-cg-demo-api-key": key} if key else None

    def validate(self, body, path):
        if not isinstance(body, dict):
            raise ProviderError(self.name, "Unexpected token response shape")
        if body.get("market_data") is not None and not isinstance(body["market_data"], dict):
            raise ProviderError(self.name, "Unexpected market_data response shape")

    async def coin(self, coin_id):
        return await self.get(f"/coins/{coin_id}", {
            "localization": "false", "tickers": "false", "market_data": "true",
            "community_data": "false", "developer_data": "false", "sparkline": "false",
        }, self.headers)
