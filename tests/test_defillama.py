"""Tests for DefiLlama ingestion — mocks tracked_get, not httpx."""

import pytest
from unittest.mock import AsyncMock, patch

from ingestion.defillama import get_stablecoins, get_stablecoin_charts, parse_supply

MOCK_ASSET = {
    "id": "1",
    "symbol": "USDT",
    "name": "Tether",
    "pegMechanism": "fiat-backed",
    "price": 1.0001,
    "circulating": {"peggedUSD": 83_000_000_000},
    "chainCirculating": {
        "Ethereum": {"current": {"peggedUSD": 45e9}},
        "Tron":     {"current": {"peggedUSD": 38e9}},
    },
}


@pytest.mark.asyncio
async def test_get_stablecoins_returns_list():
    with patch("ingestion.defillama.tracked_get", new_callable=AsyncMock) as m:
        m.return_value = {"peggedAssets": [MOCK_ASSET]}
        result = await get_stablecoins()

    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["symbol"] == "USDT"
    m.assert_awaited_once()
    _, kwargs = m.call_args
    assert kwargs["provider"] == "defillama"
    assert kwargs["endpoint"] == "stablecoins"


@pytest.mark.asyncio
async def test_get_stablecoins_empty_response():
    with patch("ingestion.defillama.tracked_get", new_callable=AsyncMock) as m:
        m.return_value = {}
        result = await get_stablecoins()
    assert result == []


@pytest.mark.asyncio
async def test_get_stablecoin_charts_list_response():
    history = [{"date": "1700000000", "totalCirculating": 80e9}]
    with patch("ingestion.defillama.tracked_get", new_callable=AsyncMock) as m:
        m.return_value = history
        result = await get_stablecoin_charts("1")
    assert result == history


@pytest.mark.asyncio
async def test_get_stablecoin_charts_dict_response():
    history = [{"date": "1700000000", "totalCirculating": 80e9}]
    with patch("ingestion.defillama.tracked_get", new_callable=AsyncMock) as m:
        m.return_value = {"totalCirculating": history}
        result = await get_stablecoin_charts("1")
    assert result == history


def test_parse_supply_core_fields():
    parsed = parse_supply(MOCK_ASSET)
    assert parsed["symbol"] == "USDT"
    assert parsed["name"] == "Tether"
    assert parsed["circulating_supply"] == 83_000_000_000
    assert parsed["peg_deviation_bps"] == pytest.approx(1.0, abs=0.1)
    assert "Ethereum" in parsed["chains"]
    assert "Tron" in parsed["chains"]


def test_parse_supply_missing_price_gives_none_deviation():
    parsed = parse_supply({**MOCK_ASSET, "price": None})
    assert parsed["peg_deviation_bps"] is None


def test_parse_supply_perfect_peg():
    parsed = parse_supply({**MOCK_ASSET, "price": 1.0})
    assert parsed["peg_deviation_bps"] == 0.0


def test_parse_supply_missing_circulating():
    parsed = parse_supply({**MOCK_ASSET, "circulating": {}})
    assert parsed["circulating_supply"] == 0


@pytest.mark.asyncio
async def test_get_stablecoins_propagates_exception():
    with patch("ingestion.defillama.tracked_get", new_callable=AsyncMock) as m:
        import httpx
        m.side_effect = httpx.HTTPStatusError("503", request=None, response=None)
        with pytest.raises(httpx.HTTPStatusError):
            await get_stablecoins()
