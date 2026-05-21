"""Tests for the chain-concentration service and its API endpoints.

The autouse `in_memory_db` fixture (conftest.py) gives each test a fresh
in-memory SQLite shared by the service and the FastAPI client. Chain supply is
stored as DefiLlama-shaped JSON ({chain: {current: {peggedUSD: amount}}}) in
supply_snapshots.supply_by_chain, the same column the ingestion pipeline writes.
"""

import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.server import app
from db.models import Stablecoin, SupplySnapshot, get_session
from services.chain_concentration import (
    chain_concentration_ranking,
    get_chain_concentration,
)

client = TestClient(app)

NOW = datetime.utcnow().replace(microsecond=0)


# ── helpers ─────────────────────────────────────────────────────────────────────

def _chain_json(chains: dict[str, float]) -> str:
    """DefiLlama nested shape: {chain: {current: {peggedUSD: amount}}}."""
    return json.dumps({c: {"current": {"peggedUSD": amt}} for c, amt in chains.items()})


def _add_supply(symbol: str, chains: dict[str, float], *, when: datetime = NOW,
                circ: float | None = None, raw: str | None = None) -> None:
    with get_session() as s:
        s.add(SupplySnapshot(
            symbol=symbol,
            circulating_supply=circ if circ is not None else float(sum(chains.values())),
            supply_by_chain=raw if raw is not None else _chain_json(chains),
            recorded_at=when,
        ))
        s.commit()


def _register(symbol: str) -> None:
    with get_session() as s:
        s.add(Stablecoin(id=symbol.lower(), symbol=symbol, name=f"{symbol} Coin"))
        s.commit()


# ── unknown / empty ───────────────────────────────────────────────────────────────

def test_unknown_symbol_returns_none(in_memory_db):
    assert get_chain_concentration("NOPE") is None


def test_registered_symbol_no_supply(in_memory_db):
    _register("USDT")
    detail = get_chain_concentration("USDT")
    assert detail is not None
    assert detail["registered"] is True
    assert detail["concentration_level"] == "Unknown"
    assert detail["top_chain"] is None
    assert detail["top_chain_pct"] is None
    assert detail["hhi"] is None
    assert detail["warning"] is False
    assert detail["chains"] == []


def test_malformed_chain_json_is_unknown(in_memory_db):
    _add_supply("DAI", {}, circ=5_000_000, raw="{not valid json")
    detail = get_chain_concentration("DAI")
    assert detail["concentration_level"] == "Unknown"
    assert detail["chains"] == []


# ── concentration bands ─────────────────────────────────────────────────────────────

def test_single_chain_is_high_severity(in_memory_db):
    _add_supply("USDT", {"Ethereum": 10_000_000})
    d = get_chain_concentration("usdt")  # case-insensitive
    assert d["chain_count"] == 1
    assert d["top_chain"] == "Ethereum"
    assert d["top_chain_pct"] == pytest.approx(100.0)
    assert d["hhi"] == pytest.approx(10_000.0)
    assert d["concentration_level"] == "Single-chain"
    assert d["severity"] == "high"
    assert d["warning"] is True
    assert "entire supply" in d["summary"]


def test_highly_concentrated(in_memory_db):
    _add_supply("USDT", {"Tron": 8_000_000, "Ethereum": 2_000_000})  # 80 / 20
    d = get_chain_concentration("USDT")
    assert d["top_chain"] == "Tron"
    assert d["top_chain_pct"] == pytest.approx(80.0)
    assert d["hhi"] == pytest.approx(80.0 ** 2 + 20.0 ** 2)  # 6800
    assert d["concentration_level"] == "Highly concentrated"
    assert d["severity"] == "high"
    assert d["warning"] is True


def test_concentrated_is_medium_no_warning(in_memory_db):
    _add_supply("USDC", {"Ethereum": 6_000_000, "Tron": 4_000_000})  # 60 / 40
    d = get_chain_concentration("USDC")
    assert d["concentration_level"] == "Concentrated"
    assert d["severity"] == "medium"
    assert d["warning"] is False


def test_moderately_diversified_is_low(in_memory_db):
    _add_supply("DAI", {"Ethereum": 4_000_000, "Tron": 3_000_000, "BSC": 3_000_000})  # 40/30/30
    d = get_chain_concentration("DAI")
    assert d["top_chain_pct"] == pytest.approx(40.0)
    assert d["concentration_level"] == "Moderately diversified"
    assert d["severity"] == "low"


def test_diversified_is_info(in_memory_db):
    _add_supply("DAI", {"Ethereum": 3_000_000, "Tron": 3_000_000, "BSC": 2_000_000, "Solana": 2_000_000})  # 30/30/20/20
    d = get_chain_concentration("DAI")
    assert d["chain_count"] == 4
    assert d["concentration_level"] == "Diversified"
    assert d["severity"] == "info"
    assert d["warning"] is False


# ── parsing details ────────────────────────────────────────────────────────────────

