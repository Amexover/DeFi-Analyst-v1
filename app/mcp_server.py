"""MCP tools backed by the same validated REST routes as the public API."""

import re

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings


mcp = FastMCP(
    'DeFi Analyst v1',
    instructions=(
        'Use these tools for current public DeFi data. Treat null as unavailable. '
        'Always mention source dates, distinguish TVL, fees, revenue, market cap and FDV, '
        'and separate protocol fundamentals from token investment analysis.'
    ),
    streamable_http_path='/',
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=['defi-analyst-v1.onrender.com', '127.0.0.1:*', 'localhost:*', 'testserver'],
    ),
)


async def _get(path: str, params: dict | None = None) -> dict:
    # Import lazily so main can mount this MCP server without a circular import.
    from .main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
        response = await client.get(path, params=params)
    body = response.json()
    if response.is_error:
        detail = body.get('error', {})
        raise ValueError(f"{detail.get('code', 'api_error')}: {detail.get('message', response.reason_phrase)}")
    return body


def _slug(value: str) -> str:
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9-]{0,99}', value):
        raise ValueError('Identifier must contain only letters, digits, and hyphens (1–100 characters)')
    return value


@mcp.tool()
async def search_protocol(query: str, limit: int = 10) -> dict:
    """Find DefiLlama protocol slugs and basic facts by name. Search before using a slug."""
    return await _get('/v1/protocols/search', {'q': query, 'limit': limit})


@mcp.tool()
async def get_protocol_metrics(slug: str) -> dict:
    """Get a protocol's current TVL, category, chains, token identifier and source date."""
    return await _get(f'/v1/protocols/{_slug(slug)}/metrics')


@mcp.tool()
async def get_historical_tvl(slug: str, days: int = 90) -> dict:
    """Get daily historical protocol TVL in USD for up to 3650 days."""
    return await _get(f'/v1/protocols/{_slug(slug)}/tvl', {'days': days})


@mcp.tool()
async def get_fees_revenue(slug: str, days: int = 90) -> dict:
    """Get protocol fees and revenue separately, with history where available."""
    return await _get(f'/v1/protocols/{_slug(slug)}/fees-revenue', {'days': days})


@mcp.tool()
async def get_token_market_data(coin_id: str, vs_currency: str = 'usd') -> dict:
    """Get CoinGecko price, market cap, FDV, volume and supply for a token ID."""
    return await _get(f'/v1/tokens/{_slug(coin_id)}/market', {'vs_currency': vs_currency})


@mcp.tool()
async def compare_peers(slug: str, limit: int = 5) -> dict:
    """Find protocols in the same DefiLlama category, ranked by current TVL."""
    return await _get(f'/v1/protocols/{_slug(slug)}/peers', {'limit': limit})
