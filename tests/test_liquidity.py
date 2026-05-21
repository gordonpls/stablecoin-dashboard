"""Tests for the liquidity-trend service and its API endpoints.

The autouse `in_memory_db` fixture (conftest.py) gives each test a fresh
in-memory SQLite shared by the service and the FastAPI client. NOW is anchored
to real utcnow so the service's HISTORY_WINDOW cutoff always includes the test
data regardless of the wall-clock date.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.server import app
from db.models import PriceSnapshot, Stablecoin, get_session
from services.liquidity import (
    get_liquidity_detail,
    largest_liquidity_drops,
)

client = TestClient(app)

NOW = datetime.utcnow().replace(microsecond=0)


# ── helpers ─────────────────────────────────────────────────────────────────────

def _add_price(symbol: str, when: datetime, *, bid: float | None, ask: float | None) -> None:
    with get_session() as s:
        s.add(PriceSnapshot(
            symbol=symbol, price=1.0, peg_deviation_bps=0.0,
            bid_depth_usd=bid, ask_depth_usd=ask,
            source="binance", recorded_at=when,
        ))
        s.commit()


def _register(symbol: str) -> None:
    with get_session() as s:
        s.add(Stablecoin(id=symbol.lower(), symbol=symbol, name=f"{symbol} Coin"))
        s.commit()


# ── unknown / empty ───────────────────────────────────────────────────────────────

def test_unknown_symbol_returns_none(in_memory_db):
    assert get_liquidity_detail("NOPE") is None


def test_registered_symbol_no_history(in_memory_db):
    _register("USDT")
    detail = get_liquidity_detail("USDT")
    assert detail is not None
    assert detail["registered"] is True
    assert detail["current"] is None
    assert detail["change_24h"] is None
    assert detail["change_7d"] is None
    assert detail["trend"] is None
    assert detail["history"] == []


# ── current depth + imbalance ──────────────────────────────────────────────────────

def test_current_depth_and_imbalance(in_memory_db):
    _add_price("USDC", NOW, bid=6_000_000, ask=4_000_000)
    detail = get_liquidity_detail("usdc")  # case-insensitive
    cur = detail["current"]
    assert cur["bid_depth_usd"] == 6_000_000
    assert cur["ask_depth_usd"] == 4_000_000
    assert cur["total_depth_usd"] == 10_000_000
    assert cur["imbalance_pct"] == pytest.approx(20.0)  # (6-4)/10


def test_rows_with_no_depth_are_ignored(in_memory_db):
    _register("DAI")
    _add_price("DAI", NOW, bid=None, ask=None)
    detail = get_liquidity_detail("DAI")
    assert detail["current"] is None
    assert detail["history"] == []


def test_one_sided_depth_counts(in_memory_db):
    _add_price("DAI", NOW, bid=5_000_000, ask=None)
    cur = get_liquidity_detail("DAI")["current"]
    assert cur["total_depth_usd"] == 5_000_000
    assert cur["imbalance_pct"] == pytest.approx(100.0)  # all bid


# ── 24h / 7d change ────────────────────────────────────────────────────────────────

def test_24h_drop_is_detected(in_memory_db):
    _add_price("USDC", NOW - timedelta(hours=24), bid=5_000_000, ask=5_000_000)  # 10M
    _add_price("USDC", NOW, bid=4_000_000, ask=4_000_000)                        # 8M, -20%
    ch = get_liquidity_detail("USDC")["change_24h"]
    assert ch["percent_change"] == pytest.approx(-20.0)
    assert ch["absolute_change"] == pytest.approx(-2_000_000)
    assert ch["severity"] == "medium"
    assert ch["comparison_window"] == "24h"
    assert "fell 20% over 24 hours" in ch["summary"]


def test_severe_drop_is_high_severity(in_memory_db):
    _add_price("USDC", NOW - timedelta(hours=24), bid=5_000_000, ask=5_000_000)  # 10M
    _add_price("USDC", NOW, bid=3_500_000, ask=3_500_000)                        # 7M, -30%
    ch = get_liquidity_detail("USDC")["change_24h"]
    assert ch["severity"] == "high"


def test_7d_change_and_trend_improving(in_memory_db):
    _add_price("USDT", NOW - timedelta(days=7), bid=4_000_000, ask=4_000_000)   # 8M
    _add_price("USDT", NOW, bid=5_000_000, ask=5_000_000)                       # 10M, +25%
    detail = get_liquidity_detail("USDT")
    assert detail["change_7d"]["percent_change"] == pytest.approx(25.0)
    assert detail["trend"] == "improving"


def test_insufficient_separation_yields_no_change(in_memory_db):
    # Two points one hour apart: not enough separation for a 24h (or 7d) claim.
    _add_price("USDT", NOW - timedelta(hours=1), bid=5_000_000, ask=5_000_000)
    _add_price("USDT", NOW, bid=4_000_000, ask=4_000_000)
    detail = get_liquidity_detail("USDT")
    assert detail["change_24h"] is None
    assert detail["change_7d"] is None
    assert detail["trend"] is None


def test_imbalance_change_24h(in_memory_db):
    _add_price("USDC", NOW - timedelta(hours=24), bid=5_000_000, ask=5_000_000)  # 0% imbalance
    _add_price("USDC", NOW, bid=8_000_000, ask=2_000_000)                        # +60% imbalance
    ic = get_liquidity_detail("USDC")["imbalance_change_24h"]
    assert ic["previous_value"] == pytest.approx(0.0)
    assert ic["current_value"] == pytest.approx(60.0)
    assert ic["absolute_change"] == pytest.approx(60.0)


def test_history_collapses_duplicate_timestamps(in_memory_db):
    # Price + liquidity pipelines can both write at the same instant.
    _add_price("USDC", NOW, bid=3_000_000, ask=3_000_000)  # 6M
    _add_price("USDC", NOW, bid=4_000_000, ask=4_000_000)  # 8M -> dominant
    detail = get_liquidity_detail("USDC")
    assert len(detail["history"]) == 1
    assert detail["current"]["total_depth_usd"] == 8_000_000


# ── largest liquidity drops ─────────────────────────────────────────────────────────

def test_drops_empty_db(in_memory_db):
    assert largest_liquidity_drops() == []


def test_drops_rank_and_exclude_rises(in_memory_db):
    # AAA: -30% (high), BBB: -10% (low), CCC: +25% (rise -> excluded).
    _add_price("AAA", NOW - timedelta(hours=24), bid=5_000_000, ask=5_000_000)
    _add_price("AAA", NOW, bid=3_500_000, ask=3_500_000)
    _add_price("BBB", NOW - timedelta(hours=24), bid=5_000_000, ask=5_000_000)
    _add_price("BBB", NOW, bid=4_500_000, ask=4_500_000)
    _add_price("CCC", NOW - timedelta(hours=24), bid=4_000_000, ask=4_000_000)
    _add_price("CCC", NOW, bid=5_000_000, ask=5_000_000)

    drops = largest_liquidity_drops(window="24h")
    assets = [d["asset"] for d in drops]
    assert assets == ["AAA", "BBB"]  # ranked by severity, CCC (rise) excluded
    assert drops[0]["severity"] == "high"


def test_drops_limit_is_honored(in_memory_db):
    for sym in ("AAA", "BBB", "CCC"):
        _add_price(sym, NOW - timedelta(hours=24), bid=5_000_000, ask=5_000_000)
        _add_price(sym, NOW, bid=3_000_000, ask=3_000_000)  # -40% each
    assert len(largest_liquidity_drops(limit=2)) == 2


def test_drops_invalid_window_raises(in_memory_db):
    with pytest.raises(ValueError):
        largest_liquidity_drops(window="1h")


def test_drops_7d_window(in_memory_db):
    _add_price("USDT", NOW - timedelta(days=7), bid=6_000_000, ask=6_000_000)  # 12M
    _add_price("USDT", NOW, bid=4_000_000, ask=4_000_000)                      # 8M, -33%
    drops = largest_liquidity_drops(window="7d")
    assert len(drops) == 1
    assert drops[0]["comparison_window"] == "7d"


# ── API endpoints ────────────────────────────────────────────────────────────────

def test_liquidity_endpoint_unknown_symbol_404(in_memory_db):
    resp = client.get("/stablecoins/NOPE/liquidity")
    assert resp.status_code == 404


def test_liquidity_endpoint_returns_structure(in_memory_db):
    _add_price("USDC", NOW - timedelta(hours=24), bid=5_000_000, ask=5_000_000)
    _add_price("USDC", NOW, bid=4_000_000, ask=4_000_000)
    resp = client.get("/stablecoins/USDC/liquidity")
    assert resp.status_code == 200
    body = resp.json()
    assert {"symbol", "current", "change_24h", "change_7d", "trend", "history"} <= set(body)
    assert body["change_24h"]["percent_change"] == pytest.approx(-20.0)


def test_drops_endpoint_empty_db(in_memory_db):
    resp = client.get("/stablecoins/liquidity-drops")
    assert resp.status_code == 200
    assert resp.json() == []


def test_drops_endpoint_not_shadowed_by_symbol_route(in_memory_db):
    # /stablecoins/liquidity-drops must resolve to the drops endpoint, not the
    # {symbol} lookup (which would 404 for an unknown symbol).
    resp = client.get("/stablecoins/liquidity-drops")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_drops_endpoint_window_validation(in_memory_db):
    resp = client.get("/stablecoins/liquidity-drops?window=bad")
    assert resp.status_code == 422


def test_drops_endpoint_limit_validation(in_memory_db):
    resp = client.get("/stablecoins/liquidity-drops?limit=500")
    assert resp.status_code == 422  # exceeds le=100
