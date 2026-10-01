from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import os
import re
from typing import Any

import httpx
from fastapi import FastAPI, Query, Request, HTTPException
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import JSONResponse

from .providers import CoinGecko, DefiLlama, ProviderError

SLUG = r'^[a-zA-Z0-9][a-zA-Z0-9-]{0,99}$'


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def date_from_epoch(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def envelope(data: Any, sources: list[str], *, warnings: list[str] | None = None) -> dict:
    return {'data': data, 'meta': {'retrieved_at': now(), 'sources': sources, 'warnings': warnings or []}}


def error(status: int, code: str, message: str, provider: str | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={'error': {'code': code, 'message': message, 'provider': provider}, 'meta': {'retrieved_at': now()}})


def validate_slug(value: str, field: str = 'slug') -> str:
    if not re.fullmatch(SLUG, value):
        raise HTTPException(status_code=422, detail=f'{field} must be 1–100 characters: letters, digits, hyphens')
    return value


def protocol_card(p: dict) -> dict:
    return {'slug': p.get('slug'), 'name': p.get('name'), 'category': p.get('category'),
            'chains': p.get('chains') or [], 'tvl_usd': number(p.get('tvl')),
            'symbol': p.get('symbol'), 'gecko_id': p.get('gecko_id')}


def tvl_points(raw: dict, days: int) -> list[dict]:
    points = raw.get('tvl') or []
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    result = []
    for p in points:
        if not isinstance(p, dict) or number(p.get('totalLiquidityUSD')) is None:
            continue
        ts = number(p.get('date'))
        if ts is not None and ts >= cutoff:
            result.append({'date': date_from_epoch(ts), 'tvl_usd': number(p['totalLiquidityUSD'])})
    return result


def fee_summary(raw: dict) -> dict:
    chart = raw.get('totalDataChart') or []
    points = [{'date': date_from_epoch(p[0]), 'value_usd': number(p[1])}
              for p in chart if isinstance(p, list) and len(p) >= 2 and date_from_epoch(p[0]) and number(p[1]) is not None]
    return {'total_24h_usd': number(raw.get('total24h')),
            'total_7d_usd': number(raw.get('total7d')),
            'total_30d_usd': number(raw.get('total30d')),
            'total_all_time_usd': number(raw.get('totalAllTime')),
            'history': points}


@asynccontextmanager
async def lifespan(app: FastAPI):
    timeout = httpx.Timeout(float(os.getenv('UPSTREAM_TIMEOUT_SECONDS', '10')))
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        app.state.llama = DefiLlama(client)
        app.state.gecko = CoinGecko(client)
        yield


app = FastAPI(title='DeFi Analyst v1', version='1.0.0', lifespan=lifespan,
              description='Normalized public DeFi data. Null means unavailable; no investment recommendations.')


@app.exception_handler(ProviderError)
async def provider_error(_: Request, exc: ProviderError):
    return error(exc.status, 'provider_error', exc.message, exc.provider)


@app.exception_handler(StarletteHTTPException)
async def http_error(_: Request, exc: StarletteHTTPException):
    return error(exc.status_code, 'validation_error' if exc.status_code == 422 else 'http_error', str(exc.detail))


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError):
    return error(422, 'validation_error', str(exc))


@app.get('/health')
async def health():
    return envelope({'status': 'ok'}, [])


@app.get('/v1/protocols/search')
async def search_protocol(request: Request, q: str = Query(min_length=2, max_length=100), limit: int = Query(10, ge=1, le=50)):
    rows = await request.app.state.llama.protocols()
    if not isinstance(rows, list):
        raise ProviderError('defillama', 'Unexpected response shape')
    needle = q.casefold().strip()
    matches = [protocol_card(p) for p in rows if isinstance(p, dict) and
               (needle in str(p.get('name') or '').casefold() or needle in str(p.get('slug') or '').casefold())]
    matches.sort(key=lambda p: (p['name'].casefold() != needle if p['name'] else True, -(p['tvl_usd'] or 0)))
    return envelope({'query': q, 'items': matches[:limit], 'total_matches': len(matches)}, ['https://api.llama.fi/protocols'])


