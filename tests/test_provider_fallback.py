"""Tests for provider fallback status — ingestion provenance, the service,
the pipeline source fix, and the FastAPI endpoint.

The autouse `in_memory_db` fixture (conftest.py) gives each test a fresh
in-memory SQLite shared by the service and the FastAPI client. Ingestion-only
tests patch `tracked_get` so no real HTTP is made.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from app.api.server import app
from db.models import PriceSnapshot, ProviderFallbackEvent, get_session
from ingestion.exchanges import get_peg_prices
from services.provider_fallback import (
    get_fallback_status,
    query_fallback_events,
    record_fallback_events,
)

client = TestClient(app)

NOW = datetime.utcnow().replace(microsecond=0)


# ── helpers ──────────────────────────────────────────────────────────────────

def _add_price(symbol: str, source: str, *, when: datetime = NOW, price: float = 1.0) -> None:
    with get_session() as s:
        s.add(PriceSnapshot(
            symbol=symbol, price=price, peg_deviation_bps=0.0,
            bid_depth_usd=None, ask_depth_usd=None,
            source=source, recorded_at=when,
        ))
        s.commit()


def _asset(status: dict, symbol: str) -> dict:
    return next(a for a in status["assets"] if a["asset"] == symbol)


# ── ingestion provenance ─────────────────────────────────────────────────────

async def test_peg_prices_primary_provenance():
    async def fake(provider, endpoint, url, **kwargs):
        if provider == "binance" and endpoint == "ticker_price":
            return {"price": "1.0001"}
        if provider == "binance" and endpoint == "order_book_depth":
            return {"bids": [], "asks": []}
        raise AssertionError(f"unexpected {provider}/{endpoint}")

    with patch("ingestion.exchanges.tracked_get", side_effect=fake):
        out = await get_peg_prices(["USDC"])

    d = out["USDC"]
    assert d["price_source"] == "binance"
    assert d["source_type"] == "primary"
    assert d["fallback_used"] is False
    assert d["fallback_reason"] is None


async def test_peg_prices_fallback_provenance():
    async def fake(provider, endpoint, url, **kwargs):
        if provider == "binance":
            raise Exception("binance down")
        if provider == "coinbase":
            return {"data": {"amount": "0.9999"}}
        raise AssertionError(f"unexpected {provider}")

    with patch("ingestion.exchanges.tracked_get", side_effect=fake):
        out = await get_peg_prices(["USDC"])

    d = out["USDC"]
    assert d["price"] == pytest.approx(0.9999)
    assert d["price_source"] == "coinbase"
    assert d["source_type"] == "fallback"
    assert d["fallback_used"] is True
    assert "Binance" in d["fallback_reason"]


async def test_peg_prices_unavailable_no_fallback_configured():
    # FRAX has no Coinbase pair; if Binance fails it is simply unavailable.
    async def fake(provider, endpoint, url, **kwargs):
        raise Exception("down")

    with patch("ingestion.exchanges.tracked_get", side_effect=fake):
        out = await get_peg_prices(["FRAX"])

    d = out["FRAX"]
    assert d["price"] is None
    assert d["price_source"] is None
    assert d["source_type"] == "unavailable"
    assert d["fallback_used"] is False
    assert "no Coinbase fallback configured" in d["fallback_reason"]


async def test_peg_prices_unavailable_both_fail():
    async def fake(provider, endpoint, url, **kwargs):
        raise Exception("down")

    with patch("ingestion.exchanges.tracked_get", side_effect=fake):
        out = await get_peg_prices(["USDC"])  # has a coinbase pair

    d = out["USDC"]
    assert d["source_type"] == "unavailable"
    assert "Binance request failed" in d["fallback_reason"]
    assert "Coinbase request failed" in d["fallback_reason"]


# ── record_fallback_events ───────────────────────────────────────────────────

def test_record_only_logs_exceptional_outcomes(in_memory_db):
    price_data = {
        "USDT": {"source_type": "primary", "price_source": "binance", "fallback_reason": None},
        "USDC": {"source_type": "fallback", "price_source": "coinbase",
                 "fallback_reason": "Binance request failed"},
        "FRAX": {"source_type": "unavailable", "price_source": None,
                 "fallback_reason": "Binance request failed; no Coinbase fallback configured"},
    }
    inserted = record_fallback_events(price_data, recorded_at=NOW)
    assert inserted == 2  # primary not logged

    with get_session() as s:
        rows = s.execute(sa.select(ProviderFallbackEvent)).scalars().all()
    symbols = {r.symbol for r in rows}
    assert symbols == {"USDC", "FRAX"}
    usdc = next(r for r in rows if r.symbol == "USDC")
    assert usdc.source_provider == "coinbase"
    assert usdc.primary_provider == "binance"
    assert usdc.fallback_provider == "coinbase"


def test_record_is_idempotent_for_same_run(in_memory_db):
    price_data = {
        "USDC": {"source_type": "fallback", "price_source": "coinbase",
                 "fallback_reason": "Binance request failed"},
    }
    assert record_fallback_events(price_data, recorded_at=NOW) == 1
    assert record_fallback_events(price_data, recorded_at=NOW) == 0  # no dup
    assert len(query_fallback_events()) == 1


def test_record_empty_when_all_primary(in_memory_db):
    price_data = {"USDT": {"source_type": "primary", "price_source": "binance"}}
    assert record_fallback_events(price_data, recorded_at=NOW) == 0


# ── get_fallback_status ──────────────────────────────────────────────────────

def test_status_empty_db_shape(in_memory_db):
    st = get_fallback_status()
    assert st["primary_provider"] == "binance"
    assert st["fallback_provider"] == "coinbase"
    assert st["summary"]["total_price_points"] == 0
    assert st["summary"]["fallback_rate"] is None
    assert st["summary"]["primary_status"] == "unknown"
    assert st["summary"]["currently_on_fallback"] is False
    assert st["assets"] == []
    assert st["recent_events"] == []


def test_status_all_primary_is_healthy(in_memory_db):
    _add_price("USDT", "binance")
    _add_price("USDC", "binance")
    st = get_fallback_status()
    assert st["summary"]["primary_status"] == "healthy"
    assert st["summary"]["fallback_rate"] == pytest.approx(0.0)
    assert st["summary"]["currently_on_fallback"] is False


def test_status_excludes_liquidity_depth_rows(in_memory_db):
    # update_liquidity writes source="exchanges_depth"; those are not a price
    # provider choice and must not count toward the primary/fallback rate.
    _add_price("USDT", "binance")
    _add_price("USDT", "exchanges_depth")
    st = get_fallback_status()
    assert st["summary"]["total_price_points"] == 1


def test_status_fallback_rate_and_degraded(in_memory_db):
    # 3 primary, 1 fallback → 25% fallback, below the failing threshold.
    _add_price("USDT", "binance")
    _add_price("USDC", "binance")
    _add_price("DAI", "binance")
    _add_price("TUSD", "coinbase")
    st = get_fallback_status()
    assert st["summary"]["fallback_points"] == 1
    assert st["summary"]["fallback_rate"] == pytest.approx(25.0)
    assert st["summary"]["primary_status"] == "degraded"
    assert st["summary"]["currently_on_fallback"] is True
    assert st["summary"]["assets_on_fallback"] == ["TUSD"]


def test_status_high_fallback_rate_is_failing(in_memory_db):
    _add_price("USDT", "coinbase")
    _add_price("USDC", "coinbase")
    _add_price("DAI", "binance")
    st = get_fallback_status()
    assert st["summary"]["fallback_rate"] == pytest.approx(66.67, abs=0.01)
    assert st["summary"]["primary_status"] == "failing"


def test_status_unavailable_event_forces_failing(in_memory_db):
    # Even a single primary point is "failing" if a price was unavailable.
    _add_price("USDT", "binance")
    record_fallback_events(
        {"FRAX": {"source_type": "unavailable", "price_source": None,
                  "fallback_reason": "Binance request failed; no Coinbase fallback configured"}},
        recorded_at=NOW,
    )
    st = get_fallback_status()
    assert st["summary"]["unavailable_events"] == 1
    assert st["summary"]["primary_status"] == "failing"


def test_status_latest_source_reflects_current_not_window(in_memory_db):
    # Asset fell back yesterday but is back on primary now → not on fallback.
    _add_price("USDT", "coinbase", when=NOW - timedelta(hours=20))
    _add_price("USDT", "binance", when=NOW)
    st = get_fallback_status()
    usdt = _asset(st, "USDT")
    assert usdt["latest_source"] == "binance"
    assert usdt["on_fallback"] is False
    # but the window still counted the earlier fallback point
    assert usdt["fallback_points"] == 1
    assert usdt["primary_points"] == 1


def test_status_window_excludes_old_points(in_memory_db):
    _add_price("USDT", "binance", when=NOW)
    _add_price("USDC", "coinbase", when=NOW - timedelta(hours=48))  # outside 24h window
    st = get_fallback_status(window_hours=24)
    assert st["summary"]["total_price_points"] == 1
    assert st["summary"]["fallback_points"] == 0


def test_status_per_asset_last_fallback(in_memory_db):
    _add_price("USDC", "coinbase")
    record_fallback_events(
        {"USDC": {"source_type": "fallback", "price_source": "coinbase",
                  "fallback_reason": "Binance request failed"}},
        recorded_at=NOW,
    )
    st = get_fallback_status()
    usdc = _asset(st, "USDC")
    assert usdc["last_fallback_at"] is not None
    assert "Binance" in (usdc["last_fallback_reason"] or "")


# ── pipeline records the real source (the bug fix) ───────────────────────────

async def test_pipeline_stores_real_fallback_source(in_memory_db):
    from pipelines.update_prices import _fetch_and_store

    fallback_data = {
        "USDC": {
            "price": 0.9999, "peg_deviation_bps": 1.0,
            "bid_depth_usd": None, "ask_depth_usd": None,
            "price_source": "coinbase", "source_type": "fallback",
            "fallback_used": True, "fallback_reason": "Binance request failed",
        }
    }
    with patch("pipelines.update_prices.get_peg_prices", new_callable=AsyncMock,
               return_value=fallback_data):
        await _fetch_and_store()

    with get_session() as s:
        snap = s.execute(sa.select(PriceSnapshot)).scalars().first()
        ev = s.execute(sa.select(ProviderFallbackEvent)).scalars().first()
    assert snap.source == "coinbase"          # NOT hard-coded "binance"
    assert ev is not None and ev.symbol == "USDC"
    assert ev.source_type == "fallback"


# ── endpoint ─────────────────────────────────────────────────────────────────

def test_endpoint_empty_db(in_memory_db):
    resp = client.get("/provider-fallback")
    assert resp.status_code == 200
    body = resp.json()
    assert body["primary_provider"] == "binance"
    assert body["summary"]["total_price_points"] == 0
    assert body["assets"] == []


def test_endpoint_structure_with_data(in_memory_db):
    _add_price("USDT", "binance")
    _add_price("USDC", "coinbase")
    resp = client.get("/provider-fallback")
    assert resp.status_code == 200
    body = resp.json()
    assert {"summary", "assets", "recent_events", "primary_provider"} <= set(body)
    assert body["summary"]["currently_on_fallback"] is True
    assert "USDC" in body["summary"]["assets_on_fallback"]


def test_endpoint_window_param(in_memory_db):
    resp = client.get("/provider-fallback?window_hours=48")
    assert resp.status_code == 200
    assert resp.json()["window_hours"] == 48


def test_endpoint_rejects_bad_window(in_memory_db):
    assert client.get("/provider-fallback?window_hours=0").status_code == 422
    assert client.get("/provider-fallback?window_hours=1000").status_code == 422


def test_endpoint_rejects_excessive_limit(in_memory_db):
    assert client.get("/provider-fallback?recent_limit=999").status_code == 422
