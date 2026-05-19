# Data Dictionary

## stablecoins

| Column | Type | Description |
|---|---|---|
| id | TEXT | DefiLlama asset ID (primary key) |
| symbol | TEXT | Ticker symbol (e.g. USDT) |
| name | TEXT | Full name |
| issuer | TEXT | Issuing entity or peg mechanism label |
| peg_mechanism | TEXT | `fiat-backed`, `crypto-backed`, or `algorithmic` |
| created_at | TIMESTAMP | First seen |
| updated_at | TIMESTAMP | Last refreshed |

## supply_snapshots

| Column | Type | Description |
|---|---|---|
| id | INTEGER | Auto PK |
| symbol | TEXT | FK → stablecoins.symbol |
| circulating_supply | REAL | Total USD value in circulation |
| supply_by_chain | TEXT | JSON: `{chain: usd_amount}` |
| recorded_at | TIMESTAMP | Snapshot time (UTC) |

## price_snapshots

| Column | Type | Description |
|---|---|---|
| id | INTEGER | Auto PK |
| symbol | TEXT | FK → stablecoins.symbol |
| price | REAL | Market price in USD |
| peg_deviation_bps | REAL | `abs(price - 1.0) × 10000` |
| bid_depth_usd | REAL | Total bid-side depth in USD (order book) |
| ask_depth_usd | REAL | Total ask-side depth in USD |
| source | TEXT | Exchange name |
| recorded_at | TIMESTAMP | Snapshot time (UTC) |

## reserve_reports

| Column | Type | Description |
|---|---|---|
| id | INTEGER | Auto PK |
| symbol | TEXT | FK → stablecoins.symbol |
| report_url | TEXT | Link to attestation/audit |
| report_date | DATE | Date of the report |
| composition | TEXT | JSON: `{asset: pct}` |
| auditor | TEXT | Audit firm name |
| ingested_at | TIMESTAMP | When we stored this record |

## risk_scores

| Column | Type | Description |
|---|---|---|
| id | INTEGER | Auto PK |
| symbol | TEXT | FK → stablecoins.symbol |
| peg_score | REAL | 0–100; higher = better peg stability |
| liquidity_score | REAL | 0–100; based on order book depth |
| reserve_score | REAL | 0–100; freshness + auditor weight |
| adoption_score | REAL | 0–100; based on market cap |
| overall_score | REAL | Weighted: peg×0.35 + liq×0.25 + res×0.25 + adopt×0.15 |
| scored_at | TIMESTAMP | When score was computed |

## api_request_log

| Column | Type | Description |
|---|---|---|
| id | INTEGER | Auto PK |
| provider | TEXT | e.g. `defillama`, `coingecko`, `exchanges` |
| url | TEXT | Full URL called |
| requested_at | TIMESTAMP | Call time (UTC) |
