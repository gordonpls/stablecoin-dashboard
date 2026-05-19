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
