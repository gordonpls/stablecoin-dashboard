"""Exchange price ingestion — Binance (primary), Coinbase (fallback).

No auth required. All HTTP calls go through core.http.tracked_get.
"""

import logging
from typing import Any

from core.http import tracked_get

logger = logging.getLogger(__name__)

BINANCE_BASE = "https://api.binance.com/api/v3"
COINBASE_BASE = "https://api.coinbase.com/v2/prices"

# Maps our symbol → exchange-specific pair identifiers.
SYMBOL_MAP: dict[str, dict[str, str | None]] = {
    "USDT": {"binance": "USDTUSD",  "coinbase": "USDT-USD"},
    "USDC": {"binance": "USDCUSDT", "coinbase": "USDC-USD"},
    "DAI":  {"binance": "DAIUSDT",  "coinbase": "DAI-USD"},
    "FRAX": {"binance": "FRAXUSDT", "coinbase": None},
    "TUSD": {"binance": "TUSDUSDT", "coinbase": "TUSD-USD"},
    "BUSD": {"binance": "BUSDUSDT", "coinbase": None},
}


async def _binance_price(pair: str) -> float | None:
    try:
        data = await tracked_get(
            provider="binance",
            endpoint="ticker_price",
            url=f"{BINANCE_BASE}/ticker/price",
            params={"symbol": pair},
            ttl=60,
        )
        return float(data["price"])
    except Exception:
        return None


async def _binance_depth(pair: str, limit: int = 20) -> dict | None:
    try:
        data = await tracked_get(
            provider="binance",
            endpoint="order_book_depth",
            url=f"{BINANCE_BASE}/depth",
            params={"symbol": pair, "limit": limit},
            ttl=60,
        )
        bids = sum(float(p) * float(q) for p, q in data.get("bids", []))
        asks = sum(float(p) * float(q) for p, q in data.get("asks", []))
        return {"bid_depth_usd": bids, "ask_depth_usd": asks}
    except Exception:
        return None


async def _coinbase_price(pair: str) -> float | None:
    try:
        data = await tracked_get(
            provider="coinbase",
            endpoint="spot_price",
            url=f"{COINBASE_BASE}/{pair}/spot",
            ttl=60,
        )
        return float(data["data"]["amount"])
    except Exception:
        return None


async def get_peg_prices(symbols: list[str] | None = None) -> dict[str, dict]:
    """Return price + order book depth for each symbol.

    Tries Binance first; falls back to Coinbase if unavailable.
    """
    symbols = symbols or list(SYMBOL_MAP.keys())
    out: dict[str, dict] = {}

    for sym in symbols:
        mapping = SYMBOL_MAP.get(sym, {})
        price: float | None = None
        depth: dict | None = None

        binance_pair = mapping.get("binance")
        coinbase_pair = mapping.get("coinbase")

        if binance_pair:
            price = await _binance_price(binance_pair)
            if price is not None:
                depth = await _binance_depth(binance_pair)

        if price is None and coinbase_pair:
            price = await _coinbase_price(coinbase_pair)
            logger.info("coinbase_fallback symbol=%s", sym)

        if price is None:
            logger.warning("no_price_available symbol=%s", sym)

        out[sym] = {
            "price": price,
            "peg_deviation_bps": round(abs(price - 1.0) * 10_000, 2) if price is not None else None,
            **(depth or {}),
        }

    return out
