# DeFi Analyst v1 API

A small FastAPI service that normalizes public DefiLlama protocol data and CoinGecko token data. It returns facts only. Missing metrics are `null`; empty history is `[]`. Source URLs and retrieval time are included in every successful response.

## Run locally

Requires Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Set COINGECKO_DEMO_API_KEY in your environment if you have a free Demo key.
uvicorn app.main:app --reload
```

Environment variables are read from the process environment; `.env` is an example file, not auto-loaded. To load it in a shell, use `set -a; source .env; set +a` before starting. Open `http://127.0.0.1:8000/docs` for interactive API docs.

## Endpoints

- `GET /health` — process health, no upstream call.
- `GET /v1/protocols/search?q=aave&limit=10` — name/slug search from DefiLlama.
- `GET /v1/protocols/aave/metrics` — current protocol TVL, date, and metadata.
- `GET /v1/protocols/aave/tvl?days=90` — protocol TVL history (USD).
- `GET /v1/protocols/aave/fees-revenue?days=90` — separate fee and revenue summaries and daily history.
- `GET /v1/tokens/aave/market?vs_currency=usd` — CoinGecko price, market cap, FDV, volume, supply.
- `GET /v1/protocols/aave/peers?limit=5` — protocols in the same DefiLlama category, ordered by TVL.

Protocol slugs and CoinGecko coin IDs are distinct identifiers. Search for a protocol first; `gecko_id` in its metrics is the best token ID to pass to `/v1/tokens/{coin_id}/market`. It may be absent. Peer selection is category based; it is not a valuation judgment. The service does not equate TVL with borrowed assets, fees with revenue, or market cap with FDV.

Successful responses have `{ "data": ..., "meta": { "retrieved_at": ..., "sources": [...], "warnings": [...] } }`. Errors have `{ "error": { "code": ..., "message": ..., "provider": ... }, "meta": ... }`. Upstream timeouts return 504, rate limits 503, missing resources 404, other upstream errors 502. Query validation returns 422. Source timestamps appear when upstream supplies them; `retrieved_at` is the time this API fetched the response, not a claim of freshness at the source.

No database or cache is used. Each call fetches upstream data; CoinGecko can rate limit calls. Add adapters in `app/providers.py` and compose them in `app/main.py` for Dune or protocol specific sources. Never fill missing data by inference.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

## Free Render deploy

1. Push this folder as a Git repository to GitHub. If it is part of a larger repository, set Render's Root Directory to this folder.
2. In Render choose **New → Blueprint**, connect the repository, and use `render.yaml`. Review that the web service plan is **Free** before creating it. Alternatively choose **New → Web Service**, select Python, build command `pip install -r requirements.txt`, start command `uvicorn app.main:app --host 0.0.0.0 --port $PORT`, and Free plan.
3. Set `COINGECKO_DEMO_API_KEY` as a secret if available. It is optional. Open the resulting `https://<service>.onrender.com/health` and `/docs`.

Render Free web services spin down after 15 minutes idle, and the next request may take about a minute. Render provides 750 free instance hours per workspace per month and applies bandwidth/build limits. Free service outbound API traffic may also be limited. Check Render's current terms before deployment.

Sources: [DefiLlama API](https://defillama.com/docs/api), [CoinGecko API](https://docs.coingecko.com/reference/introduction), [Render FastAPI guide](https://render.com/docs/deploy-fastapi), [Render Free limits](https://render.com/docs/free).
