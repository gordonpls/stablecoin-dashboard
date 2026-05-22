"""Tests for db.models — ORM shapes, session factory, and init_db."""

from datetime import date, datetime

import pytest
from sqlalchemy import inspect, text

from db.models import (
    ApiRequestLog,
    PriceSnapshot,
    ReserveReport,
    RiskScore,
    Stablecoin,
    SupplySnapshot,
    get_session,
    init_db,
)


# ── to_dict shape tests ────────────────────────────────────────────────────────

def test_stablecoin_to_dict():
    row = Stablecoin(id="1", symbol="USDT", name="Tether", issuer="fiat-backed")
    d = row.to_dict()
    assert set(d) == {"id", "symbol", "name", "issuer", "peg_mechanism", "created_at", "updated_at"}
    assert d["symbol"] == "USDT"
    assert d["name"] == "Tether"


def test_supply_snapshot_to_dict():
    now = datetime.utcnow()
    row = SupplySnapshot(symbol="USDT", circulating_supply=1e9, recorded_at=now)
    d = row.to_dict()
    assert set(d) == {"id", "symbol", "circulating_supply", "supply_by_chain", "recorded_at"}
    assert d["circulating_supply"] == 1e9


def test_price_snapshot_to_dict():
    now = datetime.utcnow()
    row = PriceSnapshot(
        symbol="USDT", price=1.0002, peg_deviation_bps=2.0,
        bid_depth_usd=1_000_000, ask_depth_usd=2_000_000,
        source="binance", recorded_at=now,
    )
    d = row.to_dict()
    assert set(d) == {
        "id", "symbol", "price", "peg_deviation_bps",
        "bid_depth_usd", "ask_depth_usd", "source", "recorded_at",
    }
    assert d["price"] == 1.0002
    assert d["source"] == "binance"


def test_reserve_report_to_dict():
    row = ReserveReport(
        symbol="USDT",
        report_url="https://example.com",
        report_date=date(2025, 4, 1),
        composition='{"US_Treasuries": 0.84}',
        auditor="BDO",
    )
    d = row.to_dict()
    assert set(d) == {"id", "symbol", "report_url", "report_date", "composition", "auditor", "ingested_at"}
    assert d["auditor"] == "BDO"


def test_risk_score_to_dict():
    now = datetime.utcnow()
    row = RiskScore(
        symbol="USDT",
        peg_score=95.0, liquidity_score=80.0,
        reserve_score=70.0, adoption_score=100.0,
        overall_score=87.25, scored_at=now,
    )
    d = row.to_dict()
    assert set(d) == {
        "id", "symbol", "peg_score", "liquidity_score",
        "reserve_score", "adoption_score", "overall_score", "scored_at",
    }
    assert d["overall_score"] == 87.25


def test_api_request_log_to_dict():
    now = datetime.utcnow()
    row = ApiRequestLog(
        provider="defillama", endpoint="stablecoins",
        url="https://stablecoins.llama.fi/stablecoins",
        status_code=200, raw_response='{"peggedAssets":[]}',
        requested_at=now,
    )
    d = row.to_dict()
    assert set(d) == {
        "id", "provider", "endpoint", "url",
        "status_code", "raw_response", "requested_at",
    }
    assert d["provider"] == "defillama"


# ── session factory ────────────────────────────────────────────────────────────

def test_get_session_adds_and_queries(in_memory_db):
    coin = Stablecoin(id="t1", symbol="DAI", name="Dai", issuer="crypto-backed")
    with get_session() as s:
        s.add(coin)
        s.commit()

    with get_session() as s:
        from sqlalchemy import select
        result = s.execute(select(Stablecoin).where(Stablecoin.symbol == "DAI")).scalar_one()
        assert result.name == "Dai"


def test_get_session_rollback_on_error(in_memory_db):
    """Session auto-rolls back when an exception propagates out of the context."""
    try:
        with get_session() as s:
            s.add(Stablecoin(id="x1", symbol="FAIL", name="Fail", issuer=None))
            raise RuntimeError("intentional")
    except RuntimeError:
        pass

    with get_session() as s:
        from sqlalchemy import select
        result = s.execute(select(Stablecoin).where(Stablecoin.symbol == "FAIL")).scalar_one_or_none()
        assert result is None


# ── init_db ────────────────────────────────────────────────────────────────────

def test_init_db_creates_all_tables(in_memory_db):
    init_db()
    inspector = inspect(in_memory_db)
    tables = set(inspector.get_table_names())
    assert {"stablecoins", "supply_snapshots", "price_snapshots",
            "reserve_reports", "risk_scores", "api_request_log"} <= tables


def test_init_db_is_idempotent(in_memory_db):
    """Calling init_db twice must not raise."""
    init_db()
    init_db()


# ── engine hardening (guards against transient SQLite corruption) ────────────────

def test_make_engine_sets_busy_timeout(tmp_path):
    """File-based SQLite engines must wait on locks rather than erroring."""
    from db.models import _make_engine

    eng = _make_engine(f"sqlite:///{tmp_path / 'probe.db'}")
    with eng.connect() as conn:
        timeout = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
    assert timeout >= 30000


def test_make_engine_uses_nullpool_for_file_db(tmp_path):
    """A file DB must not pool connections — a stale pooled connection sees a
    'malformed' image when the file is swapped/rewritten underneath it."""
    from sqlalchemy.pool import NullPool
    from db.models import _make_engine

    eng = _make_engine(f"sqlite:///{tmp_path / 'probe.db'}")
    assert isinstance(eng.pool, NullPool)


# ── writable-path resolution (read-only deploy, e.g. Streamlit Cloud) ────────────

def test_is_writable_true_for_normal_file(tmp_path):
    from db.models import _is_writable

    f = tmp_path / "ok.db"
    f.write_bytes(b"")
    assert _is_writable(str(f)) is True


def test_is_writable_false_for_readonly_file(tmp_path):
    """A committed DB on a read-only mount must be reported non-writable."""
    import os
    import stat
    from db.models import _is_writable

    f = tmp_path / "ro.db"
    f.write_bytes(b"")
    os.chmod(f, stat.S_IRUSR)  # read-only
    try:
        # Skip if running as root, where file perms are bypassed.
        if os.access(str(f), os.W_OK):
            pytest.skip("running as root; file permissions are bypassed")
        assert _is_writable(str(f)) is False
    finally:
        os.chmod(f, stat.S_IRUSR | stat.S_IWUSR)


def test_resolve_db_url_uses_writable_repo(tmp_path):
    """A writable repo DB is used directly (the local-dev case)."""
    from db.models import _resolve_db_url

    f = tmp_path / "stablecoin.db"
    f.write_bytes(b"")
    assert _resolve_db_url(str(f)) == f"sqlite:///{f}"


def test_resolve_db_url_falls_back_and_seeds_tmp(tmp_path, monkeypatch):
    """When the repo DB isn't writable, fall back to a /tmp copy seeded from it."""
    import db.models as m

    repo_db = tmp_path / "stablecoin.db"
    repo_db.write_bytes(b"SEEDDATA")
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()

    monkeypatch.setattr(m, "_is_writable", lambda p: False)
    monkeypatch.setattr(m.tempfile, "gettempdir", lambda: str(tmpdir))

    url = m._resolve_db_url(str(repo_db))
    seeded = tmpdir / "stablecoin.db"
    assert url == f"sqlite:///{seeded}"
    assert seeded.read_bytes() == b"SEEDDATA"  # seeded from committed DB
