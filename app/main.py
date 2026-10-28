from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import math
import os
import re

import httpx
from fastapi import FastAPI, Query, Request, HTTPException
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import JSONResponse

from .mcp_server import mcp
from .normalization import number, date_from_epoch, normalize_daily
from .provenance import fetch_records, metadata
from .providers import CoinGecko, DefiLlama, ProviderError

SLUG = r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,99}$"


def text(value):
    return value if isinstance(value, str) else None


def envelope(data, sources, *, warnings=None):
    return {"data": data, "meta": metadata(sources, warnings)}


def error(status, code, message, provider=None, retry_after=None):
    headers = {"Retry-After": str(math.ceil(retry_after))} if retry_after is not None else None
    return JSONResponse(status_code=status, headers=headers, content={
        "error": {"code": code, "message": message, "provider": provider},
        "meta": metadata(),
    })


def validate_slug(value, field="slug"):
    if not re.fullmatch(SLUG, value):
        raise HTTPException(status_code=422, detail=f"{field} must be 1–100 characters: letters, digits, hyphens")
    return value


def protocol_card(p):
    chains = p.get("chains")
    return {
        "slug": text(p.get("slug")), "name": text(p.get("name")),
        "category": text(p.get("category")),
        "chains": [c for c in chains if isinstance(c, str)] if isinstance(chains, list) else [],
        "tvl_usd": number(p.get("tvl")), "symbol": text(p.get("symbol")),
        "gecko_id": text(p.get("gecko_id")),
        "parent_protocol": text(p.get("parentProtocol")),
        "tvl_measure": "provider_reported_tvl",
    }


def normalize(raw, kind, days=None, include_current_day=False):
    rows = raw.get("tvl" if kind == "tvl" else "totalDataChart")
    try:
        return normalize_daily(rows, kind, days, include_current_day)
    except ValueError as exc:
        raise ProviderError("defillama", "Unexpected history response shape") from exc


def tvl_points(raw, days, include_current_day=False):
    return normalize(raw, "tvl", days, include_current_day)[0]


def provider_methodology(raw):
    value = raw.get("methodology")
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return {key: value for key, value in value.items() if isinstance(value, str)}
    return None


def fee_summary(raw, days=90, include_current_day=False):
    points, window, coverage, warnings = normalize(raw, "fees", days, include_current_day)
    data = {
        "total_24h_usd": number(raw.get("total24h")),
        "total_7d_usd": number(raw.get("total7d")),
        "total_30d_usd": number(raw.get("total30d")),
        "total_all_time_usd": number(raw.get("totalAllTime")),
        "history": points, "window": window, "coverage": coverage,
        "provider_methodology": provider_methodology(raw),
        "summary_window": "upstream_reported; may differ from the completed-day history window",
    }
    return data, warnings


@asynccontextmanager
async def lifespan(app):
    timeout = httpx.Timeout(float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "10")))
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        app.state.llama = DefiLlama(client)
        app.state.gecko = CoinGecko(client)
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await app.state.llama.aclose()
            await app.state.gecko.aclose()


app = FastAPI(
    title="DeFi Analyst v1", version="1.1.0", lifespan=lifespan,
    description="Public DeFi data with UTC windows, source provenance and explicit comparison limits.",
)
app.mount("/mcp", mcp.streamable_http_app())


@app.middleware("http")
async def request_provenance(request, call_next):
    token = fetch_records.set([])
    try:
        return await call_next(request)
    finally:
        fetch_records.reset(token)


@app.exception_handler(ProviderError)
async def provider_error(_, exc):
    return error(exc.status, "provider_error", exc.message, exc.provider, exc.retry_after)


@app.exception_handler(StarletteHTTPException)
async def http_error(_, exc):
    return error(exc.status_code, "validation_error" if exc.status_code == 422 else "http_error", str(exc.detail))


