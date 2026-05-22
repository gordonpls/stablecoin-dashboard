"""Tests for the per-asset supply detail service and its endpoint.

The autouse `in_memory_db` fixture (conftest.py) gives each test a fresh
in-memory SQLite shared by the service and the FastAPI client. Supply is read
from `supply_snapshots`, the table the update_supply pipeline writes.
"""

import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.server import app
from db.models import Stablecoin, SupplySnapshot, get_session
from services.supply import get_supply_detail

client = TestClient(app)

NOW = datetime.utcnow().replace(microsecond=0)


# ── helpers ─────────────────────────────────────────────────────────────────────

def _add_supply(symbol: str, supply: float, *, when: datetime = NOW,
                chains: str | None = None) -> None:
    with get_session() as s:
        s.add(SupplySnapshot(
            symbol=symbol,
            circulating_supply=supply,
            supply_by_chain=chains,
            recorded_at=when,
        ))
        s.commit()


def _register(symbol: str, name: str | None = None) -> None:
    with get_session() as s:
        s.add(Stablecoin(id=symbol.lower(), symbol=symbol, name=name or f"{symbol} Coin"))
        s.commit()


def _chain_json(**chains: float) -> str:
    """Build supply_by_chain JSON in DefiLlama's nested {current: {peggedUSD}} shape."""
    return json.dumps({c: {"current": {"peggedUSD": v}} for c, v in chains.items()})


# ── unknown / empty ──────────────────────────────────────────────────────────────

def test_unknown_symbol_returns_none(in_memory_db):
    assert get_supply_detail("NOPE") is None


def test_registered_but_no_supply_returns_shape(in_memory_db):
    _register("USDT", "Tether")
    detail = get_supply_detail("USDT")
    assert detail is not None
    assert detail["symbol"] == "USDT"
    assert detail["name"] == "Tether"
    assert detail["current"] is None
    assert detail["change_7d"] is None
    assert detail["change_30d"] is None
    assert detail["history"] == []


def test_symbol_is_uppercased(in_memory_db):
    _add_supply("USDT", 5_000_000)
    detail = get_supply_detail("usdt")
    assert detail["symbol"] == "USDT"
    assert detail["current"]["circulating_supply"] == pytest.approx(5_000_000)


# ── current snapshot + chains ─────────────────────────────────────────────────────

def test_current_uses_latest_snapshot(in_memory_db):
    _add_supply("USDT", 1_000_000, when=NOW - timedelta(hours=2))
    _add_supply("USDT", 9_000_000, when=NOW)  # newest wins
    detail = get_supply_detail("USDT")
    assert detail["current"]["circulating_supply"] == pytest.approx(9_000_000)
    assert detail["current"]["recorded_at"] == NOW.isoformat()


def test_current_parses_chain_breakdown(in_memory_db):
    _add_supply("USDT", 10_000_000, chains=_chain_json(Ethereum=7_000_000, Tron=3_000_000))
    current = get_supply_detail("USDT")["current"]
    assert current["top_chain"] == "Ethereum"
    assert current["top_chain_pct"] == pytest.approx(70.0)
    assert current["chain_count"] == 2
    assert [c["chain"] for c in current["chains"]] == ["Ethereum", "Tron"]


def test_current_without_chain_data(in_memory_db):
    _add_supply("USDT", 5_000_000)  # no supply_by_chain
    current = get_supply_detail("USDT")["current"]
    assert current["top_chain"] is None
    assert current["chain_count"] == 0
    assert current["chains"] == []


def test_ticker_collision_collapses_to_dominant(in_memory_db):
    # Two assets share USDX at the same timestamp; the larger row represents it.
    _add_supply("USDX", 8_000_000, when=NOW)
    _add_supply("USDX", 500_000, when=NOW)
    current = get_supply_detail("USDX")["current"]
    assert current["circulating_supply"] == pytest.approx(8_000_000)


def test_non_positive_latest_supply_yields_no_current(in_memory_db):
    _add_supply("BAD", 0.0)
    detail = get_supply_detail("BAD")
    assert detail is not None  # the asset is known (it has a snapshot)
    assert detail["current"] is None
    assert detail["history"] == []


# ── 7d / 30d change ───────────────────────────────────────────────────────────────

def test_change_7d_computed(in_memory_db):
    _add_supply("USDT", 5_000_000, when=NOW - timedelta(days=7))
    _add_supply("USDT", 6_000_000, when=NOW)
    change = get_supply_detail("USDT")["change_7d"]
    assert change["previous_value"] == pytest.approx(5_000_000)
    assert change["current_value"] == pytest.approx(6_000_000)
    assert change["absolute_change"] == pytest.approx(1_000_000)
    assert change["percent_change"] == pytest.approx(20.0)


def test_insufficient_history_yields_none_change(in_memory_db):
    # Only ~1 day of history: neither a 7d nor a 30d change is claimed.
    _add_supply("USDT", 6_000_000, when=NOW - timedelta(days=1))
    _add_supply("USDT", 7_000_000, when=NOW)
    detail = get_supply_detail("USDT")
    assert detail["change_7d"] is None
    assert detail["change_30d"] is None


