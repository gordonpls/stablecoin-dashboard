"""Tests for exchange price ingestion — mocks tracked_get, not httpx."""

import pytest
from unittest.mock import AsyncMock, patch

from ingestion.exchanges import get_peg_prices, _binance_price, _coinbase_price, _binance_depth


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
async def test_perfect_peg_zero_deviation():
    async def fake_tracked_get(provider, endpoint, url, **kwargs):
        if endpoint == "ticker_price":
            return {"price": "1.0"}
        return {"bids": [], "asks": []}

    with patch("ingestion.exchanges.tracked_get", side_effect=fake_tracked_get):
        result = await get_peg_prices(["USDT"])

    assert result["USDT"]["peg_deviation_bps"] == 0.0
