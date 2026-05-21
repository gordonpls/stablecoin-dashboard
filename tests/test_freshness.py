"""Tests for the system-wide data-freshness service and /data-freshness.

The autouse `in_memory_db` fixture (conftest.py) gives each test a fresh
in-memory SQLite shared by the service and the FastAPI client.
"""

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.api.server import app
from db.models import (
    ApiRequestLog,
    PriceSnapshot,
    ReserveReport,
    RiskScore,
    SupplySnapshot,
    get_session,
)
from services.freshness import classify, compute_data_freshness

client = TestClient(app)

NOW = datetime.utcnow()


# ── helpers ─────────────────────────────────────────────────────────────────────

def _add_price(symbol, *, when=NOW, bid=3_000_000, ask=2_000_000, source="binance"):
    with get_session() as s:
        s.add(PriceSnapshot(
            symbol=symbol, price=1.0, peg_deviation_bps=1.0,
            bid_depth_usd=bid, ask_depth_usd=ask, source=source, recorded_at=when,
        ))
        s.commit()


def _add_score(symbol, *, when=NOW):
    with get_session() as s:
        s.add(RiskScore(
            symbol=symbol, peg_score=90.0, liquidity_score=80.0,
            reserve_score=75.0, adoption_score=70.0, overall_score=82.0, scored_at=when,
        ))
        s.commit()


def _add_supply(symbol, *, when=NOW, supply=1_000_000_000):
    with get_session() as s:
        s.add(SupplySnapshot(symbol=symbol, circulating_supply=supply, recorded_at=when))
        s.commit()


def _add_reserve(symbol, *, when=NOW):
    with get_session() as s:
        s.add(ReserveReport(symbol=symbol, auditor="BDO", ingested_at=when))
        s.commit()


def _add_log(provider, *, status=200, when=NOW, endpoint="ep", url="http://x"):
    with get_session() as s:
        s.add(ApiRequestLog(
            provider=provider, endpoint=endpoint, url=url,
            status_code=status, requested_at=when,
        ))
        s.commit()


def _source(result, key):
    return next(s for s in result["sources"] if s["source"] == key)


# ── classification unit ──────────────────────────────────────────────────────────

def test_classify_bands():
    assert classify(None, 600) == "missing"
    assert classify(60, 600) == "fresh"
    assert classify(600, 600) == "fresh"
    assert classify(900, 600) == "delayed"
    assert classify(1200, 600) == "delayed"
    assert classify(5000, 600) == "stale"


# ── empty database ────────────────────────────────────────────────────────────────

def test_empty_db_all_missing():
    result = compute_data_freshness()
    assert {"generated_at", "overall_status", "sources", "providers"} <= set(result)
    assert len(result["sources"]) == 5
    assert all(s["status"] == "missing" for s in result["sources"])
    assert all(s["last_updated"] is None for s in result["sources"])
    assert all(s["assets_covered"] == 0 for s in result["sources"])
    assert result["overall_status"] == "missing"
    assert result["providers"] == []


# ── per-source classification ──────────────────────────────────────────────────────

def test_source_status_classification():
    _add_price("USDT", when=NOW - timedelta(seconds=120))            # fresh (cadence 600)
    _add_score("USDT", when=NOW - timedelta(hours=5))                # stale (cadence 600)
    _add_supply("USDT", when=NOW - timedelta(hours=1, minutes=30))   # delayed (cadence 3600)

    result = compute_data_freshness()
    assert _source(result, "prices")["status"] == "fresh"
    assert _source(result, "scores")["status"] == "stale"
    assert _source(result, "supply")["status"] == "delayed"
    assert _source(result, "reserves")["status"] == "missing"


def test_age_and_cadence_reported():
    _add_price("USDT", when=NOW - timedelta(seconds=300))
    prices = _source(compute_data_freshness(), "prices")
    assert prices["expected_cadence_seconds"] == 600
    assert 250 <= prices["age_seconds"] <= 360
    assert prices["last_updated"] is not None


# ── assets covered ──────────────────────────────────────────────────────────────────