def test_7d_available_but_30d_insufficient(in_memory_db):
    # 8 days of history: 7d window is computable, 30d is not.
    _add_supply("USDT", 5_000_000, when=NOW - timedelta(days=8))
    _add_supply("USDT", 8_000_000, when=NOW)
    detail = get_supply_detail("USDT")
    assert detail["change_7d"] is not None
    assert detail["change_30d"] is None


def test_change_30d_computed_with_long_history(in_memory_db):
    _add_supply("USDT", 4_000_000, when=NOW - timedelta(days=30))
    _add_supply("USDT", 5_000_000, when=NOW - timedelta(days=7))
    _add_supply("USDT", 6_000_000, when=NOW)
    detail = get_supply_detail("USDT")
    assert detail["change_30d"]["previous_value"] == pytest.approx(4_000_000)
    assert detail["change_30d"]["percent_change"] == pytest.approx(50.0)
    assert detail["change_7d"]["previous_value"] == pytest.approx(5_000_000)


# ── history series ────────────────────────────────────────────────────────────────

def test_history_ascending_and_deduped(in_memory_db):
    _add_supply("USDT", 3_000_000, when=NOW - timedelta(days=2))
    _add_supply("USDT", 4_000_000, when=NOW - timedelta(days=1))
    _add_supply("USDT", 5_000_000, when=NOW)
    _add_supply("USDT", 1_000_000, when=NOW)  # collision at NOW -> dominant kept
    history = get_supply_detail("USDT")["history"]
    assert [h["circulating_supply"] for h in history] == [3_000_000, 4_000_000, 5_000_000]
    # ascending by time
    ts = [h["recorded_at"] for h in history]
    assert ts == sorted(ts)


def test_history_respects_history_days_window(in_memory_db):
    _add_supply("USDT", 1_000_000, when=NOW - timedelta(days=20))  # outside 10d
    _add_supply("USDT", 2_000_000, when=NOW - timedelta(days=5))   # inside 10d
    _add_supply("USDT", 3_000_000, when=NOW)
    history = get_supply_detail("USDT", history_days=10)["history"]
    assert [h["circulating_supply"] for h in history] == [2_000_000, 3_000_000]


def test_history_limit_keeps_newest(in_memory_db):
    for i in range(5):
        _add_supply("USDT", 1_000_000 + i, when=NOW - timedelta(days=4 - i))
    history = get_supply_detail("USDT", history_limit=2)["history"]
    assert len(history) == 2
    assert [h["circulating_supply"] for h in history] == [1_000_003, 1_000_004]


def test_short_history_window_still_computes_30d_change(in_memory_db):
    # Caller wants a 5-day chart, but the 30d change should still be available
    # because the service loads at least MIN_LOOKBACK_DAYS of history.
    _add_supply("USDT", 4_000_000, when=NOW - timedelta(days=30))
    _add_supply("USDT", 6_000_000, when=NOW)
    detail = get_supply_detail("USDT", history_days=5)
    assert detail["change_30d"] is not None
    assert len(detail["history"]) == 1  # only the NOW point is within 5 days


# ── API endpoint ─────────────────────────────────────────────────────────────────

def test_supply_endpoint_structure(in_memory_db):
    _add_supply("USDT", 6_000_000, chains=_chain_json(Ethereum=6_000_000))
    resp = client.get("/stablecoins/USDT/supply")
    assert resp.status_code == 200
    body = resp.json()
    assert {"symbol", "name", "current", "change_7d", "change_30d", "history"} <= set(body)
    assert body["symbol"] == "USDT"
    assert body["current"]["circulating_supply"] == pytest.approx(6_000_000)


def test_supply_endpoint_unknown_symbol_404(in_memory_db):
    resp = client.get("/stablecoins/NOPE/supply")
    assert resp.status_code == 404


def test_supply_endpoint_not_shadowed_by_symbol_route(in_memory_db):
    # /stablecoins/{symbol}/supply must hit the supply endpoint, not the bare
    # {symbol} lookup, and is case-insensitive on the symbol.
    _add_supply("USDT", 5_000_000)
    resp = client.get("/stablecoins/usdt/supply")
    assert resp.status_code == 200
    assert isinstance(resp.json()["history"], list)


def test_supply_endpoint_validates_history_days(in_memory_db):
    assert client.get("/stablecoins/USDT/supply?history_days=0").status_code == 422
    assert client.get("/stablecoins/USDT/supply?history_days=400").status_code == 422


def test_supply_endpoint_history_limit(in_memory_db):
    for i in range(4):
        _add_supply("USDT", 1_000_000 + i, when=NOW - timedelta(days=3 - i))
    resp = client.get("/stablecoins/USDT/supply?history_limit=2")
    assert resp.status_code == 200
    assert len(resp.json()["history"]) == 2
