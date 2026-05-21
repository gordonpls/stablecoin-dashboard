# Stablecoin Dashboard Feature Backlog for AI Agent

## Purpose

This document defines the next useful product features for a hosted stablecoin and crypto risk dashboard. It is written for an AI coding agent to ingest and implement incrementally.

The current project already includes:

- Streamlit dashboard with 5 tabs
- FastAPI backend with core endpoints
- SQLite database via SQLAlchemy 2.0
- Cached and logged provider API responses
- Ingestion pipelines for supply, prices, liquidity, reserves, and risk scoring
- Provider-level request logging in `api_request_log`
- Auto-refresh and manual refresh workflows
- Tests across database models, pipelines, API, scoring, price ingestion, DefiLlama ingestion, tracked API requests, and cost limits

The goal of this backlog is to make the dashboard more useful to end users, more explainable, and more production-ready for website hosting.

---

## Current System Summary

### Data Collection Pipelines

| Pipeline | Source | Cadence |
|---|---|---:|
| `update_supply` | DefiLlama | Hourly |
| `update_prices` | Binance / Coinbase fallback | Every 10 minutes |
| `update_liquidity` | Binance order book depth | Every 10 minutes |
| `update_reserves` | Static hardcoded reserve data for USDT, USDC, DAI | Hourly |
| `score_stablecoins` | Computed from database | Every 10 minutes |

All API responses are cached and logged per provider to `api_request_log`.

### Metrics Tracked Per Stablecoin

- Circulating supply
- Supply by chain as JSON
- 7-day supply change percentage
- 30-day supply change percentage
- Price
- Peg deviation in basis points
- Bid and ask order book depth in USD
- Reserve report date
- Reserve auditor or attestor
- Reserve composition as JSON
- Risk scores from 0 to 100:
  - peg
  - liquidity
  - reserve
  - adoption
  - overall

### Current Dashboard UI

The dashboard currently has 5 tabs:

1. Overview
2. Supply
3. Peg Deviation
4. Risk Scores
5. API Usage

### Current Backend API

Existing FastAPI endpoints:

- `GET /health`
- `GET /stablecoins?limit=...`
- `GET /stablecoins/{symbol}`
- `GET /stablecoins/{symbol}/scores`
- `GET /stablecoins/{symbol}/prices?limit=...`
- `GET /providers/usage`

---

# Implementation Priorities

## Priority 1: Highest User Value

These features should be implemented first because they make the dashboard more actionable and useful for day-to-day monitoring.

---

## 1. Add a Market Changes Summary  (DONE 2026-05-21)

Implemented in `services/market_changes.py` (`compute_market_changes`), exposed
via `GET /stablecoins/changes`, and surfaced as a top-level "Market Changes"
section in the dashboard. Compares latest vs prior snapshot for supply (7d) and
peg / liquidity / risk score (24h); ranks by severity; handles missing prior
snapshots gracefully. Tests in `tests/test_market_changes.py`.

### Objective

Add a top-level summary that explains what changed since the last refresh or prior snapshot.

### User Value

Users should not need to inspect every chart to understand what moved. The dashboard should surface the biggest changes automatically.

### Functional Requirements

- Add a top-level `Market Changes` card or section.
- Compare the latest snapshot to a prior snapshot.
- Generate short plain-language summaries.
- Add a table of biggest movers.
- Add a FastAPI endpoint:
  - `GET /stablecoins/changes`

### Example Summaries

- `USDT supply increased 1.2% over 7 days.`
- `DAI peg deviation moved from 8 bps to 37 bps.`
- `USDC liquidity depth fell 18% over 24 hours.`

### Suggested Data Fields

```text
asset
metric
previous_value
current_value
absolute_change
percent_change
severity
comparison_window
timestamp
```

### Acceptance Criteria

- User can see the most important changes at the top of the dashboard.
- Changes are ranked by severity or magnitude.
- The API returns structured change objects.
- The dashboard gracefully handles missing prior snapshots.

---

## 2. Add Stablecoin Profile Pages

