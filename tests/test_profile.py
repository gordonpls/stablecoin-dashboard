"""Tests for the stablecoin-profile service and /stablecoins/{symbol}/profile.

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
    Stablecoin,
    SupplySnapshot,
    get_session,
)
from services.profile import get_stablecoin_profile

client = TestClient(app)

NOW = datetime.utcnow()


# ── helpers ─────────────────────────────────────────────────────────────────────

def _add_stablecoin(symbol="USDT", name="Tether", issuer="Tether Ltd", peg="fiat-backed"):
    with get_session() as s:
        s.add(Stablecoin(id=symbol.lower(), symbol=symbol, name=name,
                         issuer=issuer, peg_mechanism=peg))
        s.commit()


def _add_supply(symbol, supply, when=NOW, chains=None):
    with get_session() as s:
        s.add(SupplySnapshot(
            symbol=symbol, circulating_supply=supply,
            supply_by_chain=json.dumps(chains) if chains is not None else None,
            recorded_at=when,
        ))
        s.commit()


def _add_price(symbol, *, price=1.0001, bps=1.0, bid=3_000_000, ask=2_000_000,
               source="binance", when=NOW):
    with get_session() as s:
        s.add(PriceSnapshot(
            symbol=symbol, price=price, peg_deviation_bps=bps,
            bid_depth_usd=bid, ask_depth_usd=ask, source=source, recorded_at=when,
        ))
        s.commit()


def _add_score(symbol, overall=85.0, when=NOW):
    with get_session() as s:
        s.add(RiskScore(
            symbol=symbol, peg_score=95.0, liquidity_score=80.0,
            reserve_score=75.0, adoption_score=70.0,
            overall_score=overall, scored_at=when,
        ))
        s.commit()


def _add_reserve(symbol, *, report_date=date(2025, 4, 1), auditor="BDO",
                 composition=None, url="https://example.com"):
    with get_session() as s:
        s.add(ReserveReport(
            symbol=symbol, report_url=url, report_date=report_date,
            composition=json.dumps(composition) if composition is not None else None,
            auditor=auditor,
        ))
        s.commit()


CHAINS = {
    "Ethereum": {"current": {"peggedUSD": 60_000_000_000}},
    "Tron":     {"current": {"peggedUSD": 40_000_000_000}},
}


# ── unknown / empty ───────────────────────────────────────────────────────────────

def test_unknown_symbol_returns_none(in_memory_db):
    assert get_stablecoin_profile("NOPE") is None


def test_registered_but_no_data(in_memory_db):
    _add_stablecoin("USDT")
    p = get_stablecoin_profile("USDT")
    assert p is not None
    assert p["registered"] is True
    assert p["name"] == "Tether"
    assert p["price"] is None
    assert p["supply"] is None
    assert p["scores"] is None
    assert p["reserve"] is None
    assert p["freshness"]["price"]["status"] == "missing"


def test_data_without_registry_row_still_returns_profile(in_memory_db):
    # An asset can have snapshots before it is registered in the Stablecoin table.
    _add_price("USDT")
    p = get_stablecoin_profile("USDT")
    assert p is not None
    assert p["registered"] is False
    assert p["name"] is None
    assert p["price"] is not None


# ── case-insensitivity ───────────────────────────────────────────────────────────

def test_symbol_lookup_is_case_insensitive(in_memory_db):
    _add_stablecoin("USDT")
    assert get_stablecoin_profile("usdt")["symbol"] == "USDT"


# ── price + liquidity ────────────────────────────────────────────────────────────

def test_price_and_liquidity_populated(in_memory_db):
    _add_price("USDT", price=0.9998, bps=2.0, bid=3_000_000, ask=2_000_000)
    pr = get_stablecoin_profile("USDT")["price"]
    assert pr["price"] == pytest.approx(0.9998)
    assert pr["peg_deviation_bps"] == pytest.approx(2.0)
    assert pr["total_depth_usd"] == pytest.approx(5_000_000)
    assert pr["source"] == "binance"


def test_latest_price_wins(in_memory_db):
    _add_price("USDT", price=1.0, when=NOW - timedelta(hours=2))
    _add_price("USDT", price=0.95, when=NOW)
    assert get_stablecoin_profile("USDT")["price"]["price"] == pytest.approx(0.95)


# ── supply + chain breakdown ──────────────────────────────────────────────────────

def test_supply_and_chain_breakdown(in_memory_db):
    _add_supply("USDT", 100_000_000_000, chains=CHAINS)
    sup = get_stablecoin_profile("USDT")["supply"]
    assert sup["circulating_supply"] == pytest.approx(100_000_000_000)
    assert sup["top_chain"] == "Ethereum"
    assert sup["top_chain_pct"] == pytest.approx(60.0)
    assert len(sup["chains"]) == 2
    assert sup["chains"][0]["chain"] == "Ethereum"
    assert sup["chains"][1]["supply_pct"] == pytest.approx(40.0)


def test_supply_missing_chain_data(in_memory_db):
    _add_supply("USDT", 1_000_000_000, chains=None)
    sup = get_stablecoin_profile("USDT")["supply"]
    assert sup["chains"] == []
    assert sup["top_chain"] is None


def test_supply_collision_takes_dominant_row(in_memory_db):
    # Two assets share a ticker at the same timestamp; the dominant one wins.
    _add_supply("USDX", 50_000_000, when=NOW)
    _add_supply("USDX", 900_000_000, when=NOW)
    assert get_stablecoin_profile("USDX")["supply"]["circulating_supply"] == pytest.approx(900_000_000)


# ── scores ───────────────────────────────────────────────────────────────────────

def test_scores_populated_with_risk_label(in_memory_db):
    _add_score("USDT", overall=85.0)
    sc = get_stablecoin_profile("USDT")["scores"]
    assert sc["overall_score"] == pytest.approx(85.0)
    assert sc["risk_label"] == "Strong"
    assert {"peg_score", "liquidity_score", "reserve_score", "adoption_score"} <= set(sc)


def test_latest_score_wins(in_memory_db):
    _add_score("USDT", overall=85.0, when=NOW - timedelta(hours=1))
    _add_score("USDT", overall=55.0, when=NOW)
    sc = get_stablecoin_profile("USDT")["scores"]
    assert sc["overall_score"] == pytest.approx(55.0)
    assert sc["risk_label"] == "Constrained"


# ── reserves ─────────────────────────────────────────────────────────────────────

def test_reserve_composition_and_freshness(in_memory_db):
    _add_reserve("USDT", report_date=NOW.date() - timedelta(days=10),
                 composition={"US_Treasuries": 0.84, "cash": 0.16})
    res = get_stablecoin_profile("USDT")["reserve"]
    assert res["composition"]["US_Treasuries"] == pytest.approx(0.84)
    assert res["auditor"] == "BDO"
    assert res["age_days"] == 10
    assert res["is_stale"] is False


def test_reserve_stale_when_old(in_memory_db):
    _add_reserve("USDT", report_date=NOW.date() - timedelta(days=200))
    res = get_stablecoin_profile("USDT")["reserve"]
    assert res["is_stale"] is True


def test_reserve_without_date(in_memory_db):
    _add_reserve("DAI", report_date=None, auditor=None,
                 composition={"USDC": 0.3, "ETH": 0.7})
    res = get_stablecoin_profile("DAI")["reserve"]
    assert res["report_date"] is None
    assert res["age_days"] is None
    assert res["is_stale"] is None


# ── freshness ────────────────────────────────────────────────────────────────────

def test_freshness_statuses(in_memory_db):
    # price cadence is 600s: fresh within 600s, delayed within 1200s, stale beyond.
    _add_price("USDT", when=NOW - timedelta(seconds=120))   # fresh
    _add_supply("USDT", 1e9, when=NOW - timedelta(hours=1, minutes=30))  # delayed (cadence 3600)
    _add_score("USDT", when=NOW - timedelta(hours=5))       # stale
    fr = get_stablecoin_profile("USDT")["freshness"]
    assert fr["price"]["status"] == "fresh"
    assert fr["supply"]["status"] == "delayed"
    assert fr["scores"]["status"] == "stale"
    assert fr["reserve"]["status"] == "missing"


# ── full profile ─────────────────────────────────────────────────────────────────

def test_full_profile_all_sections(in_memory_db):
    _add_stablecoin("USDT")
    _add_price("USDT")
    _add_supply("USDT", 100_000_000_000, chains=CHAINS)
    _add_score("USDT")
    _add_reserve("USDT", composition={"US_Treasuries": 0.84, "cash": 0.16})
    p = get_stablecoin_profile("USDT")
    for section in ("price", "supply", "scores", "reserve"):
        assert p[section] is not None


# ── API endpoint ──────────────────────────────────────────────────────────────────

def test_profile_endpoint_not_found(in_memory_db):
    resp = client.get("/stablecoins/NOPE/profile")
    assert resp.status_code == 404


def test_profile_endpoint_returns_structure(in_memory_db):
    _add_stablecoin("USDT")
    _add_price("USDT")
    _add_supply("USDT", 100_000_000_000, chains=CHAINS)
    _add_score("USDT")
    _add_reserve("USDT", composition={"US_Treasuries": 0.84, "cash": 0.16})
    resp = client.get("/stablecoins/USDT/profile")
    assert resp.status_code == 200
    data = resp.json()
    assert data["symbol"] == "USDT"
    assert {"price", "supply", "scores", "reserve", "freshness"} <= set(data)
    assert data["supply"]["top_chain"] == "Ethereum"


def test_profile_endpoint_case_insensitive(in_memory_db):
    _add_stablecoin("USDT")
    resp = client.get("/stablecoins/usdt/profile")
    assert resp.status_code == 200
    assert resp.json()["symbol"] == "USDT"


def test_profile_route_not_shadowed_by_symbol_route(in_memory_db):
    # /stablecoins/{symbol}/profile must resolve to the profile endpoint.
    _add_stablecoin("USDT")
    resp = client.get("/stablecoins/USDT/profile")
    assert resp.status_code == 200
    assert "freshness" in resp.json()
