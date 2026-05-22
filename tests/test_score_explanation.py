"""Tests for the explainable score-drilldown service and
/stablecoins/{symbol}/score-explanation.

The autouse `in_memory_db` fixture (conftest.py) gives each test a fresh
in-memory SQLite shared by the service and the FastAPI client.
"""

import json
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.server import app
from db.models import (
    PriceSnapshot,
    ReserveReport,
    RiskScore,
    SupplySnapshot,
    get_session,
)
from pipelines.score_stablecoins import SCORE_WEIGHTS
from services.score_explanation import explain_scores

client = TestClient(app)

NOW = datetime.utcnow()


# ── helpers ─────────────────────────────────────────────────────────────────────

def _add_score(symbol="USDT", *, peg=95.0, liquidity=80.0, reserve=75.0,
               adoption=70.0, overall=85.0, when=NOW):
    with get_session() as s:
        s.add(RiskScore(
            symbol=symbol, peg_score=peg, liquidity_score=liquidity,
            reserve_score=reserve, adoption_score=adoption,
            overall_score=overall, scored_at=when,
        ))
        s.commit()


def _add_price(symbol="USDT", *, price=1.0001, bps=1.0, bid=3_000_000,
               ask=2_000_000, source="binance", when=NOW):
    with get_session() as s:
        s.add(PriceSnapshot(
            symbol=symbol, price=price, peg_deviation_bps=bps,
            bid_depth_usd=bid, ask_depth_usd=ask, source=source, recorded_at=when,
        ))
        s.commit()


def _add_supply(symbol="USDT", supply=100_000_000_000, when=NOW, chains=None):
    with get_session() as s:
        s.add(SupplySnapshot(
            symbol=symbol, circulating_supply=supply,
            supply_by_chain=json.dumps(chains) if chains is not None else None,
            recorded_at=when,
        ))
        s.commit()


def _add_reserve(symbol="USDT", *, report_date=date(2026, 4, 1), auditor="BDO"):
    with get_session() as s:
        s.add(ReserveReport(
            symbol=symbol, report_url="https://example.com",
            report_date=report_date, composition=None, auditor=auditor,
        ))
        s.commit()


def _component(explanation, key):
    return next(c for c in explanation["components"] if c["key"] == key)


# ── missing data ──────────────────────────────────────────────────────────────────

def test_no_score_returns_none(in_memory_db):
    assert explain_scores("NOPE") is None


def test_no_score_endpoint_404(in_memory_db):
    resp = client.get("/stablecoins/NOPE/score-explanation")
    assert resp.status_code == 404


# ── structure ───────────────────────────────────────────────────────────────────

def test_structure_and_weights(in_memory_db):
    _add_score("USDT", overall=85.0)
    e = explain_scores("USDT")
    assert e["symbol"] == "USDT"
    assert e["overall_score"] == pytest.approx(85.0)
    assert e["risk_label"] == "Strong"
    assert e["weights"] == SCORE_WEIGHTS
    assert {c["key"] for c in e["components"]} == {"peg", "liquidity", "reserve", "adoption"}


def test_case_insensitive(in_memory_db):
    _add_score("USDT")
    assert explain_scores("usdt")["symbol"] == "USDT"


# ── per-dimension math ────────────────────────────────────────────────────────────

def test_contribution_and_points_lost_math(in_memory_db):
    _add_score("USDT", peg=80.0, liquidity=60.0, reserve=40.0, adoption=20.0)
    e = explain_scores("USDT")
    peg = _component(e, "peg")
    assert peg["weight"] == pytest.approx(0.35)
    assert peg["weighted_contribution"] == pytest.approx(80.0 * 0.35)
    assert peg["points_lost"] == pytest.approx((100.0 - 80.0) * 0.35)
    res = _component(e, "reserve")
    assert res["points_lost"] == pytest.approx((100.0 - 40.0) * 0.25)


def test_weakest_component_is_largest_drag(in_memory_db):
    # points_lost: peg 1.75, liquidity 5.0, reserve 15.0, adoption 4.5 → reserve.
    _add_score("USDT", peg=95.0, liquidity=80.0, reserve=40.0, adoption=70.0)
    e = explain_scores("USDT")
    assert e["weakest_component"] == "reserve"
    assert "Reserve Quality" in e["weakest_explanation"]


def test_perfect_scores_have_no_weakest(in_memory_db):
    _add_score("USDT", peg=100.0, liquidity=100.0, reserve=100.0,
               adoption=100.0, overall=100.0)
    e = explain_scores("USDT")
    assert e["weakest_component"] is None
    assert "perfect" in e["weakest_explanation"].lower()


