# Stablecoin Dashboard

A cost-conscious dashboard for tracking stablecoin health, liquidity, peg risk, supply growth, reserve transparency, and market activity.

## Stack

- **Frontend**: Streamlit dashboard (entry `streamlit_app.py`) — reads the database directly through a cached services layer and makes no network calls itself.
- **Data store**: SQLite via SQLAlchemy 2.0 ORM, committed to the repo as a time-series snapshot store (`DATABASE_URL` also accepts Postgres, but SQLite is what ships).
- **Ingestion**: async `httpx` clients funnelled through `core.http.tracked_get` (budget → cache → fetch → log). Live sources: DefiLlama (supply) and exchange public APIs (peg prices), with CoinGecko as a batch price fallback.
- **Optional API**: a FastAPI + Uvicorn server (`app/api/server.py`, installed via the `api` extra) exposes the same data; it is not part of the deployed dashboard.
- **Scheduler**: GitHub Actions — a nightly refresh (`.github/workflows/nightly.yml`, 02:00 UTC) runs the pipelines and commits the updated DB; a keep-awake workflow visits the app every ~9h. The `scripts/` shell scripts run the same pipelines for local/manual use.

## Setup

```bash
cp .env.example .env
pip install -e .
python -m db.models  # initialize schema
```

## Running

```bash
# Start dashboard (deployed entry point)
streamlit run streamlit_app.py

# Start the optional API server
uvicorn app.api.server:app --reload

# Manual data update
bash scripts/daily_update.sh
```

## Data Sources

| Source | Data | Status | Cost |
|--------|------|--------|------|
| DefiLlama | Supply, chain breakdown | Live | Free |
| Binance | Peg price + order-book depth (primary) | Live | Free |
| Coinbase | Peg price (fallback) | Live | Free |
| CoinGecko | Batch peg price (final fallback) | Live | Free (rate limited) |
| Reserve reports | Curated transparency URLs | Live (static list) | Free |
| Etherscan | On-chain reads | Module present, not yet wired | Free tier |
| The Graph | Protocol data | Module present, disabled until post-MVP | Free |

## Project Structure

```
ingestion/   # API clients with caching and rate limits
pipelines/   # Scheduled data update jobs
db/          # schema.sql + SQLAlchemy ORM models and session factory
app/         # FastAPI server + Streamlit dashboard
agents/      # Agent instruction files
tests/       # Per-source and per-metric test suites
scripts/     # Cron-friendly shell scripts
docs/        # Cost estimates, data dictionary, metric definitions
```
