"""Tests for the market dominance / share-momentum service and its endpoint.

The autouse `in_memory_db` fixture (conftest.py) gives each test a fresh
in-memory SQLite shared by the service and the FastAPI client. Supply is read
from `supply_snapshots.circulating_supply`, the column the update_supply
pipeline writes.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.server import app
from db.models import Stablecoin, SupplySnapshot, get_session
from services.dominance import (
    compute_dominance,
    market_share_movers,
)

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


def _rank(dominance: dict, asset: str) -> dict:
    return next(r for r in dominance["rankings"] if r["asset"] == asset)


# ── empty / degenerate ─────────────────────────────────────────────────────────

def test_empty_db_returns_empty_shape(in_memory_db):
    d = compute_dominance()
    assert d["total_tracked_supply"] == 0.0
    assert d["asset_count"] == 0
    assert d["top_asset"] is None
    assert d["top_asset_share"] is None
    assert d["rankings"] == []
    assert d["recorded_at"] is None


def test_non_positive_supply_ignored(in_memory_db):
    _add_supply("AAA", 0.0)
    _add_supply("BBB", -5.0)
    d = compute_dominance()
    assert d["asset_count"] == 0
    assert d["total_tracked_supply"] == 0.0


# ── current market share ─────────────────────────────────────────────────────────

def test_market_share_sums_to_100(in_memory_db):
    _add_supply("USDT", 6_000_000)
    _add_supply("USDC", 3_000_000)
    _add_supply("DAI", 1_000_000)
    d = compute_dominance()
    assert d["total_tracked_supply"] == pytest.approx(10_000_000)
    assert d["asset_count"] == 3
    assert sum(r["market_share"] for r in d["rankings"]) == pytest.approx(100.0)
    assert _rank(d, "USDT")["market_share"] == pytest.approx(60.0)
    assert _rank(d, "USDC")["market_share"] == pytest.approx(30.0)


def test_rankings_sorted_by_share_and_top_asset(in_memory_db):
    _add_supply("DAI", 1_000_000)
    _add_supply("USDT", 6_000_000)
    _add_supply("USDC", 3_000_000)
    d = compute_dominance()
    assert [r["asset"] for r in d["rankings"]] == ["USDT", "USDC", "DAI"]
    assert d["top_asset"] == "USDT"
    assert d["top_asset_share"] == pytest.approx(60.0)


def test_name_attached_from_registry(in_memory_db):
    _register("USDT", "Tether")
    _add_supply("USDT", 5_000_000)
    _add_supply("WOOF", 5_000_000)  # unregistered
    d = compute_dominance()
    assert _rank(d, "USDT")["name"] == "Tether"
    assert _rank(d, "WOOF")["name"] is None


def test_uses_latest_snapshot_per_asset(in_memory_db):
    _add_supply("USDT", 1_000_000, when=NOW - timedelta(hours=2))
    _add_supply("USDT", 9_000_000, when=NOW)  # newest wins
    _add_supply("USDC", 1_000_000, when=NOW)
    d = compute_dominance()
    assert _rank(d, "USDT")["market_share"] == pytest.approx(90.0)


def test_ticker_collision_collapses_to_dominant(in_memory_db):
    # Two assets share USDX at the same timestamp; the larger row represents it.
    _add_supply("USDX", 8_000_000, when=NOW)
    _add_supply("USDX", 500_000, when=NOW)
    _add_supply("USDC", 2_000_000, when=NOW)
    d = compute_dominance()
    assert _rank(d, "USDX")["circulating_supply"] == pytest.approx(8_000_000)
    assert d["total_tracked_supply"] == pytest.approx(10_000_000)


def test_limit_truncates_rankings(in_memory_db):
    for sym, val in (("AAA", 5_000_000), ("BBB", 3_000_000), ("CCC", 2_000_000)):
        _add_supply(sym, val)
    d = compute_dominance(limit=2)
    assert len(d["rankings"]) == 2
    assert d["asset_count"] == 3  # count reflects the whole market, not the page


# ── market-share change over windows ─────────────────────────────────────────────

def test_share_change_7d_gain_and_loss(in_memory_db):
    # 7 days ago both assets were equal (50/50). USDT grew its supply, taking share.
    week_ago = NOW - timedelta(days=7)
    _add_supply("USDT", 5_000_000, when=week_ago)
    _add_supply("USDC", 5_000_000, when=week_ago)
    _add_supply("USDT", 9_000_000, when=NOW)
    _add_supply("USDC", 1_000_000, when=NOW)
    d = compute_dominance()
    usdt = _rank(d, "USDT")
    usdc = _rank(d, "USDC")
    assert usdt["market_share"] == pytest.approx(90.0)
    assert usdt["market_share_7d_ago"] == pytest.approx(50.0)
    assert usdt["market_share_change_7d"] == pytest.approx(40.0)
    assert usdc["market_share_change_7d"] == pytest.approx(-40.0)


def test_insufficient_history_yields_none_change(in_memory_db):
    # Only ~1 day of history: a 7d (and 30d) comparison is not claimed.
    _add_supply("USDT", 6_000_000, when=NOW - timedelta(days=1))
    _add_supply("USDC", 4_000_000, when=NOW - timedelta(days=1))
    _add_supply("USDT", 7_000_000, when=NOW)
    _add_supply("USDC", 3_000_000, when=NOW)
    d = compute_dominance()
    usdt = _rank(d, "USDT")
    assert usdt["market_share_7d_ago"] is None
    assert usdt["market_share_change_7d"] is None
    assert usdt["market_share_30d_ago"] is None


def test_7d_available_but_30d_insufficient(in_memory_db):
    # 8 days of history: 7d window is computable, 30d is not.
    _add_supply("USDT", 5_000_000, when=NOW - timedelta(days=8))
    _add_supply("USDC", 5_000_000, when=NOW - timedelta(days=8))
    _add_supply("USDT", 8_000_000, when=NOW)
    _add_supply("USDC", 2_000_000, when=NOW)
    usdt = _rank(compute_dominance(), "USDT")
    assert usdt["market_share_change_7d"] is not None
    assert usdt["market_share_change_30d"] is None


def test_new_asset_excluded_from_past_total(in_memory_db):
    # USDC only appears today; the 7d-ago market was USDT alone (100%).
    week_ago = NOW - timedelta(days=7)
    _add_supply("USDT", 10_000_000, when=week_ago)
    _add_supply("USDT", 6_000_000, when=NOW)
    _add_supply("USDC", 4_000_000, when=NOW)  # brand new
    d = compute_dominance()
    usdt = _rank(d, "USDT")
    usdc = _rank(d, "USDC")
    assert usdt["market_share_7d_ago"] == pytest.approx(100.0)  # was the whole market
    assert usdt["market_share"] == pytest.approx(60.0)
    assert usdt["market_share_change_7d"] == pytest.approx(-40.0)
    # USDC had no sufficiently-old point, so its past share is not claimed.
    assert usdc["market_share_7d_ago"] is None
    assert usdc["market_share_change_7d"] is None


# ── gainers / losers ────────────────────────────────────────────────────────────

def test_movers_split_gainers_and_losers(in_memory_db):
    week_ago = NOW - timedelta(days=7)
    _add_supply("USDT", 5_000_000, when=week_ago)
    _add_supply("USDC", 5_000_000, when=week_ago)
    _add_supply("USDT", 9_000_000, when=NOW)
    _add_supply("USDC", 1_000_000, when=NOW)
    movers = market_share_movers(window="7d")
    assert movers["window"] == "7d"
    assert [g["asset"] for g in movers["gainers"]] == ["USDT"]
    assert [l["asset"] for l in movers["losers"]] == ["USDC"]
    assert movers["gainers"][0]["market_share_change"] == pytest.approx(40.0)


def test_movers_exclude_none_and_zero_change(in_memory_db):
    # Flat share (no change) and insufficient-history assets are not movers.
    week_ago = NOW - timedelta(days=7)
    _add_supply("USDT", 5_000_000, when=week_ago)
    _add_supply("USDC", 5_000_000, when=week_ago)
    _add_supply("USDT", 5_000_000, when=NOW)
    _add_supply("USDC", 5_000_000, when=NOW)
    _add_supply("NEW", 1_000_000, when=NOW)  # no history -> None change
    movers = market_share_movers(window="7d")
    # USDT/USDC unchanged share (~0), NEW has no history -> all excluded.
    movers_assets = [m["asset"] for m in movers["gainers"] + movers["losers"]]
    assert "NEW" not in movers_assets


def test_movers_limit_honored(in_memory_db):
    week_ago = NOW - timedelta(days=7)
    # Five equal assets a week ago; today one shrinks and the rest grow share.
    for sym in ("A", "B", "C", "D", "E"):
        _add_supply(sym, 2_000_000, when=week_ago)
    _add_supply("A", 100_000, when=NOW)
    for sym in ("B", "C", "D", "E"):
        _add_supply(sym, 5_000_000, when=NOW)
    movers = market_share_movers(window="7d", limit=2)
    assert len(movers["gainers"]) == 2


def test_movers_rejects_bad_window(in_memory_db):
    with pytest.raises(ValueError):
        market_share_movers(window="24h")


# ── API endpoint ─────────────────────────────────────────────────────────────────

def test_rankings_endpoint_empty_db(in_memory_db):
    resp = client.get("/stablecoins/rankings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["rankings"] == []
    assert body["movers"]["gainers"] == []


def test_rankings_endpoint_structure(in_memory_db):
    _add_supply("USDT", 6_000_000)
    _add_supply("USDC", 4_000_000)
    resp = client.get("/stablecoins/rankings")
    assert resp.status_code == 200
    body = resp.json()
    assert {"total_tracked_supply", "asset_count", "top_asset", "rankings", "movers"} <= set(body)
    assert body["top_asset"] == "USDT"
    assert body["rankings"][0]["market_share"] == pytest.approx(60.0)
    assert body["movers"]["window"] == "7d"


def test_rankings_endpoint_not_shadowed_by_symbol_route(in_memory_db):
    # /stablecoins/rankings must hit the dominance endpoint, not the {symbol}
    # lookup (which would 404 for an unknown symbol).
    _add_supply("AAA", 1_000_000)
    resp = client.get("/stablecoins/rankings")
    assert resp.status_code == 200
    assert isinstance(resp.json()["rankings"], list)


def test_rankings_endpoint_window_param(in_memory_db):
    _add_supply("USDT", 5_000_000)
    resp = client.get("/stablecoins/rankings?window=30d")
    assert resp.status_code == 200
    assert resp.json()["movers"]["window"] == "30d"


def test_rankings_endpoint_rejects_bad_window(in_memory_db):
    resp = client.get("/stablecoins/rankings?window=24h")
    assert resp.status_code == 422


def test_rankings_endpoint_limit_validation(in_memory_db):
    resp = client.get("/stablecoins/rankings?limit=500")
    assert resp.status_code == 422  # exceeds le=200
