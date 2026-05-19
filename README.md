# Stablecoin Dashboard

A cost-conscious dashboard for tracking stablecoin health, liquidity, peg risk, supply growth, reserve transparency, and market activity.

## Stack

- **Backend**: FastAPI + SQLite/PostgreSQL
- **Ingestion**: DefiLlama, exchange public APIs, CoinGecko (fallback)
- **Frontend**: Streamlit dashboard
- **Scheduler**: shell cron scripts

## Setup

```bash
cp .env.example .env
pip install -e .
python -m db.models  # initialize schema
```

## Running

```bash
# Start API server
uvicorn app.api.server:app --reload

# Start dashboard
streamlit run app/dashboard/main.py

# Manual data update
bash scripts/daily_update.sh
```

## Data Sources

| Source | Data | Cost |
|--------|------|------|
| DefiLlama | Supply, TVL, yields, chains | Free |
| Binance/Kraken | Peg prices, order books | Free |
| CoinGecko | Market fallback | Free (rate limited) |
| Etherscan | On-chain reads | Free tier |
| The Graph | Protocol data | Free (post-MVP) |

## Project Structure

```
ingestion/   # API clients with caching and rate limits
pipelines/   # Scheduled data update jobs
db/          # Schema, ORM models, migrations
app/         # FastAPI server + Streamlit dashboard
agents/      # Agent instruction files
tests/       # Per-source and per-metric test suites
scripts/     # Cron-friendly shell scripts
docs/        # Cost estimates, data dictionary, metric definitions
```