### Objective

Create a dedicated detail page for each stablecoin.

### User Value

The current dashboard is useful for comparison. A profile page is better for researching one asset in depth.

### Functional Requirements

- Add a detail page route or view for each stablecoin:
  - UI route: `/stablecoins/{symbol}`
  - Existing API endpoint can be extended: `GET /stablecoins/{symbol}`
- Link from the overview table to each profile page.
- Each profile page should include:
  - Current price
  - Peg history
  - Supply history
  - Chain breakdown
  - Liquidity depth
  - Reserve composition
  - Score breakdown
  - Latest alerts or risk events
  - Data freshness by source

### Acceptance Criteria

- A user can click a stablecoin from the overview table and view a complete profile.
- Charts and tables on the profile page are scoped to the selected symbol.
- Missing data is shown as unavailable, not guessed.

---

## 3. Add Confidence and Freshness Indicators

### Objective

Make data freshness and confidence visible at the metric level.

### User Value

Users need to know whether they are looking at fresh data, stale data, or fallback data.

### Functional Requirements

- Add a freshness score per metric or source.
- Show stale badges next to individual fields.
- Add `last_updated` per source, not only a global timestamp.
- Add source status values:
  - `healthy`
  - `delayed`
  - `stale`
  - `failing`
  - `cached_fallback`
- Add warning if fallback data is being used.

### Suggested Status Rules

```text
Fresh: updated within expected cadence
Delayed: missed one expected refresh
Stale: missed two or more expected refreshes
Fallback: using cached or backup provider data
```

### Suggested Endpoint

- `GET /data-freshness`

### Acceptance Criteria

- Users can see freshness per provider and per key metric.
- The dashboard warns when using fallback or stale data.
- Freshness logic accounts for each pipeline cadence.

---

## 4. Add a Risk Events Timeline

### Objective

Track major risk-related changes over time.

### User Value

Users need context, not just current scores.

### Functional Requirements

- Create a `risk_events` table.
- Log major events when:
  - Peg deviation crosses a threshold
  - Overall score changes by more than 10 points
  - Liquidity drops sharply
  - Reserve report becomes stale
  - Supply changes sharply
  - API provider repeatedly fails
- Add an event timeline tab or section.
- Allow filtering by:
  - asset
  - severity
  - event type

### Example Event Types

```text
PEG_DEVIATION
LIQUIDITY_DROP
SUPPLY_SHOCK
SCORE_CHANGE
RESERVE_STALE
API_FAILURE
```

### Suggested Table

```sql
CREATE TABLE risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    metric_name TEXT,
    previous_value REAL,
    current_value REAL,
    triggered_at TIMESTAMP NOT NULL
);
```

### Suggested Endpoint

- `GET /risk-events`
- `GET /stablecoins/{symbol}/events`

### Acceptance Criteria

- Risk events are logged automatically by scheduled jobs.
- Users can view a chronological timeline.
- Events are filterable by symbol, severity, and event type.

---

## 5. Add Explainable Score Drilldowns

### Objective

Show users why each stablecoin received its risk score.

### User Value

A score is only useful if users understand the inputs behind it.

### Functional Requirements

- Add per-score explanation cards.
- Show the inputs behind each score.
- Show which input hurt the score most.
- Add score delta explanation versus the previous snapshot.
- Display formula and weights for each score.

### Example Explanation

`USDT overall score decreased from 86 to 79 because peg deviation widened, liquidity depth fell, and reserve report freshness weakened.`

### Acceptance Criteria

- Each score has visible input values.
- The dashboard explains what changed when scores move.
- Formulas and weights are visible in the UI.

---

## 6. Add Historical Risk Regime Labels

### Objective

Classify each asset's recent condition into a plain-language risk regime.

### User Value

Users can understand asset status quickly without interpreting raw scores and basis points.

### Suggested Regime Labels

- `Stable`
- `Mild stress`
- `Peg stress`
- `Liquidity stress`
- `Data quality concern`
- `High risk`

### Functional Requirements

