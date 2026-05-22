"""Tests for exchange price ingestion — mocks tracked_get, not httpx."""

import httpx
import pytest
from unittest.mock import AsyncMock, patch

import ingestion.exchanges as ex
from ingestion.exchanges import get_peg_prices, _binance_price, _coinbase_price, _binance_depth


@pytest.fixture(autouse=True)
def _reset_binance_breaker():
    """The geo-block breaker is process-level state; reset it around each test."""
    ex._binance_blocked_until = 0.0
    yield
    ex._binance_blocked_until = 0.0


def _http_451() -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://api.binance.com/api/v3/ticker/price")
    return httpx.HTTPStatusError("blocked", request=req, response=httpx.Response(451, request=req))


@pytest.mark.asyncio
async def test_binance_price_parsed():
    with patch("ingestion.exchanges.tracked_get", new_callable=AsyncMock) as m:
        m.return_value = {"symbol": "USDCUSDT", "price": "0.9998"}
        price = await _binance_price("USDCUSDT")
    assert price == pytest.approx(0.9998)


@pytest.mark.asyncio
async def test_binance_price_returns_none_on_error():
    with patch("ingestion.exchanges.tracked_get", new_callable=AsyncMock) as m:
        m.side_effect = Exception("network error")
        price = await _binance_price("USDCUSDT")
    assert price is None


@pytest.mark.asyncio
async def test_coinbase_price_parsed():
    with patch("ingestion.exchanges.tracked_get", new_callable=AsyncMock) as m:
        m.return_value = {"data": {"base": "USDC", "currency": "USD", "amount": "1.0001"}}
        price = await _coinbase_price("USDC-USD")
    assert price == pytest.approx(1.0001)


@pytest.mark.asyncio
async def test_coinbase_price_returns_none_on_error():
    with patch("ingestion.exchanges.tracked_get", new_callable=AsyncMock) as m:
        m.side_effect = Exception("rate limited")
        price = await _coinbase_price("USDC-USD")
    assert price is None


@pytest.mark.asyncio
async def test_binance_depth_parsed():
    with patch("ingestion.exchanges.tracked_get", new_callable=AsyncMock) as m:
        m.return_value = {
            "bids": [["1.0002", "10000"], ["1.0001", "5000"]],
            "asks": [["1.0003", "8000"]],
        }
        depth = await _binance_depth("USDCUSDT")
    assert depth["bid_depth_usd"] == pytest.approx(10000 * 1.0002 + 5000 * 1.0001, rel=1e-4)
    assert depth["ask_depth_usd"] == pytest.approx(8000 * 1.0003, rel=1e-4)


@pytest.mark.asyncio
async def test_get_peg_prices_uses_binance_first():
    calls: list[str] = []

    async def fake_tracked_get(provider, endpoint, url, **kwargs):
        calls.append(provider)
        if provider == "binance" and endpoint == "ticker_price":
            return {"price": "1.0002"}
        if provider == "binance" and endpoint == "order_book_depth":
            return {"bids": [], "asks": []}
        raise AssertionError(f"unexpected call: {provider}/{endpoint}")

    with patch("ingestion.exchanges.tracked_get", side_effect=fake_tracked_get):
        result = await get_peg_prices(["USDC"])

    assert result["USDC"]["price"] == pytest.approx(1.0002)
    assert result["USDC"]["peg_deviation_bps"] == pytest.approx(2.0, abs=0.1)
    assert "coinbase" not in calls  # coinbase not called when binance succeeds


@pytest.mark.asyncio
async def test_get_peg_prices_falls_back_to_coinbase():
    async def fake_tracked_get(provider, endpoint, url, **kwargs):
        if provider == "binance":
            raise Exception("binance down")
        if provider == "coinbase":
            return {"data": {"amount": "0.9999"}}
        raise AssertionError(f"unexpected: {provider}")

    with patch("ingestion.exchanges.tracked_get", side_effect=fake_tracked_get):
        result = await get_peg_prices(["USDC"])

    assert result["USDC"]["price"] == pytest.approx(0.9999)