# ── inputs reflect latest snapshots ────────────────────────────────────────────────

def test_inputs_pulled_from_latest_snapshots(in_memory_db):
    _add_score("USDT")
    _add_price("USDT", bps=12.5, bid=4_000_000, ask=1_000_000)
    _add_supply("USDT", 90_000_000_000)
    _add_reserve("USDT", report_date=NOW.date() - timedelta(days=10), auditor="BDO")
    e = explain_scores("USDT")
    assert _component(e, "peg")["inputs"]["peg_deviation_bps"] == pytest.approx(12.5)
    liq = _component(e, "liquidity")["inputs"]
    assert liq["total_depth_usd"] == pytest.approx(5_000_000)
    assert _component(e, "adoption")["inputs"]["circulating_supply"] == pytest.approx(90_000_000_000)
    res = _component(e, "reserve")["inputs"]
    assert res["age_days"] == 10
    assert res["auditor"] == "BDO"


def test_missing_inputs_are_explicit(in_memory_db):
    # A score with no supporting snapshots: inputs are None, details say so.
    _add_score("USDT")
    e = explain_scores("USDT")
    assert _component(e, "peg")["inputs"]["peg_deviation_bps"] is None
    assert _component(e, "adoption")["inputs"]["circulating_supply"] is None
    assert "neutral" in _component(e, "peg")["detail"].lower()


def test_supply_collision_takes_dominant_row(in_memory_db):
    _add_score("USDX")
    _add_supply("USDX", 50_000_000, when=NOW)
    _add_supply("USDX", 900_000_000, when=NOW)
    e = explain_scores("USDX")
    assert _component(e, "adoption")["inputs"]["circulating_supply"] == pytest.approx(900_000_000)


# ── delta narrative ────────────────────────────────────────────────────────────────

def test_delta_unavailable_with_single_snapshot(in_memory_db):
    _add_score("USDT")
    d = explain_scores("USDT")["delta"]
    assert d["available"] is False
    assert "no prior score" in d["summary"]


def test_delta_explains_decline(in_memory_db):
    _add_score("USDT", peg=98.0, liquidity=90.0, reserve=75.0, adoption=70.0,
               overall=86.0, when=NOW - timedelta(hours=1))
    _add_score("USDT", peg=95.0, liquidity=80.0, reserve=75.0, adoption=70.0,
               overall=79.0, when=NOW)
    d = explain_scores("USDT")["delta"]
    assert d["available"] is True
    assert d["overall_change"] == pytest.approx(-7.0)
    # Liquidity moved most (-10), then peg (-3); reserve/adoption unchanged.
    assert "fell from 86 to 79" in d["summary"]
    assert d["summary"].index("liquidity depth fell") < d["summary"].index("peg deviation widened")
    assert "reserve" not in d["summary"]
    assert d["components"]["liquidity"] == pytest.approx(-10.0)


def test_delta_explains_improvement(in_memory_db):
    _add_score("USDT", peg=90.0, overall=80.0, when=NOW - timedelta(hours=1))
    _add_score("USDT", peg=99.0, overall=83.0, when=NOW)
    d = explain_scores("USDT")["delta"]
    assert "rose from 80 to 83" in d["summary"]
    assert "peg deviation narrowed" in d["summary"]


def test_delta_held_steady(in_memory_db):
    _add_score("USDT", overall=85.0, when=NOW - timedelta(hours=1))
    _add_score("USDT", overall=85.0, when=NOW)
    d = explain_scores("USDT")["delta"]
    assert "held steady at 85" in d["summary"]


# ── API endpoint ──────────────────────────────────────────────────────────────────

def test_endpoint_returns_structure(in_memory_db):
    _add_score("USDT", peg=95.0, liquidity=80.0, reserve=40.0, adoption=70.0)
    _add_price("USDT", bps=2.0)
    resp = client.get("/stablecoins/USDT/score-explanation")
    assert resp.status_code == 200
    data = resp.json()
    assert data["symbol"] == "USDT"
    assert data["weakest_component"] == "reserve"
    assert {"components", "weights", "delta", "weakest_explanation"} <= set(data)
    assert len(data["components"]) == 4


def test_endpoint_case_insensitive(in_memory_db):
    _add_score("USDT")
    resp = client.get("/stablecoins/usdt/score-explanation")
    assert resp.status_code == 200
    assert resp.json()["symbol"] == "USDT"


def test_endpoint_not_shadowed_by_symbol_route(in_memory_db):
    # /stablecoins/{symbol}/score-explanation must resolve to this endpoint.
    _add_score("USDT")
    resp = client.get("/stablecoins/USDT/score-explanation")
    assert resp.status_code == 200
    assert "components" in resp.json()