- Define regime rules.
- Add regime label to overview table.
- Add regime history chart.
- Add regime transition events to `risk_events`.

### Example Rules

```text
Stable: peg deviation < 10 bps and overall score > 80
Mild stress: peg deviation between 10 and 50 bps or overall score between 60 and 80
High risk: peg deviation > 50 bps or overall score < 60
```

### Acceptance Criteria

- Each stablecoin has one current regime label.
- Regime transitions are tracked historically.
- Regime labels are consistent with score and peg logic.

---

## Priority 2: Better Market and Liquidity Insights

---

## 7. Add 24h and 7d Liquidity Change

### Objective

Show whether liquidity is improving or drying up.

### User Value

Liquidity movement can be more important than static liquidity depth.

### Functional Requirements

- Store liquidity snapshots over time.
- Compute:
  - 24-hour depth change
  - 7-day depth change
  - spread change
- Add liquidity history chart.
- Add `largest liquidity drops` table.

### Suggested Endpoint

- `GET /stablecoins/{symbol}/liquidity`

### Acceptance Criteria

- The dashboard shows liquidity trend direction.
- Users can identify assets with sharp liquidity deterioration.
- Missing historical liquidity data is handled gracefully.

---

## 8. Add Chain Concentration Risk

### Objective

Use supply-by-chain data to measure chain concentration risk.

### User Value

Users should know if a stablecoin is overly concentrated on one blockchain.

### Functional Requirements

- Parse chain distribution JSON into normalized rows.
- Add top-chain percentage.
- Add chain concentration score.
- Add chain heatmap by stablecoin.
- Add warning if one chain holds too much supply.

### Suggested Table

```sql
CREATE TABLE stablecoin_chain_supply (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP NOT NULL,
    symbol TEXT NOT NULL,
    chain TEXT NOT NULL,
    supply REAL,
    supply_pct REAL
);
```

### Useful Metric

```text
top_chain_concentration = largest_chain_supply / total_supply
```

Where:

- `largest_chain_supply` = stablecoin supply on the largest chain
- `total_supply` = total circulating supply across all chains
- `top_chain_concentration` = share of supply on the largest chain

### Suggested Endpoint

- `GET /stablecoins/{symbol}/chain-supply`

### Acceptance Criteria

- Chain distribution is queryable as rows, not only JSON.
- Dashboard shows top-chain concentration.
- Users can compare chain concentration across stablecoins.

---

## 9. Add Stablecoin Dominance and Market Share

### Objective

Show which stablecoins are gaining or losing share.

### User Value

Users can see market structure changes and competitive momentum.

### Functional Requirements

- Compute total tracked stablecoin supply.
- Compute market share per asset.
- Add 7-day and 30-day market share change.
- Add dominance chart.
- Add `gainers and losers` table.

### Suggested Endpoint

- `GET /stablecoins/rankings`

### Acceptance Criteria

- Dashboard shows stablecoin market share.
- Users can see market share gainers and losers.
- Market share is computed from tracked supply data.

---

## Priority 3: Backend and Production Readiness

---

## 10. Add More Backend Endpoints

### Objective

Add API routes that support the new user-facing features.

### Required Endpoints

```text
GET /stablecoins/{symbol}/supply
GET /stablecoins/{symbol}/liquidity
GET /stablecoins/{symbol}/chain-supply
GET /stablecoins/{symbol}/events
GET /stablecoins/changes
GET /stablecoins/rankings
GET /alerts
POST /alerts
PATCH /alerts/{id}
DELETE /alerts/{id}
GET /watchlist
POST /watchlist
GET /risk-events
GET /data-freshness
```

### Acceptance Criteria

- Each endpoint returns structured JSON.
- Endpoints reuse existing service/query functions where possible.
- Endpoints include error handling for unknown symbols and missing data.

---

## 11. Add Deployment Readiness Checks

### Objective

Prevent bad deployments before hosting the dashboard publicly or privately.

### Functional Requirements

- Expand `/health` to include:
  - database connected
  - latest successful pipeline run
  - provider availability
  - disk write access
  - app version
