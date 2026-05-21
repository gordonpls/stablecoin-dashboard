"""Tests for the data-validation service and its FastAPI endpoint.

The autouse `in_memory_db` fixture (conftest.py) gives each test a fresh
in-memory SQLite shared by the service and the FastAPI client.
"""

import json
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.api.server import app
from db.models import DataQualityWarning, PriceSnapshot, SupplySnapshot, get_session
from services import data_validation as dv
from services.data_validation import query_warnings, run_validation, warning_summary

client = TestClient(app)

NOW = datetime(2026, 5, 21, 12, 0, 0)


# ── helpers ─────────────────────────────────────────────────────────────────────

def _add_price(symbol, price, *, bps=None, when=NOW, source="binance"):
    with get_session() as s:
        s.add(PriceSnapshot(
            symbol=symbol, price=price, peg_deviation_bps=bps,
            bid_depth_usd=None, ask_depth_usd=None, source=source, recorded_at=when,
        ))
        s.commit()


def _consistent_bps(price):
    return round(abs(price - 1.0) * 10_000, 2)


def _add_supply(symbol, supply, *, when=NOW, chains={"Ethereum": 1}):
    with get_session() as s:
        s.add(SupplySnapshot(
            symbol=symbol, circulating_supply=supply,
            supply_by_chain=json.dumps(chains) if chains is not None else None,
            recorded_at=when,
        ))
        s.commit()


def _by_type(warnings, warning_type):
    return [w for w in warnings if w["warning_type"] == warning_type]


def _open_count():
    with get_session() as s:
        from sqlalchemy import func, select
        return s.execute(
            select(func.count()).where(DataQualityWarning.resolved_at.is_(None))
        ).scalar_one()


# ── empty / valid data ────────────────────────────────────────────────────────────

def test_empty_db_opens_nothing(in_memory_db):
    result = run_validation(now=NOW)
    assert result == {"opened": [], "resolved": []}
    assert query_warnings() == []


def test_valid_data_opens_no_warnings(in_memory_db):
    _add_price("USDT", 1.0008, bps=_consistent_bps(1.0008))
    _add_supply("USDT", 80_000_000_000, chains={"Ethereum": 50, "Tron": 30})
    assert run_validation(now=NOW)["opened"] == []
    assert query_warnings() == []


# ── impossible price ───────────────────────────────────────────────────────────────

def test_impossible_price_opens_high_warning(in_memory_db):
    _add_price("USDT", 0.50, bps=_consistent_bps(0.50))  # consistent peg → isolate price rule
    opened = _by_type(run_validation(now=NOW)["opened"], dv.IMPOSSIBLE_PRICE)
    assert len(opened) == 1
    w = opened[0]
    assert w["symbol"] == "USDT"
    assert w["severity"] == "high"
    assert w["metric_name"] == "price"
    assert "0.50" in w["message"] or "0.5000" in w["message"]


def test_price_above_band_flagged(in_memory_db):
    _add_price("USDC", 1.25, bps=_consistent_bps(1.25))
    assert len(_by_type(run_validation(now=NOW)["opened"], dv.IMPOSSIBLE_PRICE)) == 1


def test_price_in_band_not_flagged(in_memory_db):
    _add_price("USDC", 0.998, bps=_consistent_bps(0.998))
    assert _by_type(run_validation(now=NOW)["opened"], dv.IMPOSSIBLE_PRICE) == []


# ── peg deviation mismatch ─────────────────────────────────────────────────────────

def test_peg_mismatch_opens_warning(in_memory_db):
    _add_price("DAI", 1.0, bps=50.0)  # implies 0 bps, stored 50 → mismatch
    opened = _by_type(run_validation(now=NOW)["opened"], dv.PEG_DEVIATION_MISMATCH)
    assert len(opened) == 1
    assert opened[0]["severity"] == "medium"
    assert opened[0]["metric_name"] == "peg_deviation_bps"


def test_consistent_peg_not_flagged(in_memory_db):
    _add_price("DAI", 1.002, bps=_consistent_bps(1.002))  # exactly 20 bps
    assert _by_type(run_validation(now=NOW)["opened"], dv.PEG_DEVIATION_MISMATCH) == []


