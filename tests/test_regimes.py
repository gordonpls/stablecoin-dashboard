"""Tests for the risk-regime service, its REGIME_CHANGE events, and endpoints.

The autouse `in_memory_db` fixture (conftest.py) gives each test a fresh
in-memory SQLite shared by the services and the FastAPI client.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.server import app
from db.models import (
    DataQualityWarning, PriceSnapshot, RegimeSnapshot, RiskScore, get_session,
)
from services import regimes
from services.regimes import (
    DATA_QUALITY_CONCERN,
    HIGH_RISK,
    LIQUIDITY_STRESS,
    MILD_STRESS,
    PEG_STRESS,
    STABLE,
    classify_regime,
    current_regimes,
    get_regime,
    get_regime_detail,
    record_regimes,
    regime_history,
)
from services.risk_events import REGIME_CHANGE, log_new_events, query_events

client = TestClient(app)

NOW = datetime(2026, 5, 21, 12, 0, 0)


# ── helpers ─────────────────────────────────────────────────────────────────────

def _add_score(symbol, *, overall, liquidity=90.0, when):
    with get_session() as s:
        s.add(RiskScore(
            symbol=symbol, peg_score=90.0, liquidity_score=liquidity,
            reserve_score=80.0, adoption_score=70.0, overall_score=overall,
            scored_at=when,
        ))
        s.commit()


def _add_price(symbol, *, bps, when):
    with get_session() as s:
        s.add(PriceSnapshot(
            symbol=symbol, price=1.0, peg_deviation_bps=bps,
            source="binance", recorded_at=when,
        ))
        s.commit()


def _add_dq_warning(symbol, *, severity="high", when=NOW, resolved=False):
    with get_session() as s:
        s.add(DataQualityWarning(
            symbol=symbol, metric_name="price", warning_type="IMPOSSIBLE_PRICE",
            severity=severity, message="x", detected_at=when,
            resolved_at=(when if resolved else None),
        ))
        s.commit()


# ── classify_regime (pure function) ──────────────────────────────────────────────

def test_classify_stable():
    r = classify_regime(overall_score=90.0, peg_bps=5.0, liquidity_score=90.0, dq_concern=False)
    assert r["regime"] == STABLE
    assert r["severity"] == "low"


def test_classify_high_risk_on_peg_break():
    r = classify_regime(overall_score=90.0, peg_bps=60.0, liquidity_score=90.0, dq_concern=False)
    assert r["regime"] == HIGH_RISK
    assert r["severity"] == "high"


def test_classify_high_risk_on_low_score():
    r = classify_regime(overall_score=55.0, peg_bps=5.0, liquidity_score=90.0, dq_concern=False)
    assert r["regime"] == HIGH_RISK


def test_classify_peg_stress():
    # Peg in the 25–50 bps band, score healthy → Peg stress (not High risk).
    r = classify_regime(overall_score=85.0, peg_bps=30.0, liquidity_score=90.0, dq_concern=False)
    assert r["regime"] == PEG_STRESS


def test_classify_liquidity_stress():
    r = classify_regime(overall_score=85.0, peg_bps=5.0, liquidity_score=10.0, dq_concern=False)
    assert r["regime"] == LIQUIDITY_STRESS


def test_classify_data_quality_concern():
    # Everything looks fine on the surface, but a warning is open → trust limited.
    r = classify_regime(overall_score=95.0, peg_bps=3.0, liquidity_score=95.0, dq_concern=True)
    assert r["regime"] == DATA_QUALITY_CONCERN


def test_classify_mild_stress_from_score():
    r = classify_regime(overall_score=70.0, peg_bps=5.0, liquidity_score=90.0, dq_concern=False)
    assert r["regime"] == MILD_STRESS


def test_classify_mild_stress_from_peg_drift():
    r = classify_regime(overall_score=90.0, peg_bps=15.0, liquidity_score=90.0, dq_concern=False)
    assert r["regime"] == MILD_STRESS


def test_high_risk_dominates_data_quality_concern():
    # A real de-peg must not be masked as a mere data-quality concern.
    r = classify_regime(overall_score=90.0, peg_bps=80.0, liquidity_score=90.0, dq_concern=True)
    assert r["regime"] == HIGH_RISK


def test_classify_stable_with_no_peg_reading():
    r = classify_regime(overall_score=90.0, peg_bps=None, liquidity_score=90.0, dq_concern=False)
    assert r["regime"] == STABLE
    assert "no recent peg reading" in r["reason"]


# ── record_regimes ────────────────────────────────────────────────────────────────

def test_empty_db_records_nothing(in_memory_db):
    assert record_regimes(now=NOW) == []
    assert current_regimes() == []


def test_first_classification_is_recorded(in_memory_db):
    _add_score("USDT", overall=90.0, when=NOW)
    _add_price("USDT", bps=5.0, when=NOW)

    inserted = record_regimes(now=NOW)
    assert len(inserted) == 1
    assert inserted[0]["symbol"] == "USDT"
    assert inserted[0]["regime"] == STABLE
    assert inserted[0]["from_regime"] is None  # initial label, not a transition


def test_record_is_idempotent_when_unchanged(in_memory_db):
    _add_score("USDT", overall=90.0, when=NOW)
    _add_price("USDT", bps=5.0, when=NOW)

    assert len(record_regimes(now=NOW)) == 1
    assert record_regimes(now=NOW + timedelta(minutes=10)) == []  # same regime → no row
    assert len(regime_history("USDT")) == 1


def test_regime_transition_is_recorded(in_memory_db):
    _add_score("USDT", overall=90.0, when=NOW)
    _add_price("USDT", bps=5.0, when=NOW)
    record_regimes(now=NOW)  # Stable

    # Newer, worse data → High risk.
    _add_score("USDT", overall=50.0, when=NOW + timedelta(minutes=10))
    _add_price("USDT", bps=5.0, when=NOW + timedelta(minutes=10))
    inserted = record_regimes(now=NOW + timedelta(minutes=10))

    assert len(inserted) == 1
    assert inserted[0]["regime"] == HIGH_RISK
    assert inserted[0]["from_regime"] == STABLE
    assert len(regime_history("USDT")) == 2


def test_record_uses_latest_peg(in_memory_db):
    # Score is healthy but the latest peg is broken → High risk.
    _add_score("DAI", overall=90.0, when=NOW)
    _add_price("DAI", bps=5.0, when=NOW - timedelta(minutes=20))
    _add_price("DAI", bps=70.0, when=NOW)  # newest

    inserted = record_regimes(now=NOW)
    assert inserted[0]["regime"] == HIGH_RISK


def test_open_data_quality_warning_drives_regime(in_memory_db):
    _add_score("USDC", overall=95.0, liquidity=95.0, when=NOW)
    _add_price("USDC", bps=3.0, when=NOW)
    _add_dq_warning("USDC", severity="high", when=NOW)

    inserted = record_regimes(now=NOW)
    assert inserted[0]["regime"] == DATA_QUALITY_CONCERN


def test_resolved_warning_does_not_drive_regime(in_memory_db):
    _add_score("USDC", overall=95.0, liquidity=95.0, when=NOW)
    _add_price("USDC", bps=3.0, when=NOW)
    _add_dq_warning("USDC", severity="high", when=NOW, resolved=True)

    inserted = record_regimes(now=NOW)
    assert inserted[0]["regime"] == STABLE


# ── queries ─────────────────────────────────────────────────────────────────────

def test_current_regimes_sorted_most_severe_first(in_memory_db):
    _add_score("AAA", overall=90.0, when=NOW)   # Stable
    _add_price("AAA", bps=5.0, when=NOW)
    _add_score("BBB", overall=50.0, when=NOW)   # High risk
    _add_price("BBB", bps=5.0, when=NOW)
    record_regimes(now=NOW)

    order = [r["symbol"] for r in current_regimes()]
    assert order == ["BBB", "AAA"]  # High risk before Stable


def test_get_regime_returns_latest(in_memory_db):
    _add_score("USDT", overall=90.0, when=NOW)
    _add_price("USDT", bps=5.0, when=NOW)
    record_regimes(now=NOW)
    _add_score("USDT", overall=50.0, when=NOW + timedelta(minutes=10))
    _add_price("USDT", bps=5.0, when=NOW + timedelta(minutes=10))
    record_regimes(now=NOW + timedelta(minutes=10))

    assert get_regime("usdt")["regime"] == HIGH_RISK
    assert get_regime("NOPE") is None


def test_get_regime_detail_shape(in_memory_db):
    _add_score("USDT", overall=90.0, when=NOW)
    _add_price("USDT", bps=5.0, when=NOW)
    record_regimes(now=NOW)

    detail = get_regime_detail("USDT")
    assert detail["symbol"] == "USDT"
    assert detail["current"]["regime"] == STABLE
    assert len(detail["history"]) == 1


# ── REGIME_CHANGE risk events ─────────────────────────────────────────────────────

def test_transition_emits_regime_change_event(in_memory_db):
    _add_score("USDT", overall=90.0, when=NOW)
    _add_price("USDT", bps=5.0, when=NOW)
    record_regimes(now=NOW)
    assert [e for e in log_new_events(now=NOW) if e["event_type"] == REGIME_CHANGE] == []

    _add_score("USDT", overall=50.0, when=NOW + timedelta(minutes=10))
    _add_price("USDT", bps=5.0, when=NOW + timedelta(minutes=10))
    record_regimes(now=NOW + timedelta(minutes=10))

    events = [e for e in log_new_events(now=NOW + timedelta(minutes=10))
              if e["event_type"] == REGIME_CHANGE]
    assert len(events) == 1
    e = events[0]
    assert e["symbol"] == "USDT"
    assert e["severity"] == "high"
    assert "deteriorated to High risk" in e["title"]
    assert e["previous_value"] == pytest.approx(regimes.REGIME_RANK[STABLE])
    assert e["current_value"] == pytest.approx(regimes.REGIME_RANK[HIGH_RISK])


def test_regime_change_event_is_idempotent(in_memory_db):
    _add_score("USDT", overall=90.0, when=NOW)
    _add_price("USDT", bps=5.0, when=NOW)
    record_regimes(now=NOW)
    _add_score("USDT", overall=50.0, when=NOW + timedelta(minutes=10))
    _add_price("USDT", bps=5.0, when=NOW + timedelta(minutes=10))
    record_regimes(now=NOW + timedelta(minutes=10))

    first = [e for e in log_new_events(now=NOW + timedelta(minutes=10))
             if e["event_type"] == REGIME_CHANGE]
    assert len(first) == 1
    second = [e for e in log_new_events(now=NOW + timedelta(minutes=10))
              if e["event_type"] == REGIME_CHANGE]
    assert second == []
    assert len(query_events(event_type=REGIME_CHANGE)) == 1


def test_improvement_emits_low_severity_event(in_memory_db):
    _add_score("DAI", overall=50.0, when=NOW)   # High risk
    _add_price("DAI", bps=5.0, when=NOW)
    record_regimes(now=NOW)
    _add_score("DAI", overall=90.0, when=NOW + timedelta(minutes=10))  # Stable
    _add_price("DAI", bps=5.0, when=NOW + timedelta(minutes=10))
    record_regimes(now=NOW + timedelta(minutes=10))

    e = [e for e in log_new_events(now=NOW + timedelta(minutes=10))
         if e["event_type"] == REGIME_CHANGE][0]
    assert e["severity"] == "low"
    assert "improved to Stable" in e["title"]


# ── API endpoints ──────────────────────────────────────────────────────────────────

def test_regimes_endpoint_empty(in_memory_db):
    resp = client.get("/regimes")
    assert resp.status_code == 200
    assert resp.json() == []


def test_regimes_endpoint_returns_current(in_memory_db):
    _add_score("USDT", overall=90.0, when=NOW)
    _add_price("USDT", bps=5.0, when=NOW)
    record_regimes(now=NOW)

    resp = client.get("/regimes")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["symbol"] == "USDT"
    assert data[0]["regime"] == STABLE


def test_symbol_regime_endpoint(in_memory_db):
    _add_score("USDT", overall=90.0, when=NOW)
    _add_price("USDT", bps=5.0, when=NOW)
    record_regimes(now=NOW)

    resp = client.get("/stablecoins/usdt/regime")
    assert resp.status_code == 200
    data = resp.json()
    assert data["symbol"] == "USDT"
    assert data["current"]["regime"] == STABLE
    assert len(data["history"]) == 1


def test_symbol_regime_endpoint_unknown_is_empty(in_memory_db):
    resp = client.get("/stablecoins/NOPE/regime")
    assert resp.status_code == 200
    data = resp.json()
    assert data["current"] is None
    assert data["history"] == []


def test_symbol_regime_endpoint_limit_validation(in_memory_db):
    resp = client.get("/stablecoins/USDT/regime", params={"history_limit": 501})
    assert resp.status_code == 422  # exceeds le=500