- Add `/ready` endpoint.
- Add startup checks for required environment variables.
- Add warning if SQLite is used in production.
- Add warning if app is running from `/tmp`.

### Acceptance Criteria

- Hosting platform can check readiness before routing traffic.
- Health endpoint distinguishes app alive from app ready.
- Production misconfigurations are visible.

---

## 12. Add Job Run History

### Objective

Track pipeline execution results for users and admins.

### User Value

Users and admins need to know whether the data pipelines are working.

### Functional Requirements

- Add `pipeline_runs` table.
- Log every pipeline:
  - start time
  - finish time
  - status
  - duration
  - rows written
  - error message
- Show job history in API Usage or Admin tab.
- Add failed pipeline callout.

### Suggested Table

```sql
CREATE TABLE pipeline_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline_name TEXT NOT NULL,
    started_at TIMESTAMP NOT NULL,
    finished_at TIMESTAMP,
    status TEXT NOT NULL,
    rows_written INTEGER DEFAULT 0,
    error_message TEXT,
    duration_seconds REAL
);
```

### Acceptance Criteria

- Every pipeline run is logged.
- Failed runs are visible in the dashboard.
- Users can see when each pipeline last succeeded.

---

## 13. Add Data Validation Rules

### Objective

Add runtime validation in addition to tests.

### User Value

Users should be warned when data looks wrong, stale, or incomplete.

### Functional Requirements

- Detect impossible prices.
- Detect negative supply.
- Detect stale timestamps.
- Detect duplicate snapshots.
- Detect sudden supply jumps.
- Detect missing chain distribution.
- Add warnings to the dashboard.

### Example Rules

```text
price must be between 0.90 and 1.10 for stablecoins
circulating_supply must be positive
peg_deviation_bps must be computed from price
latest data must be newer than expected cadence
```

### Suggested Table

```sql
CREATE TABLE data_quality_warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT,
    provider TEXT,
    metric_name TEXT NOT NULL,
    warning_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    detected_at TIMESTAMP NOT NULL,
    resolved_at TIMESTAMP
);
```

### Acceptance Criteria

- Validation warnings are stored.
- Dashboard surfaces active warnings.
- Invalid data does not silently affect risk scores.

---

## 14. Add Provider Fallback Status

### Objective

Make fallback usage visible.

### User Value

Users should know when Coinbase or cached data is being used because Binance failed or was unavailable.

### Functional Requirements

- Store whether each data point came from:
  - primary provider
  - fallback provider
  - cached response
- Show fallback badge in dashboard.
- Log fallback reason.
- Track fallback frequency.
- Alert if primary provider repeatedly fails.

### Suggested Fields

```text
provider
source_type
fallback_used
fallback_reason
primary_provider
fallback_provider
cached
timestamp
```

### Acceptance Criteria

- Users can see when fallback data is used.
- Provider reliability can be monitored over time.
- Repeated fallback usage can trigger an event or warning.

---

## 15. Add Cache Analytics

### Objective

Turn cache behavior into a visible operational metric.

### User Value

Users and admins can see how many API calls are being saved.

### Functional Requirements

- Show cache hit rate by provider.
- Show cache hit rate by endpoint.
- Show estimated requests saved.
- Show last cache invalidation.
- Add cache clear button for admin users only.

### Acceptance Criteria

- API Usage tab includes cache metrics.
- Cache hits and misses are distinguishable.
- Admin cache clear action is protected.

---

## Priority 4: Advanced User Features

---

## 16. Add DeFi Yield Comparison

### Objective

Show stablecoin yield opportunities by protocol and chain.

### User Value

Useful for users who care about stablecoin yield, DeFi allocation, or yield-adjusted risk.

### Functional Requirements

- Pull yield data from DefiLlama.
- Show APY by protocol and chain.
- Add TVL and risk filters.
- Add stablecoin-specific yield pages.
- Show historical APY volatility.
- Add warning for unusually high APYs.

### Acceptance Criteria