@app.get('/v1/protocols/{slug}/metrics')
async def get_protocol_metrics(request: Request, slug: str):
    validate_slug(slug)
    raw = await request.app.state.llama.protocol(slug)
    if not isinstance(raw, dict):
        raise ProviderError('defillama', 'Unexpected response shape')
    latest = (raw.get('tvl') or [])[-1] if raw.get('tvl') else {}
    data = {'slug': raw.get('slug') or slug, 'name': raw.get('name'), 'category': raw.get('category'),
            'chains': raw.get('chains') or [], 'symbol': raw.get('symbol'), 'gecko_id': raw.get('gecko_id'),
            'tvl_usd': number(latest.get('totalLiquidityUSD')) if isinstance(latest, dict) else None,
            'tvl_date': date_from_epoch(latest.get('date')) if isinstance(latest, dict) else None,
            'description': raw.get('description'), 'url': raw.get('url')}
    return envelope(data, [f'https://api.llama.fi/protocol/{slug}'])


@app.get('/v1/protocols/{slug}/tvl')
async def get_historical_tvl(request: Request, slug: str, days: int = Query(90, ge=1, le=3650)):
    validate_slug(slug)
    raw = await request.app.state.llama.protocol(slug)
    if not isinstance(raw, dict):
        raise ProviderError('defillama', 'Unexpected response shape')
    return envelope({'slug': slug, 'unit': 'USD', 'days': days, 'points': tvl_points(raw, days)},
                    [f'https://api.llama.fi/protocol/{slug}'])


@app.get('/v1/protocols/{slug}/fees-revenue')
async def get_fees_revenue(request: Request, slug: str, days: int = Query(90, ge=1, le=3650)):
    validate_slug(slug)
    values, warnings = {}, []
    for label, kind in [('fees', 'dailyFees'), ('revenue', 'dailyRevenue')]:
        try:
            raw = await request.app.state.llama.fees(slug, kind)
            if not isinstance(raw, dict):
                raise ProviderError('defillama', 'Unexpected response shape')
            data = fee_summary(raw)
            cutoff = datetime.now(timezone.utc).date().toordinal() - days
            data['history'] = [p for p in data['history'] if datetime.fromisoformat(p['date']).date().toordinal() >= cutoff]
            values[label] = data
        except ProviderError as exc:
            if exc.status != 404:
                raise
            values[label] = None
            warnings.append(f'{label} unavailable for this protocol')
    return envelope({'slug': slug, 'unit': 'USD', 'days': days, **values},
                    [f'https://api.llama.fi/summary/fees/{slug}?dataType=dailyFees',
                     f'https://api.llama.fi/summary/fees/{slug}?dataType=dailyRevenue'], warnings=warnings)


@app.get('/v1/tokens/{coin_id}/market')
async def get_token_market_data(request: Request, coin_id: str, vs_currency: str = Query('usd', pattern=r'^[a-z]{2,10}$')):
    validate_slug(coin_id, 'coin_id')
    raw = await request.app.state.gecko.coin(coin_id)
    if not isinstance(raw, dict):
        raise ProviderError('coingecko', 'Unexpected response shape')
    market = raw.get('market_data') or {}
    def value(field: str):
        item = market.get(field)
        return number(item.get(vs_currency)) if isinstance(item, dict) else None
    return envelope({'coin_id': raw.get('id') or coin_id, 'name': raw.get('name'), 'symbol': raw.get('symbol'),
                     'currency': vs_currency, 'price': value('current_price'), 'market_cap': value('market_cap'),
                     'fully_diluted_valuation': value('fully_diluted_valuation'),
                     'total_volume_24h': value('total_volume'),
                     'circulating_supply': number(market.get('circulating_supply')),
                     'total_supply': number(market.get('total_supply')),
                     'max_supply': number(market.get('max_supply')),
                     'last_updated': market.get('last_updated') or raw.get('last_updated')},
                    [f'https://api.coingecko.com/api/v3/coins/{coin_id}'])


@app.get('/v1/protocols/{slug}/peers')
async def compare_peers(request: Request, slug: str, limit: int = Query(5, ge=1, le=20)):
    validate_slug(slug)
    rows = await request.app.state.llama.protocols()
    if not isinstance(rows, list):
        raise ProviderError('defillama', 'Unexpected response shape')
    target = next((p for p in rows if isinstance(p, dict) and p.get('slug') == slug), None)
    if target is None:
        return error(404, 'not_found', 'Protocol not found')
    category = target.get('category')
    if not category:
        return envelope({'target': protocol_card(target), 'peers': [], 'selection': 'same_category_by_tvl'},
                        ['https://api.llama.fi/protocols'], warnings=['Category unavailable; peers cannot be selected'])
    peers = [protocol_card(p) for p in rows if isinstance(p, dict) and p.get('slug') != slug and p.get('category') == category]
    peers.sort(key=lambda p: -(p['tvl_usd'] or 0))
    return envelope({'target': protocol_card(target), 'peers': peers[:limit], 'selection': 'same_category_by_tvl'},
                    ['https://api.llama.fi/protocols'])
