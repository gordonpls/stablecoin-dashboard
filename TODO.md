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

## Priority 0: Stability First — Fix Errors Before Features  (STANDING — RUN EVERY ITERATION)

**This section always outranks everything below it.** Before implementing ANY
feature, confirm the app is healthy. If it is not, fixing it IS this iteration's
task. Do not start feature work on a broken app.

### Health check (do this first, every iteration)

1. Run the test suite: `python -m pytest tests/ -x -q`.
   If anything fails, stop and fix it. Never begin a feature with a red suite.
2. Confirm the app imports without raising:
   - `python -c "import app.dashboard.main"`
   - `python -c "import app.api.server"`
3. Scan for obvious runtime errors and rough edges:
   - Unhandled exceptions or tracebacks in logs
   - Broken/missing imports
   - Crashes on empty or missing data (`None` / `NaN` / `NaT`, empty DataFrames)
   - API endpoints returning 500s
   - Pipelines that fail silently or write malformed data
   - Mislabeled or misleading UI values (e.g. a metric that is always 0)
4. Clear the running bug list in the **Discovered Issues** section at the bottom
   of this file. Those are known, already-triaged defects — fix them (highest
   severity first) before any new feature. When one is fixed, remove it from
   Discovered Issues and note it in `CHANGELOG.md`.
5. Only once the suite is green, the app imports, and Discovered Issues is empty,
   drop down to the highest-priority unfinished feature.

### Rules

- A fix ALWAYS outranks a feature. Found a bug? Fixing it is the whole iteration.
- Reproduce the error first, then fix it, then add a regression test.
- If a fix and a feature are entangled, ship the fix as its own iteration.
- Any new bug you notice goes into **Discovered Issues**, not silently ignored.
- Record every fix in `CHANGELOG.md`: what broke, why, and how it was fixed.

---

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

## 2. Add Stablecoin Profile Pages  (DONE 2026-05-21)

Implemented in `services/profile.py` (`get_stablecoin_profile`), exposed via
`GET /stablecoins/{symbol}/profile`, and surfaced as a new "Asset Profile" tab
plus an inline profile when a row is selected in the Overview table. Each
profile shows current price/peg/supply/score metrics, per-source freshness
pills, a score breakdown with the weakest dimension called out, price/supply/
score history charts, chain distribution (with concentration warning), order
book liquidity, and reserve composition — all scoped to the selected symbol,
with missing data shown explicitly. Deep-linkable via `?symbol=`. Tests in
`tests/test_profile.py`. (Note: this lays groundwork for #3 freshness and #8
chain concentration, but those remain to be built out fully — e.g. a global
`/data-freshness` endpoint and a cross-asset concentration table.)

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

## 3. Add Confidence and Freshness Indicators  (DONE 2026-05-21)

Implemented in `services/freshness.py` (`compute_data_freshness`), exposed via
`GET /data-freshness`, and surfaced as a "Data Freshness" section at the top of
the API Usage tab. Reports, for each source (prices, liquidity, scores, supply,
reserves): last update, age, expected cadence, `fresh`/`delayed`/`stale`/
`missing` status, and assets covered — plus an `overall_status` (worst source)
and a per-provider request-health table (`healthy`/`failing`/`missing` from
`api_request_log`). The dashboard warns when any source is stale/missing or a
provider's last request errored. Per-asset freshness already lived in
`services/profile.py`; this adds the global view. Tests in
`tests/test_freshness.py`.

Remaining from the original spec (not yet built): an explicit `cached_fallback`
status and a "fallback in use" warning tied to which provider actually served
each data point — that depends on per-row source tracking and belongs with
task #14 (Provider Fallback Status). The `failing` provider signal here is a
first step toward it.

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

## 4. Add a Risk Events Timeline  (DONE 2026-05-21)