- Users can compare yields by asset, protocol, and chain.
- Dashboard distinguishes yield from risk.
- Very high APYs trigger cautionary warnings.

---

# Suggested Implementation Order

## Sprint 1: Make the Dashboard More Useful Immediately

1. Market Changes Summary
2. Stablecoin Profile Pages
3. Confidence and Freshness Indicators
4. CSV export for key tables
5. Improved overview table links and drilldowns

## Sprint 2: Make the Dashboard Risk-Focused

1. Risk Events Timeline
2. Explainable Score Drilldowns
3. Historical Risk Regime Labels
4. Reserve source tracking
5. Alert-ready event structure

## Sprint 3: Make the Dashboard Production-Ready

1. Job Run History
2. Data Validation Rules
3. Provider Fallback Status
4. Cache Analytics
5. Deployment Readiness Checks

## Sprint 4: Add More Market Intelligence

1. 24h and 7d Liquidity Change
2. Chain Concentration Risk
3. Stablecoin Dominance and Market Share
4. DeFi Yield Comparison
5. Additional liquidity providers

---

# Agent Implementation Rules

## General Rules

- Keep the dashboard user-facing and decision-focused.
- Do not add paid APIs unless explicitly approved.
- Do not call external APIs directly from UI code.
- All external API requests must go through the existing tracked and cached provider request layer.
- Store raw provider responses where already supported.
- Store normalized metrics in database tables.
- Add tests for every new model, service, endpoint, and pipeline.
- Make missing data explicit in the UI.
- Do not hallucinate reserve quality or issuer details.
- Prefer simple explainable scoring over opaque models.

## Database Rules

- Use SQLAlchemy 2.0 patterns.
- Keep migrations or schema updates explicit.
- Preserve SQLite compatibility.
- Avoid schema changes that require destructive migrations.
- Add indexes for frequently queried fields:
  - `symbol`
  - `timestamp`
  - `provider`
  - `pipeline_name`
  - `event_type`

## API Rules

- Return structured JSON.
- Include timestamps and source metadata.
- Handle unknown symbols gracefully.
- Do not expose internal stack traces.
- Validate query limits.
- Reuse service-layer functions rather than duplicating SQL in route handlers.

## UI Rules

- Show clear status labels.
- Use tooltips or expanders for methodology.
- Avoid overwhelming users with raw logs by default.
- Add drilldowns from summary views.
- Show fallback, stale, or missing data visibly.
- Keep manual refresh protected.

## Testing Rules

For every new feature, add tests for:

- database models
- service-layer logic
- API endpoints
- edge cases with missing data
- stale data handling
- invalid values
- fallback provider behavior where relevant

---

# Best Next 10 Tasks

These are the highest-value next tasks for an AI coding agent:

1. Implement `Market Changes Summary`.
2. Implement stablecoin profile pages.
3. Add metric-level freshness and confidence indicators.
4. Add `pipeline_runs` table and job run history UI.
5. Add data validation warnings.
6. Add provider fallback status visibility.
7. Add explainable score drilldowns.
8. Add risk events timeline.
9. Add chain concentration risk.
10. Add stablecoin dominance and market share.

---

# Discovered Issues

## Supply snapshots collide on ticker symbol

`supply_snapshots` is keyed only by `symbol`, but DefiLlama lists multiple
distinct assets that share a ticker (e.g. several coins all reported as `USDS`,
`DEUSD`, `USDX`). A single `update_supply` run therefore writes several rows per
symbol with the same `recorded_at` but very different supplies, and the dominant
asset behind a ticker can change between runs. This produces misleading
period-over-period moves (e.g. a 1700% jump or a 100% drop) in Market Changes
and an arbitrary "latest" row in the overview.

`services/market_changes.py` mitigates this by collapsing same-timestamp rows to
the max value, but the real fix is a data-model change: key `SupplySnapshot` (and
the overview/scoring joins) on the DefiLlama asset `id`, not the ticker, and
disambiguate display symbols. Should be tackled before relying on per-asset
supply history.