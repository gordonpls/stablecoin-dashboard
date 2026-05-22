"""Tests for the watchlist feature: services/watchlist.py + /watchlist endpoints.

The autouse `in_memory_db` fixture (conftest.py) gives each test a fresh
in-memory SQLite, so the watchlist starts empty.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.server import app
from db.models import (
    PriceSnapshot,
    RiskScore,
    Stablecoin,
    SupplySnapshot,
    WatchlistItem,
    get_session,
)
from services.watchlist import (
    add_to_watchlist,
    get_watchlist,
    remove_from_watchlist,
    set_watchlist,
    watchlist_symbols,
)

client = TestClient(app)


# ── helpers ─────────────────────────────────────────────────────────────────────

def _add_stablecoin(symbol: str = "USDT", name: str = "Tether") -> None:
    with get_session() as s:
        s.add(Stablecoin(id=symbol.lower(), symbol=symbol, name=name, issuer="fiat-backed"))
        s.commit()


def _add_price(symbol: str, price: float = 1.0001, dev: float = 1.0) -> None:
    with get_session() as s:
        s.add(PriceSnapshot(
            symbol=symbol, price=price, peg_deviation_bps=dev,
            source="binance", recorded_at=datetime.utcnow(),
        ))
        s.commit()


def _add_score(symbol: str, overall: float = 88.0) -> None:
    with get_session() as s:
        s.add(RiskScore(
            symbol=symbol, peg_score=95.0, liquidity_score=80.0,
            reserve_score=75.0, adoption_score=70.0,
            overall_score=overall, scored_at=datetime.utcnow(),
        ))
        s.commit()


def _add_supply(symbol: str, supply: float = 1_000_000.0) -> None:
    with get_session() as s:
        s.add(SupplySnapshot(
            symbol=symbol, circulating_supply=supply,
            recorded_at=datetime.utcnow(),
        ))
        s.commit()


# ── service: add / remove / membership ───────────────────────────────────────────

def test_add_known_symbol(in_memory_db):
    _add_stablecoin("USDT")
    item = add_to_watchlist("USDT")
    assert item is not None
    assert item["symbol"] == "USDT"
    assert watchlist_symbols() == {"USDT"}


def test_add_unknown_symbol_rejected(in_memory_db):
    item = add_to_watchlist("FAKE")
    assert item is None
    assert watchlist_symbols() == set()


def test_add_is_case_insensitive(in_memory_db):
    _add_stablecoin("USDT")
    item = add_to_watchlist("usdt")
    assert item["symbol"] == "USDT"
    assert watchlist_symbols() == {"USDT"}


def test_add_is_idempotent_and_updates_note(in_memory_db):
    _add_stablecoin("USDT")
    add_to_watchlist("USDT", note="first")
    add_to_watchlist("USDT", note="second")
    # Only one row, note updated to the latest non-null value.
    with get_session() as s:
        rows = s.query(WatchlistItem).all()
    assert len(rows) == 1
    assert rows[0].note == "second"


def test_add_again_without_note_keeps_existing_note(in_memory_db):
    _add_stablecoin("USDT")
    add_to_watchlist("USDT", note="keep me")
    add_to_watchlist("USDT")  # no note → must not wipe the existing one
    wl = get_watchlist()
    assert wl[0]["note"] == "keep me"


def test_remove_existing(in_memory_db):
    _add_stablecoin("USDT")
    add_to_watchlist("USDT")
    assert remove_from_watchlist("usdt") is True
    assert watchlist_symbols() == set()


def test_remove_absent_returns_false(in_memory_db):
    assert remove_from_watchlist("USDT") is False


# ── service: get_watchlist enrichment ─────────────────────────────────────────────

def test_get_watchlist_empty(in_memory_db):
    assert get_watchlist() == []


def test_get_watchlist_enriches_with_latest_metrics(in_memory_db):
    _add_stablecoin("USDC", name="USD Coin")
    _add_price("USDC", price=0.9994, dev=6.0)
    _add_score("USDC", overall=91.0)
    _add_supply("USDC", supply=2_500_000.0)
    add_to_watchlist("USDC", note="primary")

    wl = get_watchlist()
    assert len(wl) == 1
    row = wl[0]
    assert row["symbol"] == "USDC"
    assert row["name"] == "USD Coin"
    assert row["note"] == "primary"
    assert row["price"] == pytest.approx(0.9994)
    assert row["peg_deviation_bps"] == pytest.approx(6.0)
    assert row["overall_score"] == pytest.approx(91.0)
    assert row["circulating_supply"] == pytest.approx(2_500_000.0)


def test_get_watchlist_missing_metrics_are_null(in_memory_db):
    _add_stablecoin("DAI")
    add_to_watchlist("DAI")
    row = get_watchlist()[0]
    assert row["price"] is None
    assert row["peg_deviation_bps"] is None
    assert row["overall_score"] is None
    assert row["circulating_supply"] is None


def test_get_watchlist_uses_latest_price(in_memory_db):
    _add_stablecoin("USDT")
    add_to_watchlist("USDT")
    with get_session() as s:
        s.add(PriceSnapshot(symbol="USDT", price=1.0, peg_deviation_bps=0.0,
                            source="binance", recorded_at=datetime.utcnow() - timedelta(hours=2)))
        s.add(PriceSnapshot(symbol="USDT", price=1.0050, peg_deviation_bps=50.0,
                            source="binance", recorded_at=datetime.utcnow()))
        s.commit()
    row = get_watchlist()[0]
    assert row["price"] == pytest.approx(1.0050)


def test_get_watchlist_newest_first(in_memory_db):
    for sym in ("USDT", "USDC", "DAI"):
        _add_stablecoin(sym, name=sym)
    # Stagger added_at so ordering is deterministic.
    base = datetime.utcnow()
    with get_session() as s:
        s.add(WatchlistItem(symbol="USDT", added_at=base - timedelta(minutes=10)))
        s.add(WatchlistItem(symbol="USDC", added_at=base - timedelta(minutes=5)))
        s.add(WatchlistItem(symbol="DAI", added_at=base))
        s.commit()
    order = [r["symbol"] for r in get_watchlist()]
    assert order == ["DAI", "USDC", "USDT"]


# ── service: set_watchlist sync ───────────────────────────────────────────────────

def test_set_watchlist_adds_and_removes(in_memory_db):
    for sym in ("USDT", "USDC", "DAI"):
        _add_stablecoin(sym, name=sym)
    add_to_watchlist("USDT")
    res = set_watchlist(["USDC", "DAI"])
    assert res["added"] == ["DAI", "USDC"]
    assert res["removed"] == ["USDT"]
    assert watchlist_symbols() == {"USDC", "DAI"}


def test_set_watchlist_skips_unknown(in_memory_db):
    _add_stablecoin("USDT")
    res = set_watchlist(["USDT", "FAKE"])
    assert res["added"] == ["USDT"]
    assert res["skipped"] == ["FAKE"]
    assert watchlist_symbols() == {"USDT"}


def test_set_watchlist_empty_clears(in_memory_db):
    _add_stablecoin("USDT")
    add_to_watchlist("USDT")
    res = set_watchlist([])
    assert res["removed"] == ["USDT"]
    assert watchlist_symbols() == set()


# ── API endpoints ─────────────────────────────────────────────────────────────────

def test_get_watchlist_endpoint_empty(in_memory_db):
    resp = client.get("/watchlist")
    assert resp.status_code == 200
    assert resp.json() == []


def test_post_watchlist_known_symbol(in_memory_db):
    _add_stablecoin("USDT")
    resp = client.post("/watchlist", json={"symbol": "usdt", "note": "watch peg"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "USDT"
    assert body["note"] == "watch peg"
    assert {r["symbol"] for r in client.get("/watchlist").json()} == {"USDT"}


def test_post_watchlist_unknown_symbol_404(in_memory_db):
    resp = client.post("/watchlist", json={"symbol": "FAKE"})
    assert resp.status_code == 404


def test_delete_watchlist_endpoint(in_memory_db):
    _add_stablecoin("USDT")
    client.post("/watchlist", json={"symbol": "USDT"})
    resp = client.delete("/watchlist/USDT")
    assert resp.status_code == 200
    assert resp.json()["removed"] is True
    assert client.get("/watchlist").json() == []


def test_delete_watchlist_absent_404(in_memory_db):
    resp = client.delete("/watchlist/USDT")
    assert resp.status_code == 404


def test_watchlist_endpoint_not_shadowed_by_symbol_route(in_memory_db):
    # GET /watchlist must resolve to the watchlist, not /stablecoins/{symbol}.
    _add_stablecoin("USDT")
    client.post("/watchlist", json={"symbol": "USDT"})
    rows = client.get("/watchlist").json()
    assert isinstance(rows, list)
    assert rows[0]["symbol"] == "USDT"
    assert "added_at" in rows[0]