Implemented as a `risk_events` table (`db/models.py` `RiskEvent`, `db/schema.sql`)
plus `services/risk_events.py`. `log_new_events()` detects six event types —
`PEG_DEVIATION`, `LIQUIDITY_DROP`, `SUPPLY_SHOCK`, `SCORE_CHANGE`,
`RESERVE_STALE`, `API_FAILURE` — by comparing the two most recent snapshots of
each metric (step-change semantics), and is called automatically at the end of
the scoring pipeline (`pipelines/score_stablecoins.py`). Detection is
idempotent: events de-duplicate on `(symbol, event_type, triggered_at,
metric_name)`, so re-running over unchanged data inserts nothing. Exposed via
`GET /risk-events` (filterable by symbol/severity/event_type) and
`GET /stablecoins/{symbol}/events`, and surfaced as a new "Risk Events" tab
with client-side asset/type/severity filters and a severity-coloured timeline.
Tests in `tests/test_risk_events.py`.

Remaining / follow-ups:
- Wire the latest events per asset into the Asset Profile page (the profile
  spec in #2 listed "Latest alerts or risk events" but it was not built).
- `SCORE_CHANGE` currently compares consecutive 10-minute score snapshots; a
  longer comparison window (e.g. 24h) may better match user intuition for
  "score changed by 10 points".
- Regime transitions (#6) should emit `risk_events` rows once regimes exist.

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

## 5. Add Explainable Score Drilldowns  (DONE 2026-05-21)

Implemented in `services/score_explanation.py` (`explain_scores`), exposed via
`GET /stablecoins/{symbol}/score-explanation`, and surfaced both in the Risk
Scores tab ("Why does {asset} score what it does?" drilldown, reusing the score-
history asset selector) and on the Asset Profile score breakdown. For the latest
stored `RiskScore` it returns, per dimension: the raw inputs that drove it (peg
deviation, bid/ask depth, reserve age + auditor, circulating supply, each with
its own snapshot timestamp), the weight, the points it contributes to the
overall, and a plain-language formula/detail. It identifies the **weakest**
dimension by weighted points lost vs a perfect 100 (not raw min), and explains
the **delta** versus the prior snapshot in prose (e.g. "USDT overall score fell
from 86 to 79 because liquidity depth fell and peg deviation widened"). Weights
were extracted to a single `SCORE_WEIGHTS` constant in
`pipelines/score_stablecoins.py` so the drilldown can never disagree with the
pipeline. Missing inputs are shown explicitly (neutral-default notes). Tests in
`tests/test_score_explanation.py`.

Remaining / follow-ups:
- The narrative compares consecutive 10-minute snapshots; a 24h comparison
  window (matching Market Changes / the SCORE_CHANGE risk event) may read more
  intuitively for "what changed today".
- Score-delta `risk_events` (#4 SCORE_CHANGE) and this narrative now overlap;
  consider sourcing the profile's "Latest alerts" from one of them.

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

## 6. Add Historical Risk Regime Labels  (DONE 2026-05-21)

Implemented in `services/regimes.py`. `classify_regime()` is a pure, deterministic
function of the latest overall score, peg deviation (bps), liquidity-dimension
score, and whether an active data-quality warning is open — returning one of six
regimes: `Stable`, `Mild stress`, `Data quality concern`, `Liquidity stress`,
`Peg stress`, `High risk`. Thresholds mirror the dashboard's `risk_label` bands
and the `risk_events` peg thresholds, so the regime never disagrees with the
score/peg logic shown elsewhere. `record_regimes()` (called from the scoring
pipeline, best-effort) appends a `RegimeSnapshot` **only when an asset's regime
changes**, so the new `regime_snapshots` table (model + `db/schema.sql`) is a
compact, idempotent transition history. `services/risk_events.py` gained a
`REGIME_CHANGE` event type + `_detect_regime` detector that turns each transition
into an event (severity by destination regime; "deteriorated"/"improved"
wording). Exposed via `GET /regimes` (current regime per asset, most severe
first) and `GET /stablecoins/{symbol}/regime` (current + history). Surfaced as a
colour-coded **Regime** column in the Overview table, a current-regime callout +
**Risk Regime History** step chart on the Asset Profile, and Regime Change rows
in the Risk Events tab. Tests in `tests/test_regimes.py`.

Remaining / follow-ups:
- The classifier uses the *latest* peg reading regardless of the price
  staleness window; a very old price could classify on a stale peg. Consider a
  recency cutoff (the scoring pipeline already uses a 2h price cutoff).
- `Liquidity stress` triggers on `liquidity_score < 30` — a heuristic; tune once
  there's real liquidity history (ties into #7).
- Surface the current regime in the header KPI strip / a regime-distribution
  count, and wire the latest regime + events into the profile's "Latest alerts"
  (still open from #2 / #4).

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

## 7. Add 24h and 7d Liquidity Change  (DONE 2026-05-21)

Implemented in `services/liquidity.py`. Order-book depth already accumulates in
`price_snapshots` (bid/ask depth, written by both the price and liquidity
pipelines), so this turns that raw history into trend signals without a schema
change. `get_liquidity_detail(symbol)` returns current depth, 24h and 7d
total-depth change (absolute + percent + severity, reusing the
market_changes liquidity thresholds), a bid/ask depth-imbalance metric and its
24h move (a thin-side proxy for spread — we store depth, not best bid/ask, so a
true spread is unavailable), a plain-language `trend`
(improving/deteriorating/stable), and a chartable depth history.
`largest_liquidity_drops(window, limit)` ranks the sharpest cross-asset depth
declines. A window comparison requires a point at least half the window old, so
a "24h change" is never claimed from an hour of data — insufficient history
returns `None` rather than a misleading number. Exposed via
`GET /stablecoins/{symbol}/liquidity` and `GET /stablecoins/liquidity-drops`
(window=24h|7d), and surfaced as an enhanced Order Book Liquidity section on the
Asset Profile (trend callout + depth-over-time chart) plus a "Largest Liquidity
Drops" table in the Peg Deviation tab. Tests in `tests/test_liquidity.py`.

Remaining / follow-ups:
- True bid/ask *spread* (vs the depth-imbalance proxy used here) needs storing
  best bid/ask prices in `price_snapshots` — a schema + ingestion change.
- Liquidity history is read from `price_snapshots`; consider a dedicated
  `liquidity_snapshots` table only if depth needs a different cadence/retention
  from prices.

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

## 8. Add Chain Concentration Risk  (DONE 2026-05-21)

Implemented in `services/chain_concentration.py`. The supply-by-chain breakdown
already lives as JSON in `supply_snapshots.supply_by_chain`, so this derives
concentration signals on read (no new table / pipeline change, consistent with
the #7 liquidity approach) using the single canonical chain parser
(`services.profile._parse_chains`) so the profile page and this service can never
disagree. `get_chain_concentration(symbol)` returns normalized per-chain rows
plus the top chain + its share (the spec's `top_chain_concentration`, as a
0–100 percent), an HHI (0–10000), a graded `concentration_level`/`severity`, and
a `warning` flag (fires at ≥75% on one chain or single-chain, matching the
existing Asset Profile threshold). `chain_concentration_ranking(limit)` ranks
assets most-concentrated-first (severity → top-chain share → HHI), collapsing
ticker-collision rows to the dominant asset and omitting assets with no parseable
breakdown. Exposed via `GET /stablecoins/{symbol}/chain-supply` and
`GET /stablecoins/chain-concentration` (registered before `/stablecoins/{symbol}`
so it isn't shadowed), and surfaced as a "Chain Concentration Risk" section in
the Supply tab: a highly-concentrated-asset warning callout, an asset×chain
supply-share heatmap (top 12 assets, top 10 chains + "Other"), and a sortable
comparison table (top chain, top-chain %, chain count, HHI, level). Tests in
`tests/test_chain_concentration.py`.

Remaining / follow-ups:
- Wire the per-asset concentration block (HHI + level) into the Asset Profile
  page's existing Chain Distribution section (it currently shows only the 75%
  callout); reuse `get_chain_concentration`.
- The Supply-tab heatmap loads each top-asset's detail separately
  (`load_chain_supply` ×12, all cached). If a normalized
  `stablecoin_chain_supply` table is ever added (the spec's suggestion), the
  heatmap and ranking could read it directly, and chain *history* (share drift
  over time) would become queryable.

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

## 9. Add Stablecoin Dominance and Market Share  (DONE 2026-05-22)

Implemented in `services/dominance.py`. `compute_dominance()` reads
`supply_snapshots` (no schema/pipeline change, consistent with the #7/#8
read-time approach), collapses ticker-collision rows to the dominant value, and
returns total tracked supply, asset count, the dominant asset + its share, and a
`rankings` list (market share desc) where each asset carries 7d and 30d
market-share-ago + share-change in percentage points. A window's "share N days
ago" is only computed from a snapshot at least half the window old (otherwise
`None` — insufficient history), and the past-share denominator sums only assets
with sufficiently-old history so a newly-tracked coin is not credited a phantom
swing. `market_share_movers(window)` derives gainers/losers from the same
rankings. Exposed via `GET /stablecoins/rankings?window=7d|30d` (registered
before `/stablecoins/{symbol}` so it isn't shadowed; bundles `rankings` +
`movers`) and surfaced as a "Market Dominance & Share" section in the Supply tab:
headline dominance KPIs, a market-share donut (top 10 + Other), a gainers/losers
split for the selected window, and a full ranking table. Tests in
`tests/test_dominance.py`.

Remaining / follow-ups:
- Share history over time (a dominance-over-time stacked area) would need either
  storing computed shares or recomputing per historical snapshot; the current
  view is point-in-time + window deltas.
- The market-share denominator is *tracked* supply, not the whole stablecoin
  market — fine for relative momentum, but note it in the UI if more assets are
  added later.
- Per-asset dominance (current share + 7d/30d change) could be wired into the
  Asset Profile page's supply section, reusing `compute_dominance`.

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

## 10. Add More Backend Endpoints  (PARTIALLY DONE)

### Objective

Add API routes that support the new user-facing features.

### Required Endpoints

```text
GET /stablecoins/{symbol}/supply     DONE 2026-05-22 (services/supply.py)
GET /stablecoins/{symbol}/liquidity  DONE (#7)
GET /stablecoins/{symbol}/chain-supply DONE (#8)
GET /stablecoins/{symbol}/events     DONE (#4)
GET /stablecoins/changes             DONE (#1)
GET /stablecoins/rankings            DONE (#9)
GET /alerts                          TODO (stateful write feature — see note)
POST /alerts                         TODO
PATCH /alerts/{id}                   TODO
DELETE /alerts/{id}                  TODO
GET /watchlist                       DONE 2026-05-22 (services/watchlist.py)
POST /watchlist                      DONE 2026-05-22
DELETE /watchlist/{symbol}           DONE 2026-05-22
GET /risk-events                     DONE (#4)
GET /data-freshness                  DONE (#3)
```

The **watchlist** iteration is now done (see below). The only remaining write
feature is the **alerts** CRUD set. Alerts are a stateful *write* feature: a new
table (`alerts`), a service layer, and — crucially — a **password-protected** UI,
because anonymous write controls that change app behaviour are not allowed (see
memory: `feedback_dashboard_controls`; reuse the `DASHBOARD_PASSWORD` gate the
watchlist editor and manual refresh already share). Alerts also need an
*evaluation* step (check each active rule against the latest snapshot and surface
triggered alerts), which overlaps `services/risk_events.py` — design alerts as
user-defined thresholds that reuse the same comparison primitives rather than a
parallel detector. Implement alerts end-to-end (model + service + endpoints +
gated UI + tests) in a single iteration so it does not land half-finished.

### Watchlist (DONE 2026-05-22)

`watchlist` table (`WatchlistItem` model + `db/schema.sql`, one row per symbol,
unique) + `services/watchlist.py`. `add_to_watchlist`/`remove_from_watchlist`
normalise the symbol and only accept assets present in `stablecoins` (unknown
symbols are rejected, never invented); `add` is idempotent and updates the note.
`set_watchlist` syncs the list to a desired set (used by the dashboard
multiselect, skips unknowns). `get_watchlist` returns watched assets newest-first
enriched with latest price / peg deviation / supply / overall score (null where
absent). Exposed via `GET /watchlist`, `POST /watchlist` (404 on unknown symbol),
`DELETE /watchlist/{symbol}` (404 when not watched). Surfaced as a ⭐ Watchlist
panel atop the Overview tab, a ⭐ marker column + "Watchlist only" filter on the
overview table, and a **password-gated** multiselect editor in the sidebar.
Tests in `tests/test_watchlist.py`.

Follow-ups:
- The watchlist is a single global list (no per-user auth); revisit if/when an
  auth/user model is added so each user gets their own.
- The ⭐ marker / watchlist filter could be mirrored on the Asset Profile and in
  other comparison tables (Risk Scores, Supply) for consistency.
- A `note` is only editable via the API today; the sidebar editor manages
  membership only. Add note editing to the gated UI if useful.

### Supply endpoint shape (DONE)

`GET /stablecoins/{symbol}/supply?history_days=&history_limit=` →
`services.supply.get_supply_detail`: latest supply + chain breakdown, 7d/30d
supply change (null on insufficient history), and a deduped supply time series.
404 only for a completely unknown symbol; a known asset with no supply data
returns null sections. Reuses the canonical `services.profile._parse_chains`
parser and the same ticker-collision / insufficient-history guards as
`services/dominance.py`. Tests in `tests/test_supply.py`. Not yet surfaced in the
dashboard (the Supply tab and Asset Profile already chart supply via other
services); wire `/supply` in if a single source for per-asset supply history is
wanted.

### Acceptance Criteria

- Each endpoint returns structured JSON.
- Endpoints reuse existing service/query functions where possible.
- Endpoints include error handling for unknown symbols and missing data.

---

## 11. Add Deployment Readiness Checks  (DONE 2026-05-22)

Implemented in `services/readiness.py`. `get_readiness()` runs six checks —
`database` (connectivity via `SELECT 1`), `disk` (writability of the directory
backing the SQLite file), `environment` (operator-declared required vars via the
opt-in `REQUIRED_ENV_VARS` allowlist), `configuration` (production-only warnings:
SQLite in prod, DB under the temp dir, app running from the temp dir),
`pipelines` (reuses `pipeline_status_summary` for latest success / failing
pipelines), and `providers` (reuses `compute_data_freshness` provider health) —
and rolls them into a verdict. Only **database connectivity** and **missing
required env vars** are critical (block readiness); everything else is a
non-blocking warning. `GET /health` is now liveness + diagnostics (always 200,
carries the full `checks` block plus `ready`/`readiness_status`/`version`) while
the new `GET /ready` returns **503 when not ready** so an orchestrator can gate
traffic. App version is read from installed package metadata (falls back to
`0.1.0`). Surfaced as a "Deployment Readiness" panel at the top of the API Usage
tab (verdict + version, failing/degraded callout, per-check status pills, detail
table). Tests in `tests/test_readiness.py`; existing `/health` test updated for
the expanded contract.

Remaining / follow-ups:
- "Startup checks for required env vars" are evaluated lazily per request via the
  `environment` check rather than crashing the process at boot — deliberately, so
  a misconfiguration is *visible* (and shows in `/health`) instead of taking the
  app down before it can report why. A hard startup gate could be added as a
  FastAPI/Streamlit startup hook that calls `check_environment()` and refuses to
  start on a critical fail, if fail-fast is preferred over fail-visible.
- The production-misconfig signal overlaps the "SQLite fallback to /tmp" logic in
  `db/models._default_db_url`; consider having that fallback emit a one-off
  `risk_events`/log entry so an ephemeral-DB deployment is recorded, not just
  surfaced on read.

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

## 12. Add Job Run History  (DONE 2026-05-21)

Implemented as a `pipeline_runs` table (`db/models.py` `PipelineRun`,
`db/schema.sql`) plus `services/pipeline_runs.py`. `record_run(name)` is a
context manager wrapped around every pipeline's `run()` (`update_supply`,
`update_prices`, `update_liquidity`, `update_reserves`, `score_stablecoins`):
it captures start/finish time, duration, status (`success`/`error`),
`rows_written` (set by the pipeline via the yielded handle), and the truncated
error message — always persisting a row even on failure, then re-raising so
caller behaviour is unchanged. Recording is best-effort (a logging failure
never masks the pipeline outcome). Exposed via `GET /pipeline-runs` (returns a
per-pipeline `summary` + filterable `runs` log) and surfaced as a "Pipeline
Runs" section at the top of the API Usage tab: per-pipeline health pills, a
summary table (last status / last run / last success / rows / duration / 24h
failures), a failed-pipeline callout, and an expandable recent-runs table.
Tests in `tests/test_pipeline_runs.py`.

Follow-ups: the dashboard's freshness `failing` provider signal and this run
history together cover most of #11 (deployment readiness) — a `/ready`
endpoint and an expanded `/health` (last successful pipeline run, db
connectivity) could now reuse `pipeline_status_summary()`.

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

## 13. Add Data Validation Rules  (DONE 2026-05-21)

Implemented as a `data_quality_warnings` table (`db/models.py`
`DataQualityWarning`, `db/schema.sql`) plus `services/data_validation.py`.
`run_validation()` detects six warning types — `IMPOSSIBLE_PRICE` (price outside
[0.90, 1.10]), `NON_POSITIVE_SUPPLY`, `PEG_DEVIATION_MISMATCH` (stored
`peg_deviation_bps` disagrees with `|price-1|*10_000`), `SUPPLY_JUMP` (≥50%
single-interval move — a data error, set well above the 5–10% SUPPLY_SHOCK
risk-event band so the two surfaces don't double-flag normal moves),
`DUPLICATE_SNAPSHOT` (the documented DefiLlama ticker collision), and
`MISSING_CHAIN_DISTRIBUTION` — and is auto-called (best-effort) at the end of
the scoring pipeline. Warnings have a lifecycle: a row opens (`resolved_at`
NULL) when first detected and resolves on the first run where the data no
longer trips the rule; identity is `(symbol, metric_name, warning_type)` among
open rows, so re-running is idempotent. Exposed via `GET /data-quality`
(active-warning `summary` + filterable `warnings`, `active_only` toggle for
history) and surfaced as a "Data Quality" panel in the API Usage tab (severity
pills, headline callout, severity-sorted detail table). Tests in
`tests/test_data_validation.py`.

Remaining / follow-ups:
- Wire active warnings into the Asset Profile page and add a stale/duplicate
  badge in the Overview table, so users see data caveats next to the figures
  themselves rather than only in the API Usage tab.
- Warning `message`/`severity` are captured at open time and not refreshed
  while the warning stays open (e.g. a price that drifts further out of band);
  refreshing the open row on each run would keep the detail current.
- Per-symbol staleness validation ("latest data newer than expected cadence",
  from the original spec) was intentionally left to `services/freshness.py` to
  avoid duplicating the system-wide freshness panel; revisit if a *per-asset*
  staleness signal proves useful.

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

## 14. Add Provider Fallback Status  (DONE 2026-05-22)

Implemented in `services/provider_fallback.py`, fixing a real bug along the way:
`pipelines/update_prices.py` hard-coded `source="binance"` on every price
snapshot even when ingestion had actually fallen back to Coinbase, so fallback
usage was invisible. `ingestion/exchanges.get_peg_prices` now returns per-symbol
provenance (`price_source`, `source_type` primary/fallback/unavailable,
`fallback_used`, `fallback_reason`); the price pipeline stores the real
`price_source` and calls `record_fallback_events()` (best-effort, idempotent on
`(symbol, data_type, source_type, recorded_at)`) which logs a row to the new
`provider_fallback_events` table only for the *exceptional* outcomes (a fallback
served the price, or no price was available — never the normal primary path,
keeping the table compact like `risk_events`). `get_fallback_status(window_hours)`
derives the healthy primary-vs-fallback *rate* and each asset's current source
from `price_snapshots.source` (excluding the liquidity pipeline's
`exchanges_depth` rows), pulls the *reason* from the event table, and grades the
primary provider `healthy`/`degraded`/`failing`/`unknown` (failing on ≥50%
fallback rate in-window or any unavailable price). Exposed via
`GET /provider-fallback?window_hours=&recent_limit=` and surfaced as a "Provider
Fallback" panel in the API Usage tab (primary-health KPI strip, degraded/failing
callout, per-asset current-source table with an On-Fallback badge, recent-events
expander) plus a Coinbase-fallback flag on the Asset Profile price card. Tests in
`tests/test_provider_fallback.py`.

Remaining / follow-ups:
- The liquidity pipeline (`update_liquidity`) queries Binance depth only and
  still writes a generic `source="exchanges_depth"`; depth has no fallback
  provider, so a Binance depth outage is silently absent rather than recorded as
  an availability event. Consider logging a `data_type="depth"` unavailable
  event there too.
- The "primary repeatedly fails" signal here overlaps the `API_FAILURE`
  risk event (#4) and the `failing` provider pill in `services/freshness.py`;
  consider having one of them cite the others so the three don't drift.
- Surface the per-asset On-Fallback badge in the Overview table (currently only
  the Asset Profile + API Usage tab show it).

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

1. ~~Implement `Market Changes Summary`.~~ (DONE 2026-05-21)
2. ~~Implement stablecoin profile pages.~~ (DONE 2026-05-21)
3. ~~Add metric-level freshness and confidence indicators.~~ (DONE 2026-05-21 — `/data-freshness` + Data Freshness panel)
4. ~~Add `pipeline_runs` table and job run history UI.~~ (DONE 2026-05-21 — `/pipeline-runs` + Pipeline Runs panel)
5. ~~Add data validation warnings.~~ (DONE 2026-05-21 — `/data-quality` + Data Quality panel)
6. ~~Add provider fallback status visibility.~~ (DONE 2026-05-22 — `services/provider_fallback.py`, `provider_fallback_events` table, `/provider-fallback`, API-Usage "Provider Fallback" panel; also fixed the hard-coded `source="binance"` bug in `update_prices`)
7. ~~Add explainable score drilldowns.~~ (DONE 2026-05-21 — `/stablecoins/{symbol}/score-explanation` + Risk Scores / Profile drilldown)
8. ~~Add risk events timeline.~~ (DONE 2026-05-21)
9. ~~Add chain concentration risk.~~ (DONE 2026-05-21 — `services/chain_concentration.py`, `/stablecoins/{symbol}/chain-supply` + `/stablecoins/chain-concentration`, Supply-tab heatmap + table)
10. ~~Add stablecoin dominance and market share.~~ (DONE 2026-05-22 — `services/dominance.py`, `/stablecoins/rankings`, Supply-tab Market Dominance section)

---

# Discovered Issues

## Deprecated Streamlit `use_container_width` will break after 2025-12-31

Every `st.plotly_chart`/`st.dataframe` call in `app/dashboard/main.py` passes
`use_container_width=True`, which Streamlit warns will be removed after
2025-12-31 (replace with `width="stretch"`). Likewise `st.components.v1.html`
(the sidebar countdown timer) is deprecated in favour of `st.iframe` after
2026-06-01. These are cosmetic warnings today but will become hard failures —
do a sweep before those dates. (New code added 2026-05-21 intentionally matched
the existing pattern for consistency; migrate the whole file at once.)

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