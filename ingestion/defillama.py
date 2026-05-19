"""DefiLlama ingestion — stablecoin supply, chain breakdown.

All HTTP calls go through core.http.tracked_get.
"""

from typing import Any

from core.http import tracked_get

BASE = "https://stablecoins.llama.fi"
PROVIDER = "defillama"


async def get_stablecoins() -> list[dict]:
    """Fetch all stablecoin assets with supply and price data."""
    data = await tracked_get(
        provider=PROVIDER,
        endpoint="stablecoins",
        url=f"{BASE}/stablecoins",
        params={"includePrices": "true"},
        ttl=3600,
    )
    return data.get("peggedAssets", [])


async def get_stablecoin_charts(asset_id: str) -> list[dict]:
    """Historical circulating supply for one asset (all chains combined)."""
    data = await tracked_get(
        provider=PROVIDER,
        endpoint="stablecoin_charts",
        url=f"{BASE}/stablecoincharts/all",
        params={"stablecoin": asset_id},
        ttl=14_400,
    )
    if isinstance(data, list):
        return data
    return data.get("totalCirculating", [])


def parse_supply(asset: dict) -> dict:
    """Extract supply metrics from a DefiLlama peggedAsset record."""
    price = asset.get("price")
    supply_usd = (asset.get("circulating") or {}).get("peggedUSD", 0)
    return {
        "id": asset.get("id"),
        "symbol": asset.get("symbol"),
        "name": asset.get("name"),
        "issuer": asset.get("pegMechanism"),
        "circulating_supply": supply_usd,
        "price": price,
        "peg_deviation_bps": round(abs(price - 1.0) * 10_000, 2) if price is not None else None,
        "chains": list((asset.get("chainCirculating") or {}).keys()),
    }
