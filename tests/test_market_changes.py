"""Tests for the market-changes service and the /stablecoins/changes endpoint.

The autouse `in_memory_db` fixture (conftest.py) gives each test a fresh
in-memory SQLite shared by the service and the FastAPI client.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.server import app
from db.models import PriceSnapshot, RiskScore, SupplySnapshot, get_session
from services.market_changes import compute_market_changes

client = TestClient(app)

NOW = datetime(2026, 5, 21, 12, 0, 0)


# ── helpers ─────────────────────────────────────────────────────────────────────

def _add_supply(symbol: str, supply: float, when: datetime) -> None:
    with get_session() as s:
        s.add(SupplySnapshot(symbol=symbol, circulating_supply=supply, recorded_at=when))
        s.commit()


def _add_price(symbol: str, *, bps: float | None, when: datetime,
               bid: float | None = None, ask: float | None = None) -> None:
    with get_session() as s:
        s.add(PriceSnapshot(
            symbol=symbol, price=1.0, peg_deviation_bps=bps,
            bid_depth_usd=bid, ask_depth_usd=ask,
            source="binance", recorded_at=when,
        ))
        s.commit()


def _add_score(symbol: str, overall: float, when: datetime) -> None:
    with get_session() as s:
        s.add(RiskScore(
            symbol=symbol, peg_score=90.0, liquidity_score=80.0,
            reserve_score=70.0, adoption_score=60.0,
            overall_score=overall, scored_at=when,
        ))
        s.commit()


def _by_metric(changes: list[dict], metric: str) -> list[dict]:
    return [c for c in changes if c["metric"] == metric]


# ── empty / insufficient data ─────────────────────────────────────────────────────

def test_empty_db_returns_no_changes(in_memory_db):
    assert compute_market_changes() == []


def test_single_snapshot_is_skipped(in_memory_db):
    _add_supply("USDT", 1_000_000_000, NOW)
    assert compute_market_changes() == []


# ── supply ────────────────────────────────────────────────────────────────────────

def test_supply_increase_produces_change(in_memory_db):
    _add_supply("USDT", 1_000_000_000, NOW - timedelta(days=7))
    _add_supply("USDT", 1_012_000_000, NOW)

    changes = _by_metric(compute_market_changes(), "supply")
    assert len(changes) == 1
    c = changes[0]
    assert c["asset"] == "USDT"
    assert c["comparison_window"] == "7d"
    assert c["percent_change"] == pytest.approx(1.2, abs=0.05)
    assert c["absolute_change"] == pytest.approx(12_000_000)
    assert "increased 1.2% over 7 days" in c["summary"]


def test_supply_decrease_direction_and_severity(in_memory_db):
    _add_supply("DAI", 5_000_000_000, NOW - timedelta(days=7))
    _add_supply("DAI", 4_000_000_000, NOW)  # -20%

    c = _by_metric(compute_market_changes(), "supply")[0]
    assert c["percent_change"] == pytest.approx(-20.0)
    assert c["severity"] == "high"
    assert "decreased 20.0% over 7 days" in c["summary"]


def test_tiny_supply_move_is_ignored(in_memory_db):
    _add_supply("USDT", 1_000_000_000, NOW - timedelta(days=7))
    _add_supply("USDT", 1_000_100_000, NOW)  # +0.01%
    assert _by_metric(compute_market_changes(), "supply") == []


# ── peg deviation ────────────────────────────────────────────────────────────────

def test_peg_deviation_change(in_memory_db):
    _add_price("DAI", bps=8.0, when=NOW - timedelta(hours=24))
    _add_price("DAI", bps=37.0, when=NOW)

    c = _by_metric(compute_market_changes(), "peg_deviation_bps")[0]
    assert c["previous_value"] == pytest.approx(8.0)
    assert c["current_value"] == pytest.approx(37.0)
    assert c["severity"] == "medium"  # current >= 20 bps escalates
    assert "from 8 bps to 37 bps" in c["summary"]


def test_peg_break_is_high_severity(in_memory_db):
    _add_price("USDC", bps=5.0, when=NOW - timedelta(hours=24))
    _add_price("USDC", bps=60.0, when=NOW)

    c = _by_metric(compute_market_changes(), "peg_deviation_bps")[0]
    assert c["severity"] == "high"


# ── liquidity ────────────────────────────────────────────────────────────────────

def test_liquidity_drop(in_memory_db):
    _add_price("USDC", bps=1.0, when=NOW - timedelta(hours=24), bid=5_000_000, ask=5_000_000)
    _add_price("USDC", bps=1.0, when=NOW, bid=4_100_000, ask=4_100_000)  # -18%

    c = _by_metric(compute_market_changes(), "liquidity_usd")[0]
    assert c["percent_change"] == pytest.approx(-18.0, abs=0.1)
    assert c["severity"] == "medium"
    assert "fell 18% over 24 hours" in c["summary"]


# ── risk score ──────────────────────────────────────────────────────────────────

def test_score_drop(in_memory_db):
    _add_score("USDT", 86.0, NOW - timedelta(hours=24))
    _add_score("USDT", 79.0, NOW)

    c = _by_metric(compute_market_changes(), "overall_score")[0]
    assert c["absolute_change"] == pytest.approx(-7.0)
    assert c["severity"] == "medium"
    assert "fell 7 points over 24 hours" in c["summary"]


# ── ranking & limit ──────────────────────────────────────────────────────────────

def test_changes_ranked_by_severity(in_memory_db):
    # high-severity supply collapse
    _add_supply("AAA", 1_000_000_000, NOW - timedelta(days=7))
    _add_supply("AAA", 800_000_000, NOW)  # -20% -> high
    # low-severity supply nudge
    _add_supply("BBB", 1_000_000_000, NOW - timedelta(days=7))
    _add_supply("BBB", 1_030_000_000, NOW)  # +3% -> low

    changes = compute_market_changes()
    severities = [c["severity"] for c in changes]
    assert severities == sorted(
        severities, key=lambda s: {"info": 0, "low": 1, "medium": 2, "high": 3}[s], reverse=True
    )
    assert changes[0]["asset"] == "AAA"


def test_limit_is_honored(in_memory_db):
    for sym in ("AAA", "BBB", "CCC"):
        _add_supply(sym, 1_000_000_000, NOW - timedelta(days=7))
        _add_supply(sym, 1_200_000_000, NOW)
    assert len(compute_market_changes(limit=2)) == 2


def test_closest_prior_snapshot_is_used(in_memory_db):
    # Two prior points; the one nearest the 7d target should be chosen.
    _add_supply("USDT", 900_000_000, NOW - timedelta(days=14))
    _add_supply("USDT", 1_000_000_000, NOW - timedelta(days=7))
    _add_supply("USDT", 1_100_000_000, NOW)

    c = _by_metric(compute_market_changes(), "supply")[0]
    assert c["previous_value"] == pytest.approx(1_000_000_000)


# ── API endpoint ──────────────────────────────────────────────────────────────────

def test_changes_endpoint_empty_db(in_memory_db):
    resp = client.get("/stablecoins/changes")
    assert resp.status_code == 200
    assert resp.json() == []


def test_changes_endpoint_returns_structured_objects(in_memory_db):
    _add_supply("USDT", 1_000_000_000, NOW - timedelta(days=7))
    _add_supply("USDT", 1_120_000_000, NOW)

    resp = client.get("/stablecoins/changes")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    expected = {
        "asset", "metric", "previous_value", "current_value",
        "absolute_change", "percent_change", "severity",
        "comparison_window", "timestamp", "summary",
    }
    assert expected <= set(data[0].keys())


def test_changes_endpoint_limit_validation(in_memory_db):
    resp = client.get("/stablecoins/changes?limit=500")
    assert resp.status_code == 422  # exceeds le=100


def test_changes_route_not_shadowed_by_symbol_route(in_memory_db):
    # /stablecoins/changes must resolve to the changes endpoint, not the
    # {symbol} lookup (which would 404 for an unknown symbol).
    resp = client.get("/stablecoins/changes")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
