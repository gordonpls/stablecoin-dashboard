-- Stablecoin dashboard schema

CREATE TABLE IF NOT EXISTS stablecoins (
    id              TEXT PRIMARY KEY,
    symbol          TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    issuer          TEXT,
    peg_mechanism   TEXT,           -- fiat-backed | crypto-backed | algorithmic
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS supply_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol              TEXT NOT NULL REFERENCES stablecoins(symbol),
    circulating_supply  REAL NOT NULL,  -- USD value
    supply_by_chain     TEXT,           -- JSON: {chain: usd_amount}
    recorded_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_supply_symbol_time
    ON supply_snapshots(symbol, recorded_at DESC);

CREATE TABLE IF NOT EXISTS price_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol              TEXT NOT NULL REFERENCES stablecoins(symbol),
    price               REAL NOT NULL,
    peg_deviation_bps   REAL,
    bid_depth_usd       REAL,
    ask_depth_usd       REAL,
    source              TEXT NOT NULL,  -- exchange name
    recorded_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_price_symbol_time
    ON price_snapshots(symbol, recorded_at DESC);

CREATE TABLE IF NOT EXISTS reserve_reports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL REFERENCES stablecoins(symbol),
    report_url      TEXT,
    report_date     DATE,
    composition     TEXT,   -- JSON: {asset: pct}
    auditor         TEXT,
    ingested_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS risk_scores (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL REFERENCES stablecoins(symbol),
    peg_score       REAL NOT NULL CHECK(peg_score BETWEEN 0 AND 100),
    liquidity_score REAL NOT NULL CHECK(liquidity_score BETWEEN 0 AND 100),
    reserve_score   REAL NOT NULL CHECK(reserve_score BETWEEN 0 AND 100),
    adoption_score  REAL NOT NULL CHECK(adoption_score BETWEEN 0 AND 100),
    overall_score   REAL NOT NULL CHECK(overall_score BETWEEN 0 AND 100),
    scored_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_scores_symbol_time
    ON risk_scores(symbol, scored_at DESC);

CREATE TABLE IF NOT EXISTS risk_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,          -- stablecoin ticker, or "SYSTEM" for global events
    event_type      TEXT NOT NULL,          -- PEG_DEVIATION | LIQUIDITY_DROP | SUPPLY_SHOCK | SCORE_CHANGE | RESERVE_STALE | API_FAILURE
    severity        TEXT NOT NULL,          -- low | medium | high
    title           TEXT NOT NULL,
    description     TEXT,
    metric_name     TEXT,
    previous_value  REAL,
    current_value   REAL,
    triggered_at    TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_risk_events_time
    ON risk_events(triggered_at DESC);

CREATE INDEX IF NOT EXISTS idx_risk_events_symbol_time
    ON risk_events(symbol, triggered_at DESC);

CREATE TABLE IF NOT EXISTS regime_snapshots (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol            TEXT NOT NULL,
    regime            TEXT NOT NULL,      -- Stable | Mild stress | Peg stress | Liquidity stress | Data quality concern | High risk
    severity          TEXT NOT NULL,      -- low | medium | high
    reason            TEXT,
    overall_score     REAL,
    peg_deviation_bps REAL,
    classified_at     TIMESTAMP NOT NULL  -- only written when the regime changes
);

CREATE INDEX IF NOT EXISTS idx_regime_symbol_time
    ON regime_snapshots(symbol, classified_at DESC);

CREATE TABLE IF NOT EXISTS data_quality_warnings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT,                  -- stablecoin ticker, or NULL for non-asset warnings
    provider     TEXT,
    metric_name  TEXT NOT NULL,
    warning_type TEXT NOT NULL,         -- IMPOSSIBLE_PRICE | NON_POSITIVE_SUPPLY | PEG_DEVIATION_MISMATCH | SUPPLY_JUMP | DUPLICATE_SNAPSHOT | MISSING_CHAIN_DISTRIBUTION
    severity     TEXT NOT NULL,         -- low | medium | high
    message      TEXT NOT NULL,
    detected_at  TIMESTAMP NOT NULL,
    resolved_at  TIMESTAMP              -- NULL while the warning is active
);

CREATE INDEX IF NOT EXISTS idx_dq_warnings_active
    ON data_quality_warnings(resolved_at, detected_at DESC);

CREATE INDEX IF NOT EXISTS idx_dq_warnings_symbol
    ON data_quality_warnings(symbol, warning_type);

CREATE TABLE IF NOT EXISTS provider_fallback_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol            TEXT NOT NULL,
    data_type         TEXT NOT NULL,         -- currently always "price"
    primary_provider  TEXT NOT NULL,         -- provider tried first, e.g. binance
    fallback_provider TEXT,                  -- configured fallback, e.g. coinbase
    source_provider   TEXT,                  -- provider that actually served the data; NULL if unavailable
    source_type       TEXT NOT NULL,         -- fallback | unavailable
    fallback_reason   TEXT,                  -- why the primary was skipped/failed
    recorded_at       TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fallback_events_time
    ON provider_fallback_events(recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_fallback_events_symbol_time
    ON provider_fallback_events(symbol, recorded_at DESC);

CREATE TABLE IF NOT EXISTS api_request_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    provider     TEXT NOT NULL,
    endpoint     TEXT NOT NULL,         -- logical name, e.g. "stablecoins"
    url          TEXT NOT NULL,
    status_code  INTEGER,
    raw_response TEXT,                  -- first 4096 chars of response body
    requested_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_log_provider_time
    ON api_request_log(provider, requested_at DESC);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline_name    TEXT NOT NULL,         -- e.g. update_supply, score_stablecoins
    started_at       TIMESTAMP NOT NULL,
    finished_at      TIMESTAMP,
    status           TEXT NOT NULL,         -- success | error
    rows_written     INTEGER DEFAULT 0,
    error_message    TEXT,
    duration_seconds REAL
);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_name_time
    ON pipeline_runs(pipeline_name, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_time
    ON pipeline_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS watchlist (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol    TEXT NOT NULL UNIQUE REFERENCES stablecoins(symbol),  -- one row per asset
    note      TEXT,                                                 -- optional operator note
    added_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
