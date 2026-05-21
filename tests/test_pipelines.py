"""Integration tests for all five ingestion pipelines.

Each test uses the autouse `in_memory_db` fixture (see conftest.py) so no
real SQLite file is touched, and patches external API calls so no HTTP
requests are made.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from db.models import (
    PriceSnapshot,
    ReserveReport,
    RiskScore,
    Stablecoin,
    SupplySnapshot,
    get_session,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_asset(
    symbol: str = "USDT",
    asset_id: str = "tether",
    name: str = "Tether",
    peg_mechanism: str = "fiat-backed",
    price: float = 1.0,
    supply_usd: float = 1_000_000_000,
) -> dict:
    return {
        "id": asset_id,
        "symbol": symbol,
        "name": name,
        "pegMechanism": peg_mechanism,
        "price": price,
        "circulating": {"peggedUSD": supply_usd},
        "chainCirculating": {
            "Ethereum": {"current": {"peggedUSD": supply_usd * 0.6}},
            "Tron":     {"current": {"peggedUSD": supply_usd * 0.4}},
        },
    }


def _make_price_data(
    price: float = 1.0002,
    bps: float = 2.0,
    bid: float = 5_000_000,
    ask: float = 8_000_000,
) -> dict:
    return {"price": price, "peg_deviation_bps": bps, "bid_depth_usd": bid, "ask_depth_usd": ask}


def _seed_stablecoin(symbol: str = "USDT") -> None:
    with get_session() as s:
        s.add(Stablecoin(id=symbol.lower(), symbol=symbol, name=symbol, issuer="fiat-backed"))
        s.commit()


# ── update_supply ──────────────────────────────────────────────────────────────

async def test_supply_inserts_stablecoin(in_memory_db):
    from pipelines.update_supply import _fetch_and_store

    with patch("pipelines.update_supply.get_stablecoins", new_callable=AsyncMock,
               return_value=[_make_asset()]):
        await _fetch_and_store()

    with get_session() as s:
        coins = s.execute(select(Stablecoin)).scalars().all()
    assert len(coins) == 1
    assert coins[0].symbol == "USDT"
    assert coins[0].issuer == "fiat-backed"


async def test_supply_inserts_snapshot(in_memory_db):
    from pipelines.update_supply import _fetch_and_store

    with patch("pipelines.update_supply.get_stablecoins", new_callable=AsyncMock,
               return_value=[_make_asset(supply_usd=2_000_000_000)]):
        await _fetch_and_store()

    with get_session() as s:
        snaps = s.execute(select(SupplySnapshot)).scalars().all()
    assert len(snaps) == 1
    assert snaps[0].circulating_supply == 2_000_000_000


async def test_supply_upserts_stablecoin_on_repeat(in_memory_db):
    """Second run must produce one Stablecoin row but two supply snapshots."""
    from pipelines.update_supply import _fetch_and_store

    asset = _make_asset()
    with patch("pipelines.update_supply.get_stablecoins", new_callable=AsyncMock,
               return_value=[asset]):
        await _fetch_and_store()
        await _fetch_and_store()

    with get_session() as s:
        coins = s.execute(select(Stablecoin)).scalars().all()
        snaps = s.execute(select(SupplySnapshot)).scalars().all()
    assert len(coins) == 1
    assert len(snaps) == 2


async def test_supply_skips_asset_without_symbol(in_memory_db):
    from pipelines.update_supply import _fetch_and_store

    bad = _make_asset()
    bad.pop("symbol")
    with patch("pipelines.update_supply.get_stablecoins", new_callable=AsyncMock,
               return_value=[bad]):
        await _fetch_and_store()

    with get_session() as s:
        assert s.execute(select(Stablecoin)).scalars().first() is None


async def test_supply_skips_asset_without_id(in_memory_db):
    from pipelines.update_supply import _fetch_and_store

    bad = _make_asset()
    bad.pop("id")
    with patch("pipelines.update_supply.get_stablecoins", new_callable=AsyncMock,
               return_value=[bad]):
        await _fetch_and_store()

    with get_session() as s:
        assert s.execute(select(Stablecoin)).scalars().first() is None


async def test_supply_skips_zero_circulating(in_memory_db):
    """Zero supply should upsert the Stablecoin but not create a snapshot."""
    from pipelines.update_supply import _fetch_and_store

    with patch("pipelines.update_supply.get_stablecoins", new_callable=AsyncMock,
               return_value=[_make_asset(supply_usd=0)]):
        await _fetch_and_store()

    with get_session() as s:
        coins = s.execute(select(Stablecoin)).scalars().all()
        snaps = s.execute(select(SupplySnapshot)).scalars().all()
    assert len(coins) == 1
    assert len(snaps) == 0


async def test_supply_stores_chain_breakdown(in_memory_db):
    from pipelines.update_supply import _fetch_and_store

    with patch("pipelines.update_supply.get_stablecoins", new_callable=AsyncMock,
               return_value=[_make_asset()]):
        await _fetch_and_store()

    with get_session() as s:
        snap = s.execute(select(SupplySnapshot)).scalars().first()
    chains = json.loads(snap.supply_by_chain)
    assert "Ethereum" in chains


# ── update_prices ──────────────────────────────────────────────────────────────

async def test_prices_inserts_snapshot(in_memory_db):
    from pipelines.update_prices import _fetch_and_store

    with patch("pipelines.update_prices.get_peg_prices", new_callable=AsyncMock,
               return_value={"USDT": _make_price_data()}):
        await _fetch_and_store()

    with get_session() as s:
        rows = s.execute(select(PriceSnapshot)).scalars().all()
    assert len(rows) == 1
    assert rows[0].symbol == "USDT"
    assert rows[0].price == pytest.approx(1.0002)
    assert rows[0].source == "binance"


async def test_prices_skips_none_price(in_memory_db):
    from pipelines.update_prices import _fetch_and_store

    with patch("pipelines.update_prices.get_peg_prices", new_callable=AsyncMock,
               return_value={"USDT": {"price": None, "peg_deviation_bps": None}}):
        await _fetch_and_store()

    with get_session() as s:
        assert s.execute(select(PriceSnapshot)).scalars().first() is None


async def test_prices_targeted_symbols(in_memory_db):
    from pipelines.update_prices import _fetch_and_store

    with patch("pipelines.update_prices.get_peg_prices", new_callable=AsyncMock,
               return_value={"USDC": _make_price_data(price=1.0001, bps=1.0)}) as mock:
        await _fetch_and_store(symbols=["USDC"])
        mock.assert_called_once_with(["USDC"])

    with get_session() as s:
        rows = s.execute(select(PriceSnapshot)).scalars().all()
    assert len(rows) == 1
    assert rows[0].symbol == "USDC"


async def test_prices_stores_depth_columns(in_memory_db):
    from pipelines.update_prices import _fetch_and_store

    with patch("pipelines.update_prices.get_peg_prices", new_callable=AsyncMock,
               return_value={"USDT": _make_price_data(bid=3_000_000, ask=4_000_000)}):
        await _fetch_and_store()

    with get_session() as s:
        row = s.execute(select(PriceSnapshot)).scalars().first()
    assert row.bid_depth_usd == 3_000_000
    assert row.ask_depth_usd == 4_000_000


# ── update_liquidity ───────────────────────────────────────────────────────────

def test_liquidity_inserts_snapshot(in_memory_db):
    from pipelines.update_liquidity import run

    with patch("pipelines.update_liquidity.get_peg_prices", new_callable=AsyncMock,
               return_value={"USDT": _make_price_data()}):
        run()

    with get_session() as s:
        rows = s.execute(select(PriceSnapshot)).scalars().all()
    assert len(rows) == 1
    assert rows[0].source == "exchanges_depth"


def test_liquidity_skips_symbol_with_no_depth(in_memory_db):
    from pipelines.update_liquidity import run

    with patch("pipelines.update_liquidity.get_peg_prices", new_callable=AsyncMock,
               return_value={"USDT": {"price": 1.0, "bid_depth_usd": None, "ask_depth_usd": None}}):
        run()

    with get_session() as s:
        assert s.execute(select(PriceSnapshot)).scalars().first() is None


def test_liquidity_partial_depth_is_kept(in_memory_db):
    """A symbol with only bid depth (no ask) should still be stored."""
    from pipelines.update_liquidity import run

    with patch("pipelines.update_liquidity.get_peg_prices", new_callable=AsyncMock,
               return_value={"USDT": {"price": 1.0, "bid_depth_usd": 1_000_000, "ask_depth_usd": None}}):
        run()

    with get_session() as s:
        row = s.execute(select(PriceSnapshot)).scalars().first()
    assert row is not None
    assert row.bid_depth_usd == 1_000_000


# ── update_reserves ────────────────────────────────────────────────────────────

def test_reserves_inserts_all_known_reports(in_memory_db):
    from pipelines.update_reserves import run
    run()

    with get_session() as s:
        rows = s.execute(select(ReserveReport)).scalars().all()
    symbols = {r.symbol for r in rows}
    assert {"USDT", "USDC", "DAI"} == symbols


def test_reserves_usdt_has_auditor_and_date(in_memory_db):
    from pipelines.update_reserves import run
    run()

    with get_session() as s:
        usdt = s.execute(
            select(ReserveReport).where(ReserveReport.symbol == "USDT")
        ).scalars().first()
    assert usdt.auditor == "BDO"
    from datetime import date
    assert usdt.report_date == date(2025, 4, 1)


def test_reserves_dai_has_null_date_and_no_auditor(in_memory_db):
    from pipelines.update_reserves import run
    run()

    with get_session() as s:
        dai = s.execute(
            select(ReserveReport).where(ReserveReport.symbol == "DAI")
        ).scalars().first()
    assert dai.report_date is None
    assert dai.auditor is None


def test_reserves_composition_is_valid_json(in_memory_db):
    from pipelines.update_reserves import run
    run()

    with get_session() as s:
        rows = s.execute(select(ReserveReport)).scalars().all()
    for row in rows:
        composition = json.loads(row.composition)
        assert isinstance(composition, dict)
        assert composition  # non-empty


# ── score_stablecoins ──────────────────────────────────────────────────────────

def test_scoring_happy_path(in_memory_db):
    """Full data present → score is stored and overall is a weighted average."""
    from pipelines.score_stablecoins import run

    now = datetime.utcnow()
    with get_session() as s:
        s.add(Stablecoin(id="usdt", symbol="USDT", name="Tether", issuer="fiat-backed"))
        s.add(SupplySnapshot(symbol="USDT", circulating_supply=50_000_000_000, recorded_at=now))
        s.add(PriceSnapshot(
            symbol="USDT", price=1.0001, peg_deviation_bps=1.0,
            bid_depth_usd=30_000_000, ask_depth_usd=20_000_000,
            source="binance", recorded_at=now,
        ))
        s.add(ReserveReport(
            symbol="USDT", report_url="https://example.com",
            report_date=now.date(), auditor="BDO",
            composition='{"US_Treasuries": 0.84}',
        ))
        s.commit()

    run()

    with get_session() as s:
        score = s.execute(select(RiskScore).where(RiskScore.symbol == "USDT")).scalars().first()
    assert score is not None
    assert 0 <= score.overall_score <= 100
    # Peg 35% + Liquidity 25% + Reserve 25% + Adoption 15% must equal overall
    expected = round(
        score.peg_score * 0.35 + score.liquidity_score * 0.25 +
        score.reserve_score * 0.25 + score.adoption_score * 0.15, 2
    )
    assert score.overall_score == pytest.approx(expected, abs=0.01)


def test_scoring_defaults_when_no_snapshots(in_memory_db):
    """Stablecoin with no price/supply/reserve data gets neutral default scores."""
    from pipelines.score_stablecoins import run

    with get_session() as s:
        s.add(Stablecoin(id="foo", symbol="FOO", name="Foo", issuer=None))
        s.commit()

    run()

    with get_session() as s:
        score = s.execute(select(RiskScore).where(RiskScore.symbol == "FOO")).scalars().first()
    assert score is not None
    assert score.peg_score == pytest.approx(50.0)       # neutral: no price
    assert score.liquidity_score == pytest.approx(50.0) # neutral: no depth
    assert score.reserve_score == pytest.approx(20.0)   # low: no report
    assert score.adoption_score == pytest.approx(0.0)   # zero: no supply


def test_scoring_ignores_price_older_than_two_hours(in_memory_db):
    """A price snapshot older than 2 hours must not affect the peg score."""
    from pipelines.score_stablecoins import run

    stale_ts = datetime.utcnow() - timedelta(hours=3)
    with get_session() as s:
        s.add(Stablecoin(id="usdc", symbol="USDC", name="USD Coin", issuer="fiat-backed"))
        s.add(PriceSnapshot(
            symbol="USDC", price=0.95, peg_deviation_bps=500.0,
            bid_depth_usd=None, ask_depth_usd=None,
            source="binance", recorded_at=stale_ts,
        ))
        s.commit()

    run()

    with get_session() as s:
        score = s.execute(select(RiskScore).where(RiskScore.symbol == "USDC")).scalars().first()
    # Stale price not used → falls back to neutral 50
    assert score.peg_score == pytest.approx(50.0)


def test_scoring_no_stablecoins_produces_no_scores(in_memory_db):
    from pipelines.score_stablecoins import run
    run()

    with get_session() as s:
        assert s.execute(select(RiskScore)).scalars().first() is None


def test_scoring_multiple_symbols(in_memory_db):
    """Scorer processes all symbols in the Stablecoin table."""
    from pipelines.score_stablecoins import run

    with get_session() as s:
        for sym in ("USDT", "USDC", "DAI"):
            s.add(Stablecoin(id=sym.lower(), symbol=sym, name=sym, issuer=None))
        s.commit()

    run()

    with get_session() as s:
        scores = s.execute(select(RiskScore)).scalars().all()
    assert {sc.symbol for sc in scores} == {"USDT", "USDC", "DAI"}