@pytest.mark.asyncio
async def test_peg_prices_all_fail_returns_none():
    async def fake_tracked_get(**kwargs):
        raise Exception("all down")

    with patch("ingestion.exchanges.tracked_get", side_effect=fake_tracked_get):
        result = await get_peg_prices(["USDT"])

    assert result["USDT"]["price"] is None
    assert result["USDT"]["peg_deviation_bps"] is None


@pytest.mark.asyncio
async def test_coingecko_batch_fallback_when_exchanges_fail():
    """When Binance and Coinbase return nothing, fill from one CoinGecko batch call."""
    calls: list[str] = []

    async def fake_tracked_get(provider, endpoint=None, url=None, **kwargs):
        calls.append(provider)
        if provider in ("binance", "coinbase"):
            raise Exception(f"{provider} unavailable")
        if provider == "coingecko":
            # one call returns prices for all requested ids
            return {"frax": {"usd": 0.998}, "binance-usd": {"usd": 1.0}}
        raise AssertionError(f"unexpected provider: {provider}")

    with patch("ingestion.exchanges.tracked_get", side_effect=fake_tracked_get):
        result = await get_peg_prices(["FRAX", "BUSD"])

    assert result["FRAX"]["price"] == pytest.approx(0.998)
    assert result["FRAX"]["price_source"] == "coingecko"
    assert result["FRAX"]["source_type"] == "fallback"
    assert result["FRAX"]["peg_deviation_bps"] == pytest.approx(20.0, abs=0.1)
    assert result["BUSD"]["price"] == pytest.approx(1.0)
    # CoinGecko must be hit exactly once (batch), not per-symbol
    assert calls.count("coingecko") == 1


@pytest.mark.asyncio
async def test_binance_geo_block_trips_circuit_breaker():
    """After a 451, Binance is skipped on subsequent calls (no repeated hammering)."""
    binance_calls = 0

    async def fake_tracked_get(provider, endpoint=None, url=None, **kwargs):
        nonlocal binance_calls
        if provider == "binance":
            binance_calls += 1
            raise _http_451()
        if provider == "coinbase":
            return {"data": {"amount": "0.9999"}}
        raise AssertionError(f"unexpected provider: {provider}")

    with patch("ingestion.exchanges.tracked_get", side_effect=fake_tracked_get):
        r1 = await get_peg_prices(["USDT"])      # binance 451 -> trips breaker, coinbase fills
        calls_after_first = binance_calls
        r2 = await get_peg_prices(["USDC"])      # binance now skipped entirely

    assert r1["USDT"]["price"] == pytest.approx(0.9999)
    assert r2["USDC"]["price"] == pytest.approx(0.9999)
    assert calls_after_first >= 1                 # Binance was tried before the block
    assert binance_calls == calls_after_first     # ...and not called again afterward


@pytest.mark.asyncio
async def test_coingecko_not_called_when_exchange_succeeds():
    async def fake_tracked_get(provider, endpoint=None, url=None, **kwargs):
        if provider == "binance" and endpoint == "ticker_price":
            return {"price": "1.0001"}
        if provider == "binance" and endpoint == "order_book_depth":
            return {"bids": [], "asks": []}
        raise AssertionError(f"unexpected provider: {provider}")  # coingecko must NOT be called

    with patch("ingestion.exchanges.tracked_get", side_effect=fake_tracked_get):
        result = await get_peg_prices(["USDC"])

    assert result["USDC"]["price"] == pytest.approx(1.0001)
    assert result["USDC"]["price_source"] == "binance"


@pytest.mark.asyncio
async def test_perfect_peg_zero_deviation():
    async def fake_tracked_get(provider, endpoint, url, **kwargs):
        if endpoint == "ticker_price":
            return {"price": "1.0"}
        return {"bids": [], "asks": []}

    with patch("ingestion.exchanges.tracked_get", side_effect=fake_tracked_get):
        result = await get_peg_prices(["USDT"])

    assert result["USDT"]["peg_deviation_bps"] == 0.0