def test_chains_sorted_descending_with_shares(in_memory_db):
    _add_supply("USDT", {"Ethereum": 2_000_000, "Tron": 7_000_000, "BSC": 1_000_000})
    d = get_chain_concentration("USDT")
    chains = d["chains"]
    assert [c["chain"] for c in chains] == ["Tron", "Ethereum", "BSC"]
    assert chains[0]["supply_pct"] == pytest.approx(70.0)
    assert d["total_supply"] == pytest.approx(10_000_000)


def test_zero_supply_chains_dropped(in_memory_db):
    _add_supply("USDT", {"Ethereum": 9_000_000, "Tron": 1_000_000, "Dead": 0})
    d = get_chain_concentration("USDT")
    assert d["chain_count"] == 2  # the 0-supply chain is excluded


def test_ticker_collision_picks_dominant_row(in_memory_db):
    # Two assets share the ticker at the same timestamp; the dominant (largest
    # circulating_supply) row wins, matching profile / market_changes.
    _add_supply("USDX", {"Ethereum": 10_000_000}, circ=10_000_000)          # dominant, single-chain
    _add_supply("USDX", {"Tron": 600_000, "Ethereum": 400_000}, circ=1_000_000)  # smaller, 60/40
    d = get_chain_concentration("USDX")
    assert d["top_chain"] == "Ethereum"
    assert d["concentration_level"] == "Single-chain"


def test_latest_timestamp_used(in_memory_db):
    _add_supply("USDT", {"Ethereum": 5_000_000, "Tron": 5_000_000}, when=NOW - timedelta(days=2))  # old: 50/50
    _add_supply("USDT", {"Ethereum": 9_000_000, "Tron": 1_000_000}, when=NOW)                      # new: 90/10
    d = get_chain_concentration("USDT")
    assert d["top_chain_pct"] == pytest.approx(90.0)


# ── cross-asset ranking ──────────────────────────────────────────────────────────────

def test_ranking_empty_db(in_memory_db):
    assert chain_concentration_ranking() == []


def test_ranking_orders_by_severity(in_memory_db):
    _add_supply("AAA", {"Ethereum": 10_000_000})                                          # single-chain (high)
    _add_supply("BBB", {"Ethereum": 6_000_000, "Tron": 4_000_000})                        # concentrated (medium)
    _add_supply("CCC", {"Ethereum": 3_000_000, "Tron": 3_000_000, "BSC": 2_000_000, "Sol": 2_000_000})  # diversified (info)
    ranking = chain_concentration_ranking()
    assert [r["asset"] for r in ranking] == ["AAA", "BBB", "CCC"]
    assert ranking[0]["severity"] == "high"


def test_ranking_excludes_assets_without_chain_data(in_memory_db):
    _add_supply("AAA", {"Ethereum": 10_000_000})
    _add_supply("BBB", {}, circ=5_000_000, raw="{}")        # empty breakdown -> excluded
    _add_supply("CCC", {}, circ=5_000_000, raw="garbage")   # malformed -> excluded
    assets = [r["asset"] for r in chain_concentration_ranking()]
    assert assets == ["AAA"]


def test_ranking_limit_is_honored(in_memory_db):
    for sym in ("AAA", "BBB", "CCC"):
        _add_supply(sym, {"Ethereum": 10_000_000})
    assert len(chain_concentration_ranking(limit=2)) == 2


def test_ranking_tiebreak_by_top_chain_share(in_memory_db):
    # Both high severity; the more concentrated single chain ranks first.
    _add_supply("AAA", {"Ethereum": 8_000_000, "Tron": 2_000_000})  # 80%, high
    _add_supply("BBB", {"Ethereum": 9_000_000, "Tron": 1_000_000})  # 90%, high
    ranking = chain_concentration_ranking()
    assert [r["asset"] for r in ranking] == ["BBB", "AAA"]


# ── API endpoints ────────────────────────────────────────────────────────────────

def test_chain_supply_endpoint_unknown_symbol_404(in_memory_db):
    resp = client.get("/stablecoins/NOPE/chain-supply")
    assert resp.status_code == 404


def test_chain_supply_endpoint_returns_structure(in_memory_db):
    _add_supply("USDT", {"Tron": 8_000_000, "Ethereum": 2_000_000})
    resp = client.get("/stablecoins/USDT/chain-supply")
    assert resp.status_code == 200
    body = resp.json()
    assert {"symbol", "top_chain", "top_chain_pct", "hhi", "concentration_level", "chains"} <= set(body)
    assert body["top_chain"] == "Tron"
    assert body["warning"] is True


def test_chain_concentration_endpoint_empty_db(in_memory_db):
    resp = client.get("/stablecoins/chain-concentration")
    assert resp.status_code == 200
    assert resp.json() == []


def test_chain_concentration_endpoint_not_shadowed_by_symbol_route(in_memory_db):
    # /stablecoins/chain-concentration must resolve to the ranking endpoint, not
    # the {symbol} lookup (which would 404 for an unknown symbol).
    _add_supply("AAA", {"Ethereum": 10_000_000})
    resp = client.get("/stablecoins/chain-concentration")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert body[0]["asset"] == "AAA"


def test_chain_concentration_endpoint_limit_validation(in_memory_db):
    resp = client.get("/stablecoins/chain-concentration?limit=500")
    assert resp.status_code == 422  # exceeds le=200