@app.exception_handler(RequestValidationError)
async def validation_error(_, exc):
    return error(422, "validation_error", str(exc))


@app.get("/health")
async def health():
    return envelope({"status": "ok", "version": app.version}, [])


@app.get("/v1/protocols/search")
async def search_protocol(request: Request, q: str = Query(min_length=2, max_length=100),
                          limit: int = Query(10, ge=1, le=50)):
    query = q.strip()
    if len(query) < 2:
        raise HTTPException(status_code=422, detail="Search must contain at least two non-padding characters")
    rows = await request.app.state.llama.protocols()
    if not isinstance(rows, list):
        raise ProviderError("defillama", "Unexpected response shape")
    needle = query.casefold()
    matches = [protocol_card(p) for p in rows if isinstance(p, dict) and
               (needle in (text(p.get("name")) or "").casefold() or
                needle in (text(p.get("slug")) or "").casefold())]
    matches.sort(key=lambda p: ((p["name"] or "").casefold() != needle,
                               p["tvl_usd"] is None, -(p["tvl_usd"] or 0)))
    return envelope({"query": query, "items": matches[:limit], "total_matches": len(matches)},
                    ["https://api.llama.fi/protocols"])


@app.get("/v1/protocols/{slug}/metrics")
async def get_protocol_metrics(request: Request, slug: str):
    validate_slug(slug)
    raw = await request.app.state.llama.protocol(slug)
    if not isinstance(raw, dict):
        raise ProviderError("defillama", "Unexpected response shape")
    points, _, _, warnings = normalize(raw, "tvl")
    latest = points[-1] if points else {}
    card = protocol_card(raw)
    data = {
        **card, "slug": text(raw.get("slug")) or slug,
        "tvl_usd": latest.get("tvl_usd"), "tvl_date": latest.get("date"),
        "description": text(raw.get("description")), "url": text(raw.get("url")),
        "scope": {
            "entity_slug": slug,
            "provider_methodology": provider_methodology(raw),
            "coverage_verified": False,
            "note": "Provider-defined TVL; do not add it to overlapping vault, bridge or chain balances",
        },
    }
    data["tvl_is_partial"] = latest.get("date") == datetime.now(timezone.utc).date().isoformat() if latest else None
    if data["tvl_is_partial"]:
        warnings.append("Latest TVL is an observation from the incomplete current UTC day")
    return envelope(data, [f"https://api.llama.fi/protocol/{slug}"], warnings=warnings)


@app.get("/v1/protocols/{slug}/tvl")
async def get_historical_tvl(request: Request, slug: str,
                             days: int = Query(90, ge=1, le=3650),
                             include_current_day: bool = False):
    validate_slug(slug)
    raw = await request.app.state.llama.protocol(slug)
    if not isinstance(raw, dict):
        raise ProviderError("defillama", "Unexpected response shape")
    points, window, coverage, warnings = normalize(raw, "tvl", days, include_current_day)
    return envelope({
        "slug": slug, "unit": "USD", "days": days, "points": points,
        "window": window, "coverage": coverage,
        "tvl_measure": "provider_reported_tvl",
    }, [f"https://api.llama.fi/protocol/{slug}"], warnings=warnings)


@app.get("/v1/protocols/{slug}/fees-revenue")
async def get_fees_revenue(request: Request, slug: str,
                           days: int = Query(90, ge=1, le=3650),
                           include_current_day: bool = False):
    validate_slug(slug)
    values, warnings = {}, []
    for label, kind in [("fees", "dailyFees"), ("revenue", "dailyRevenue")]:
        try:
            raw = await request.app.state.llama.fees(slug, kind)
            if not isinstance(raw, dict):
                raise ProviderError("defillama", "Unexpected response shape")
            data, notes = fee_summary(raw, days, include_current_day)
            values[label] = data
            warnings.extend(f"{label}: {note}" for note in notes)
        except ProviderError as exc:
            if exc.status != 404:
                raise
            values[label] = None
            warnings.append(f"{label} unavailable for this protocol")
    warnings.append("Provider-defined fee/revenue scope; reserve yield and pass-through fees may be included. Inspect methodology before comparing.")
    return envelope({
        "slug": slug, "unit": "USD", "days": days, **values,
        "scope": {"entity_slug": slug, "coverage_verified": False,
                  "summaries_recomputed_from_history": False},
    }, [f"https://api.llama.fi/summary/fees/{slug}?dataType=dailyFees",
        f"https://api.llama.fi/summary/fees/{slug}?dataType=dailyRevenue"], warnings=warnings)


