"""Tests for the FastAPI server endpoints (app/api/server.py).

The autouse `in_memory_db` fixture (conftest.py) patches db.models.engine so
every endpoint hits the same in-memory SQLite that the test populates.
"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.api.server import app
from db.models import (
    ApiRequestLog,
    PriceSnapshot,
    RiskScore,
    Stablecoin,
    get_session,
)

client = TestClient(app)


# ── helpers ────────────────────────────────────────────────────────────────────

def _add_stablecoin(symbol: str = "USDT", name: str = "Tether") -> None:
    with get_session() as s:
        s.add(Stablecoin(id=symbol.lower(), symbol=symbol, name=name, issuer="fiat-backed"))
        s.commit()


def _add_risk_score(symbol: str = "USDT", overall: float = 85.0) -> None:
    with get_session() as s:
        s.add(RiskScore(
            symbol=symbol,
            peg_score=95.0, liquidity_score=80.0,
            reserve_score=75.0, adoption_score=70.0,
            overall_score=overall, scored_at=datetime.utcnow(),
        ))
        s.commit()


def _add_price_snapshot(symbol: str = "USDT", price: float = 1.0001) -> None:
    with get_session() as s:
        s.add(PriceSnapshot(
            symbol=symbol, price=price, peg_deviation_bps=1.0,
            source="binance", recorded_at=datetime.utcnow(),
        ))
        s.commit()


# ── /health ────────────────────────────────────────────────────────────────────

def test_health_returns_ok(in_memory_db):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ── /stablecoins ───────────────────────────────────────────────────────────────

def test_list_stablecoins_empty_db(in_memory_db):
    resp = client.get("/stablecoins")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_stablecoins_returns_all(in_memory_db):
    for sym in ("USDT", "USDC", "DAI"):
        _add_stablecoin(sym)
    resp = client.get("/stablecoins")
    assert resp.status_code == 200
    symbols = {row["symbol"] for row in resp.json()}
    assert symbols == {"USDT", "USDC", "DAI"}


def test_list_stablecoins_limit_honored(in_memory_db):
    for i in range(5):
        _add_stablecoin(f"TOK{i}", name=f"Token {i}")
    resp = client.get("/stablecoins?limit=2")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_list_stablecoins_response_shape(in_memory_db):
    _add_stablecoin("USDT")
    rows = client.get("/stablecoins").json()
    assert len(rows) == 1
    keys = set(rows[0].keys())
    assert {"id", "symbol", "name", "issuer"} <= keys


# ── /stablecoins/{symbol} ──────────────────────────────────────────────────────

def test_get_stablecoin_found(in_memory_db):
    _add_stablecoin("USDT")
    resp = client.get("/stablecoins/USDT")
    assert resp.status_code == 200
    assert resp.json()["symbol"] == "USDT"


def test_get_stablecoin_not_found(in_memory_db):
    resp = client.get("/stablecoins/FAKE")
    assert resp.status_code == 404


def test_get_stablecoin_lookup_is_case_insensitive(in_memory_db):
    _add_stablecoin("USDT")
    resp = client.get("/stablecoins/usdt")
    assert resp.status_code == 200
    assert resp.json()["symbol"] == "USDT"


# ── /stablecoins/{symbol}/scores ───────────────────────────────────────────────

def test_get_scores_returns_latest(in_memory_db):
    _add_stablecoin("USDT")
    _add_risk_score("USDT", overall=82.5)
    resp = client.get("/stablecoins/USDT/scores")
    assert resp.status_code == 200
    data = resp.json()
    assert data["symbol"] == "USDT"
    assert data["overall_score"] == pytest.approx(82.5)


def test_get_scores_not_found(in_memory_db):
    _add_stablecoin("USDT")
    resp = client.get("/stablecoins/USDT/scores")
    assert resp.status_code == 404


def test_get_scores_all_fields_present(in_memory_db):
    _add_stablecoin("USDT")
    _add_risk_score("USDT")
    data = client.get("/stablecoins/USDT/scores").json()
    assert {"peg_score", "liquidity_score", "reserve_score", "adoption_score", "overall_score"} <= set(data)


# ── /stablecoins/{symbol}/prices ───────────────────────────────────────────────

def test_get_prices_returns_snapshots(in_memory_db):
    _add_stablecoin("USDT")
    _add_price_snapshot("USDT", price=1.0001)
    _add_price_snapshot("USDT", price=1.0002)
    resp = client.get("/stablecoins/USDT/prices")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_get_prices_empty_when_no_data(in_memory_db):
    _add_stablecoin("USDT")
    resp = client.get("/stablecoins/USDT/prices")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_prices_limit_honored(in_memory_db):
    _add_stablecoin("USDT")
    for _ in range(10):
        _add_price_snapshot("USDT")
    resp = client.get("/stablecoins/USDT/prices?limit=3")
    assert resp.status_code == 200
    assert len(resp.json()) == 3


def test_get_prices_response_shape(in_memory_db):
    _add_stablecoin("USDT")
    _add_price_snapshot("USDT")
    rows = client.get("/stablecoins/USDT/prices").json()
    keys = set(rows[0].keys())
    assert {"symbol", "price", "peg_deviation_bps", "source", "recorded_at"} <= keys


# ── /providers/usage ───────────────────────────────────────────────────────────

def test_provider_usage_empty_db(in_memory_db):
    resp = client.get("/providers/usage")
    assert resp.status_code == 200
    assert resp.json() == {}


def test_provider_usage_counts_by_provider(in_memory_db):
    now = datetime.utcnow()
    with get_session() as s:
        for _ in range(3):
            s.add(ApiRequestLog(
                provider="defillama", endpoint="stablecoins",
                url="https://stablecoins.llama.fi/stablecoins",
                status_code=200, requested_at=now,
            ))
        s.add(ApiRequestLog(
            provider="binance", endpoint="ticker_price",
            url="https://api.binance.com/api/v3/ticker/price",
            status_code=200, requested_at=now,
        ))
        s.commit()

    resp = client.get("/providers/usage")
    assert resp.status_code == 200
    data = resp.json()
    assert data["defillama"] == 3
    assert data["binance"] == 1