def test_null_peg_not_flagged(in_memory_db):
    _add_price("DAI", 1.002, bps=None)
    assert _by_type(run_validation(now=NOW)["opened"], dv.PEG_DEVIATION_MISMATCH) == []


# ── non-positive supply ────────────────────────────────────────────────────────────

def test_zero_supply_opens_high_warning(in_memory_db):
    _add_supply("USDT", 0.0)
    opened = _by_type(run_validation(now=NOW)["opened"], dv.NON_POSITIVE_SUPPLY)
    assert len(opened) == 1
    assert opened[0]["severity"] == "high"


def test_negative_supply_flagged(in_memory_db):
    _add_supply("USDT", -100.0)
    assert len(_by_type(run_validation(now=NOW)["opened"], dv.NON_POSITIVE_SUPPLY)) == 1


def test_positive_supply_not_flagged(in_memory_db):
    _add_supply("USDT", 1_000_000.0)
    assert _by_type(run_validation(now=NOW)["opened"], dv.NON_POSITIVE_SUPPLY) == []


# ── supply jump ────────────────────────────────────────────────────────────────────

def test_large_supply_jump_is_high(in_memory_db):
    _add_supply("USDT", 1_000_000_000, when=NOW - timedelta(hours=1))
    _add_supply("USDT", 2_000_000_000, when=NOW)  # +100%
    opened = _by_type(run_validation(now=NOW)["opened"], dv.SUPPLY_JUMP)
    assert len(opened) == 1
    assert opened[0]["severity"] == "high"


def test_moderate_supply_jump_is_medium(in_memory_db):
    _add_supply("USDC", 1_000_000_000, when=NOW - timedelta(hours=1))
    _add_supply("USDC", 1_600_000_000, when=NOW)  # +60%
    opened = _by_type(run_validation(now=NOW)["opened"], dv.SUPPLY_JUMP)
    assert len(opened) == 1
    assert opened[0]["severity"] == "medium"


def test_normal_supply_change_not_flagged(in_memory_db):
    _add_supply("USDC", 1_000_000_000, when=NOW - timedelta(hours=1))
    _add_supply("USDC", 1_080_000_000, when=NOW)  # +8% — a market move, not bad data
    assert _by_type(run_validation(now=NOW)["opened"], dv.SUPPLY_JUMP) == []


# ── duplicate snapshot ─────────────────────────────────────────────────────────────

def test_duplicate_snapshot_flagged(in_memory_db):
    _add_supply("USDX", 1_000_000_000, when=NOW)
    _add_supply("USDX", 5_000, when=NOW)  # ticker collision at same timestamp
    opened = _by_type(run_validation(now=NOW)["opened"], dv.DUPLICATE_SNAPSHOT)
    assert len(opened) == 1
    assert opened[0]["severity"] == "medium"
    assert opened[0]["symbol"] == "USDX"


def test_single_snapshot_not_flagged_as_duplicate(in_memory_db):
    _add_supply("USDX", 1_000_000_000, when=NOW)
    assert _by_type(run_validation(now=NOW)["opened"], dv.DUPLICATE_SNAPSHOT) == []


# ── missing chain distribution ─────────────────────────────────────────────────────

def test_missing_chain_distribution_flagged(in_memory_db):
    _add_supply("USDT", 1_000_000_000, chains=None)
    opened = _by_type(run_validation(now=NOW)["opened"], dv.MISSING_CHAIN_DISTRIBUTION)
    assert len(opened) == 1
    assert opened[0]["severity"] == "low"


def test_empty_chain_dict_flagged(in_memory_db):
    _add_supply("USDT", 1_000_000_000, chains={})
    assert len(_by_type(run_validation(now=NOW)["opened"], dv.MISSING_CHAIN_DISTRIBUTION)) == 1


def test_present_chain_distribution_not_flagged(in_memory_db):
    _add_supply("USDT", 1_000_000_000, chains={"Ethereum": 100})
    assert _by_type(run_validation(now=NOW)["opened"], dv.MISSING_CHAIN_DISTRIBUTION) == []


# ── lifecycle: idempotency and auto-resolve ────────────────────────────────────────

def test_validation_is_idempotent(in_memory_db):
    _add_price("USDT", 0.50, bps=_consistent_bps(0.50))
    first = run_validation(now=NOW)
    assert len(_by_type(first["opened"], dv.IMPOSSIBLE_PRICE)) == 1

    second = run_validation(now=NOW + timedelta(minutes=10))  # unchanged problem
    assert second["opened"] == []
    assert second["resolved"] == []
    assert _open_count() == 1  # not re-opened