@app.get("/v1/tokens/{coin_id}/market")
async def get_token_market_data(request: Request, coin_id: str,
                                vs_currency: str = Query("usd", pattern=r"^[a-z]{2,10}$")):
    validate_slug(coin_id, "coin_id")
    raw = await request.app.state.gecko.coin(coin_id)
    if not isinstance(raw, dict):
        raise ProviderError("coingecko", "Unexpected response shape")
    market = raw.get("market_data")
    if market is None:
        market = {}
    if not isinstance(market, dict):
        raise ProviderError("coingecko", "Unexpected market_data response shape")
    def value(field):
        item = market.get(field)
        return number(item.get(vs_currency)) if isinstance(item, dict) else None
    data = {
        "coin_id": text(raw.get("id")) or coin_id,
        "name": text(raw.get("name")), "symbol": text(raw.get("symbol")),
        "currency": vs_currency, "price": value("current_price"),
        "market_cap": value("market_cap"),
        "fully_diluted_valuation": value("fully_diluted_valuation"),
        "total_volume_24h": value("total_volume"),
        "circulating_supply": number(market.get("circulating_supply")),
        "total_supply": number(market.get("total_supply")),
        "max_supply": number(market.get("max_supply")),
        "last_updated": text(market.get("last_updated")) or text(raw.get("last_updated")),
    }
    warnings = []
    if all(data[key] is None for key in ["price", "market_cap", "fully_diluted_valuation", "total_volume_24h"]):
        warnings.append("Market values unavailable for the requested currency; no fallback currency applied")
    return envelope(data, [f"https://api.coingecko.com/api/v3/coins/{coin_id}"], warnings=warnings)


@app.get("/v1/protocols/{slug}/peers")
async def compare_peers(request: Request, slug: str, limit: int = Query(5, ge=1, le=20)):
    validate_slug(slug)
    rows = await request.app.state.llama.protocols()
    if not isinstance(rows, list):
        raise ProviderError("defillama", "Unexpected response shape")
    target = next((p for p in rows if isinstance(p, dict) and p.get("slug") == slug), None)
    if target is None:
        return error(404, "not_found", "Protocol not found")
    category = text(target.get("category"))
    warnings = [
        "Same-category peers are research candidates, not a valuation or investment ranking",
        "TVL definitions, parent/child overlap and product coverage are not reconciled; do not sum these balances",
    ]
    if not category:
        peers = []
        warnings.append("Category unavailable; peers cannot be selected")
    else:
        peers = [protocol_card(p) for p in rows if isinstance(p, dict) and
                 p.get("slug") != slug and p.get("category") == category]
        peers.sort(key=lambda p: (p["tvl_usd"] is None, -(p["tvl_usd"] or 0)))
    return envelope({
        "target": protocol_card(target), "peers": peers[:limit],
        "selection": "same_category_by_tvl",
        "comparability": {
            "criteria": ["same_provider_category", "provider_reported_tvl_descending"],
            "coverage_verified": False,
            "not_adjusted_for": ["parent_child_overlap", "borrowed_assets", "bridge_or_vault_overlap",
                                 "reserve_yield", "fee_distributions", "token_value_accrual"],
        },
    }, ["https://api.llama.fi/protocols"], warnings=warnings)
