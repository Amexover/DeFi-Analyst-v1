# Data methodology

## Scope

The service normalizes public responses from DefiLlama and CoinGecko. It does
not audit their accounting or independently establish economic comparability.
Missing values remain null. A reported zero remains zero.

## Daily history

TVL and fee/revenue histories follow one calendar rule:

- A day is a UTC calendar date.
- The default end date is yesterday; the start is end minus days plus one.
- With include_current_day=true, the end is today, and the window contains
  days dates including today. The window is marked includes_partial_day=true.
- Observations are sorted by date. Future-dated observations are excluded.
- Within a date, the observation with the latest upstream timestamp wins.
  When timestamps tie, the last occurrence in the upstream response wins.
- Duplicate observations are treated as replacement snapshots, never summed.
  The duplicate count is disclosed. This assumption fits the current daily
  adapters and must be revisited before adding event-level or interval data.
- Invalid timestamps and non-finite values are skipped and counted.
- Missing dates are listed in coverage.missing_days and are never filled
  with zero or interpolation. Complete windows are not guaranteed.

The current-metrics endpoint selects the latest valid provider observation
rather than assuming the last input row is the newest. It may include today's
TVL; tvl_is_partial and a warning identify that case. An old tvl_date is not
made current by the timestamp of the HTTP request.

## Totals and accounting

total_24h_usd, total_7d_usd, total_30d_usd and total_all_time_usd are summaries
reported by the provider. They are not recomputed from the filtered history.
Their rolling periods can differ from the explicit completed-day window.

The fees adapter returns provider_methodology when the upstream payload
supplies a string or a string-valued mapping. Absent methodology remains null.
The API does not infer which revenue components are present.

For example, a provider's Hyperliquid Perps series may include reserve yield
as well as trading economics. A label containing “Perps” does not by itself
prove a pure trading-fee definition. Verify the current provider methodology
before comparing or annualizing such values.

Protocol fees, retained revenue, audited net income, executed buybacks and
tokenholder cash distributions are distinct observations.

## Peer selection and overlapping capital

Peers share the target's DefiLlama category and are ordered by provider-reported
TVL. Known zero TVL sorts ahead of unknown TVL. parent_protocol is exposed when
available. A parent and its components can still appear together: the service
does not reconstruct consolidated accounts or exclude overlap automatically.

comparability.coverage_verified=false means product definitions have not been
independently reconciled. It does not mean the underlying provider values
are fabricated or necessarily wrong.

Do not add chain TVL, bridge balances, vault TVL and lending collateral without
checking for repeated assets. Category peers are candidates for further
research, not an investment ranking or proof of comparable business models.

## Fetch time and cache age

Each source_fetches record includes the provider, requested URL, actual
retrieval time, cache_hit, cache age and configured TTL.

- meta.retrieved_at is the oldest contributing fetch time in that response.
- meta.served_at is when this service builds the response envelope.
- Source observation dates such as tvl_date or last_updated remain separate.
- Cache hits retain the original retrieval time. They do not refresh its age.
- A coalesced concurrent request can have cache_hit=false with the same
  retrieval timestamp as another request: both shared one upstream fetch.
- No expired-cache fallback is served after a provider error.
- Health, validation failures and mocked adapters without fetch records use
  the response assembly time for retrieved_at; source_fetches is empty.

## Runtime limits

The cache is an in-memory LRU per process, limited to 128 entries per provider.
The protocol list uses a 300-second TTL; protocol and fee responses use
60 seconds; token market data uses 30 seconds. Restarting or scaling the
process creates independent caches. It is not persistent storage.

Each cache miss can make at most three GET attempts. Only connection errors,
timeouts, HTTP 429 and HTTP 500/502/503/504 are retried. Backoff is bounded
to two seconds per wait and the entire source fetch has a 20-second budget.

Retry-After accepts delta seconds and HTTP dates. If the requested delay
exceeds the wait budget, the service returns an error with Retry-After
instead of retrying early. Authentication errors, 404s, redirects and invalid
JSON are not retried or cached. Cache failures do not become empty success data.

Two fee/revenue sources are fetched sequentially; the REST call can therefore
consume two source budgets. This cache is not an application authentication,
abuse-prevention or per-user quota system.

## Provider references

- [DefiLlama API](https://defillama.com/docs/api)
- [CoinGecko API](https://docs.coingecko.com/reference/introduction)
- [HTTPX transports and MockTransport](https://www.python-httpx.org/advanced/transports/)