def test_warning_auto_resolves_when_data_fixed(in_memory_db):
    _add_price("USDT", 0.50, bps=_consistent_bps(0.50))
    run_validation(now=NOW)
    assert _open_count() == 1

    # A later, valid price means the latest data no longer trips the rule.
    _add_price("USDT", 1.0, bps=0.0, when=NOW + timedelta(minutes=10))
    resolved = run_validation(now=NOW + timedelta(minutes=10))["resolved"]
    assert len(_by_type(resolved, dv.IMPOSSIBLE_PRICE)) == 1
    assert _open_count() == 0

    assert query_warnings() == []                       # active only → empty
    history = query_warnings(active_only=False)         # history retains it
    assert len(history) == 1
    assert history[0]["resolved_at"] is not None


# ── queries and summary ────────────────────────────────────────────────────────────

def test_query_filters(in_memory_db):
    _add_price("USDT", 0.50, bps=_consistent_bps(0.50))      # IMPOSSIBLE_PRICE, high, USDT
    _add_supply("USDC", 1_000_000_000, chains=None)          # MISSING_CHAIN, low, USDC
    run_validation(now=NOW)

    assert {w["symbol"] for w in query_warnings(symbol="USDT")} == {"USDT"}
    assert all(w["severity"] == "high" for w in query_warnings(severity="high"))
    assert all(w["warning_type"] == dv.MISSING_CHAIN_DISTRIBUTION
               for w in query_warnings(warning_type=dv.MISSING_CHAIN_DISTRIBUTION))


def test_warning_summary_counts(in_memory_db):
    _add_price("USDT", 0.50, bps=_consistent_bps(0.50))      # high
    _add_supply("USDC", 1_000_000_000, chains=None)          # low
    run_validation(now=NOW)

    summary = warning_summary()
    assert summary["active_total"] == 2
    assert summary["by_severity"]["high"] == 1
    assert summary["by_severity"]["low"] == 1
    assert summary["by_type"][dv.IMPOSSIBLE_PRICE] == 1
    assert summary["by_type"][dv.MISSING_CHAIN_DISTRIBUTION] == 1


# ── API endpoint ───────────────────────────────────────────────────────────────────

def test_data_quality_endpoint_empty(in_memory_db):
    resp = client.get("/data-quality")
    assert resp.status_code == 200
    body = resp.json()
    assert body["warnings"] == []
    assert body["summary"]["active_total"] == 0


def test_data_quality_endpoint_returns_warnings(in_memory_db):
    _add_price("USDT", 0.50, bps=_consistent_bps(0.50))
    run_validation(now=NOW)

    resp = client.get("/data-quality")
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["active_total"] == 1
    assert len(body["warnings"]) == 1
    expected = {
        "id", "symbol", "provider", "metric_name", "warning_type",
        "severity", "message", "detected_at", "resolved_at",
    }
    assert expected <= set(body["warnings"][0].keys())
    assert body["warnings"][0]["warning_type"] == dv.IMPOSSIBLE_PRICE


def test_data_quality_endpoint_filters(in_memory_db):
    _add_price("USDT", 0.50, bps=_consistent_bps(0.50))
    run_validation(now=NOW)

    resp = client.get("/data-quality", params={"severity": "low"})
    assert resp.status_code == 200
    assert resp.json()["warnings"] == []  # only a high-severity warning exists


def test_data_quality_endpoint_active_only_toggle(in_memory_db):
    _add_price("USDT", 0.50, bps=_consistent_bps(0.50))
    run_validation(now=NOW)
    _add_price("USDT", 1.0, bps=0.0, when=NOW + timedelta(minutes=10))
    run_validation(now=NOW + timedelta(minutes=10))  # resolves it

    assert client.get("/data-quality").json()["warnings"] == []
    resp = client.get("/data-quality", params={"active_only": "false"})
    assert len(resp.json()["warnings"]) == 1


def test_data_quality_endpoint_limit_validation(in_memory_db):
    resp = client.get("/data-quality", params={"limit": 501})
    assert resp.status_code == 422  # exceeds le=500