def test_assets_covered_counts_distinct_symbols():
    _add_price("USDT")
    _add_price("USDC")
    _add_price("USDT")  # duplicate symbol should not double-count
    assert _source(compute_data_freshness(), "prices")["assets_covered"] == 2


def test_assets_covered_excludes_old_rows():
    _add_supply("USDT", when=NOW)
    _add_supply("USDC", when=NOW - timedelta(hours=10))  # older than 2× cadence
    assert _source(compute_data_freshness(), "supply")["assets_covered"] == 1


# ── liquidity requires depth ────────────────────────────────────────────────────────

def test_liquidity_ignores_price_only_rows():
    # A price snapshot with no depth keeps prices fresh but leaves liquidity missing.
    _add_price("USDT", bid=None, ask=None)
    result = compute_data_freshness()
    assert _source(result, "prices")["status"] == "fresh"
    assert _source(result, "liquidity")["status"] == "missing"
    assert _source(result, "liquidity")["assets_covered"] == 0


def test_liquidity_fresh_when_depth_present():
    _add_price("USDT", bid=1_000_000, ask=None)
    assert _source(compute_data_freshness(), "liquidity")["status"] == "fresh"


# ── provider health ──────────────────────────────────────────────────────────────────

def test_provider_health_healthy_failing_missing():
    _add_log("binance", status=200, when=NOW - timedelta(minutes=2))
    _add_log("coinbase", status=200, when=NOW - timedelta(minutes=10))
    _add_log("coinbase", status=500, when=NOW)  # most recent errored → failing
    _add_log("defillama", status=None, when=NOW)  # no response → failing

    providers = {p["provider"]: p for p in compute_data_freshness()["providers"]}
    assert providers["binance"]["status"] == "healthy"
    assert providers["coinbase"]["status"] == "failing"
    assert providers["coinbase"]["error_count"] == 1
    assert providers["coinbase"]["total_requests"] == 2
    assert providers["defillama"]["status"] == "failing"


def test_provider_latest_success_after_error_is_healthy():
    _add_log("binance", status=500, when=NOW - timedelta(minutes=5))
    _add_log("binance", status=200, when=NOW)  # recovered
    providers = {p["provider"]: p for p in compute_data_freshness()["providers"]}
    assert providers["binance"]["status"] == "healthy"
    assert providers["binance"]["error_count"] == 1


# ── overall status ────────────────────────────────────────────────────────────────────

def test_overall_status_is_worst_source():
    # All five sources present; one is stale → overall is stale.
    _add_price("USDT", when=NOW)            # prices fresh + liquidity fresh
    _add_supply("USDT", when=NOW)           # supply fresh
    _add_reserve("USDT", when=NOW)          # reserves fresh
    _add_score("USDT", when=NOW - timedelta(hours=5))  # scores stale
    result = compute_data_freshness()
    assert _source(result, "scores")["status"] == "stale"
    assert result["overall_status"] == "stale"


def test_overall_status_fresh_when_all_fresh():
    _add_price("USDT", when=NOW)
    _add_supply("USDT", when=NOW)
    _add_reserve("USDT", when=NOW)
    _add_score("USDT", when=NOW)
    assert compute_data_freshness()["overall_status"] == "fresh"


# ── API endpoint ──────────────────────────────────────────────────────────────────────

def test_endpoint_returns_structure_on_empty_db():
    resp = client.get("/data-freshness")
    assert resp.status_code == 200
    data = resp.json()
    assert {"generated_at", "overall_status", "sources", "providers"} <= set(data)
    assert len(data["sources"]) == 5


def test_endpoint_reflects_data():
    _add_price("USDT", when=NOW)
    _add_log("binance", status=200, when=NOW)
    resp = client.get("/data-freshness")
    assert resp.status_code == 200
    data = resp.json()
    prices = next(s for s in data["sources"] if s["source"] == "prices")
    assert prices["status"] == "fresh"
    assert prices["assets_covered"] == 1
    assert data["providers"][0]["provider"] == "binance"
