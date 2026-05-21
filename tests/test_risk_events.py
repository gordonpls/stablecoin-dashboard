"""Tests for the risk-events service and its FastAPI endpoints.

The autouse `in_memory_db` fixture (conftest.py) gives each test a fresh
in-memory SQLite shared by the service and the FastAPI client.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.server import app
from db.models import (
    ApiRequestLog, PriceSnapshot, ReserveReport, RiskScore, SupplySnapshot, get_session,
)
from services import risk_events
from services.risk_events import log_new_events, query_events

client = TestClient(app)

NOW = datetime(2026, 5, 21, 12, 0, 0)


# ── helpers ─────────────────────────────────────────────────────────────────────

def _add_price(symbol, *, bps=None, when, bid=None, ask=None):
    with get_session() as s:
        s.add(PriceSnapshot(
            symbol=symbol, price=1.0, peg_deviation_bps=bps,
            bid_depth_usd=bid, ask_depth_usd=ask, source="binance", recorded_at=when,
        ))
        s.commit()


def _add_supply(symbol, supply, when):
    with get_session() as s:
        s.add(SupplySnapshot(symbol=symbol, circulating_supply=supply, recorded_at=when))
        s.commit()


def _add_score(symbol, overall, when):
    with get_session() as s:
        s.add(RiskScore(
            symbol=symbol, peg_score=90.0, liquidity_score=80.0, reserve_score=70.0,
            adoption_score=60.0, overall_score=overall, scored_at=when,
        ))
        s.commit()


def _add_reserve(symbol, report_date, ingested_at, auditor="Deloitte"):
    with get_session() as s:
        s.add(ReserveReport(
            symbol=symbol, report_date=report_date, auditor=auditor, ingested_at=ingested_at,
        ))
        s.commit()


def _add_failed_request(provider, when, status=500):
    with get_session() as s:
        s.add(ApiRequestLog(
            provider=provider, endpoint="x", url="http://x", status_code=status,
            requested_at=when,
        ))
        s.commit()


def _by_type(events, event_type):
    return [e for e in events if e["event_type"] == event_type]


# ── empty / insufficient data ─────────────────────────────────────────────────────

def test_empty_db_logs_nothing(in_memory_db):
    assert log_new_events(now=NOW) == []
    assert query_events() == []


def test_single_snapshot_is_skipped(in_memory_db):
    _add_price("USDT", bps=40.0, when=NOW)
    assert log_new_events(now=NOW) == []


# ── peg deviation ────────────────────────────────────────────────────────────────

def test_peg_crossing_logs_event(in_memory_db):
    _add_price("DAI", bps=10.0, when=NOW - timedelta(minutes=10))
    _add_price("DAI", bps=40.0, when=NOW)

    inserted = _by_type(log_new_events(now=NOW), risk_events.PEG_DEVIATION)
    assert len(inserted) == 1
    e = inserted[0]
    assert e["symbol"] == "DAI"
    assert e["severity"] == "medium"
    assert e["previous_value"] == pytest.approx(10.0)
    assert e["current_value"] == pytest.approx(40.0)
    assert "crossed 40 bps" in e["title"]


def test_peg_break_is_high_severity(in_memory_db):
    _add_price("USDC", bps=5.0, when=NOW - timedelta(minutes=10))
    _add_price("USDC", bps=70.0, when=NOW)
    e = _by_type(log_new_events(now=NOW), risk_events.PEG_DEVIATION)[0]
    assert e["severity"] == "high"


def test_sustained_peg_stress_does_not_re_log(in_memory_db):
    # Already above threshold on both snapshots → no upward crossing → no event.
    _add_price("USDC", bps=30.0, when=NOW - timedelta(minutes=10))
    _add_price("USDC", bps=45.0, when=NOW)
    assert _by_type(log_new_events(now=NOW), risk_events.PEG_DEVIATION) == []


# ── liquidity ────────────────────────────────────────────────────────────────────

def test_liquidity_drop_logs_event(in_memory_db):
    _add_price("USDC", bps=1.0, when=NOW - timedelta(minutes=10), bid=5_000_000, ask=5_000_000)
    _add_price("USDC", bps=1.0, when=NOW, bid=3_000_000, ask=3_000_000)  # -40%

    e = _by_type(log_new_events(now=NOW), risk_events.LIQUIDITY_DROP)[0]
    assert e["severity"] == "high"
    assert e["current_value"] == pytest.approx(6_000_000)
    assert "dropped 40%" in e["title"]


def test_small_liquidity_dip_ignored(in_memory_db):
    _add_price("USDC", bps=1.0, when=NOW - timedelta(minutes=10), bid=5_000_000, ask=5_000_000)
    _add_price("USDC", bps=1.0, when=NOW, bid=4_700_000, ask=4_700_000)  # -6%
    assert _by_type(log_new_events(now=NOW), risk_events.LIQUIDITY_DROP) == []


# ── supply ────────────────────────────────────────────────────────────────────────

def test_supply_shock_logs_event(in_memory_db):
    _add_supply("USDT", 1_000_000_000, NOW - timedelta(hours=1))
    _add_supply("USDT", 1_200_000_000, NOW)  # +20%

    e = _by_type(log_new_events(now=NOW), risk_events.SUPPLY_SHOCK)[0]
    assert e["severity"] == "high"
    assert "surged 20.0%" in e["title"]


def test_supply_collision_uses_dominant_value(in_memory_db):
    # Two rows share the latest timestamp (ticker collision); the dominant
    # (larger) one must be used so the comparison tracks the same asset.
    _add_supply("USDP", 1_000_000_000, NOW - timedelta(hours=1))
    _add_supply("USDP", 50_000, NOW)            # tiny collision row
    _add_supply("USDP", 1_120_000_000, NOW)     # dominant +12%

    e = _by_type(log_new_events(now=NOW), risk_events.SUPPLY_SHOCK)[0]
    assert e["current_value"] == pytest.approx(1_120_000_000)
    assert e["severity"] == "high"


# ── score ──────────────────────────────────────────────────────────────────────

def test_score_change_logs_event(in_memory_db):
    _add_score("USDT", 86.0, NOW - timedelta(minutes=10))
    _add_score("USDT", 70.0, NOW)  # -16 points

    e = _by_type(log_new_events(now=NOW), risk_events.SCORE_CHANGE)[0]
    assert e["severity"] == "medium"
    assert "fell 16 points" in e["title"]


def test_small_score_move_ignored(in_memory_db):
    _add_score("USDT", 86.0, NOW - timedelta(minutes=10))
    _add_score("USDT", 80.0, NOW)  # -6 points
    assert _by_type(log_new_events(now=NOW), risk_events.SCORE_CHANGE) == []


# ── reserve staleness ─────────────────────────────────────────────────────────────

def test_stale_reserve_logs_event(in_memory_db):
    _add_reserve("USDT", (NOW - timedelta(days=200)).date(), ingested_at=NOW)
    e = _by_type(log_new_events(now=NOW), risk_events.RESERVE_STALE)[0]
    assert e["severity"] == "high"  # >= 180 days
    assert e["current_value"] == pytest.approx(200)


def test_fresh_reserve_logs_nothing(in_memory_db):
    _add_reserve("USDC", (NOW - timedelta(days=10)).date(), ingested_at=NOW)
    assert _by_type(log_new_events(now=NOW), risk_events.RESERVE_STALE) == []


# ── API failures ──────────────────────────────────────────────────────────────────

def test_repeated_api_failures_log_event(in_memory_db):
    for i in range(4):
        _add_failed_request("binance", when=NOW - timedelta(hours=1, minutes=i))
    e = _by_type(log_new_events(now=NOW), risk_events.API_FAILURE)[0]
    assert e["symbol"] == "SYSTEM"
    assert e["metric_name"] == "binance"
    assert e["severity"] == "medium"
    assert e["current_value"] == pytest.approx(4)


def test_few_api_failures_ignored(in_memory_db):
    for i in range(2):  # below the threshold of 3
        _add_failed_request("coinbase", when=NOW - timedelta(hours=1, minutes=i))
    assert _by_type(log_new_events(now=NOW), risk_events.API_FAILURE) == []


# ── idempotency ───────────────────────────────────────────────────────────────────

def test_detection_is_idempotent(in_memory_db):
    _add_price("DAI", bps=10.0, when=NOW - timedelta(minutes=10))
    _add_price("DAI", bps=40.0, when=NOW)

    first = log_new_events(now=NOW)
    assert len(first) == 1
    second = log_new_events(now=NOW)  # unchanged data
    assert second == []
    assert len(query_events()) == 1


# ── query filters ─────────────────────────────────────────────────────────────────

def test_query_filters(in_memory_db):
    _add_price("DAI", bps=10.0, when=NOW - timedelta(minutes=10))
    _add_price("DAI", bps=40.0, when=NOW)
    _add_score("USDT", 86.0, NOW - timedelta(minutes=10))
    _add_score("USDT", 60.0, NOW)  # -26 → high
    log_new_events(now=NOW)

    assert {e["symbol"] for e in query_events(symbol="DAI")} == {"DAI"}
    assert all(e["event_type"] == "SCORE_CHANGE"
               for e in query_events(event_type="SCORE_CHANGE"))
    assert all(e["severity"] == "high" for e in query_events(severity="high"))


def test_query_orders_newest_first(in_memory_db):
    _add_score("USDT", 86.0, NOW - timedelta(minutes=20))
    _add_score("USDT", 60.0, NOW - timedelta(minutes=10))  # event @ -10m
    log_new_events(now=NOW)
    _add_score("DAI", 90.0, NOW - timedelta(minutes=5))
    _add_score("DAI", 70.0, NOW)                            # event @ now
    log_new_events(now=NOW)

    events = query_events()
    assert [e["symbol"] for e in events] == ["DAI", "USDT"]


# ── API endpoints ──────────────────────────────────────────────────────────────────

def test_risk_events_endpoint_empty(in_memory_db):
    resp = client.get("/risk-events")
    assert resp.status_code == 200
    assert resp.json() == []


def test_risk_events_endpoint_returns_events(in_memory_db):
    _add_price("DAI", bps=10.0, when=NOW - timedelta(minutes=10))
    _add_price("DAI", bps=40.0, when=NOW)
    log_new_events(now=NOW)

    resp = client.get("/risk-events")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    expected = {
        "id", "symbol", "event_type", "severity", "title", "description",
        "metric_name", "previous_value", "current_value", "triggered_at",
    }
    assert expected <= set(data[0].keys())
    assert data[0]["event_type"] == "PEG_DEVIATION"


def test_risk_events_endpoint_filters(in_memory_db):
    _add_score("USDT", 86.0, NOW - timedelta(minutes=10))
    _add_score("USDT", 60.0, NOW)
    log_new_events(now=NOW)

    resp = client.get("/risk-events", params={"event_type": "PEG_DEVIATION"})
    assert resp.status_code == 200
    assert resp.json() == []  # only a SCORE_CHANGE exists


def test_risk_events_endpoint_limit_validation(in_memory_db):
    resp = client.get("/risk-events", params={"limit": 501})
    assert resp.status_code == 422  # exceeds le=500


def test_stablecoin_events_endpoint(in_memory_db):
    _add_price("DAI", bps=10.0, when=NOW - timedelta(minutes=10))
    _add_price("DAI", bps=40.0, when=NOW)
    _add_price("USDC", bps=5.0, when=NOW - timedelta(minutes=10))
    _add_price("USDC", bps=60.0, when=NOW)
    log_new_events(now=NOW)

    resp = client.get("/stablecoins/DAI/events")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["symbol"] == "DAI"
