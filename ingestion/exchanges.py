"""Exchange price ingestion — Binance (primary), Coinbase (fallback).

No auth required. All HTTP calls go through core.http.tracked_get.
"""

import logging
from typing import Any

from core.http import tracked_get

logger = logging.getLogger(__name__)

BINANCE_BASE = "https://api.binance.com/api/v3"
COINBASE_BASE = "https://api.coinbase.com/v2/prices"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# Maps our symbol → exchange-specific pair identifiers.
SYMBOL_MAP: dict[str, dict[str, str | None]] = {
    "USDT": {"binance": "USDTUSD",  "coinbase": "USDT-USD"},
    "USDC": {"binance": "USDCUSDT", "coinbase": "USDC-USD"},
    "DAI":  {"binance": "DAIUSDT",  "coinbase": "DAI-USD"},
    "FRAX": {"binance": "FRAXUSDT", "coinbase": None},
    "TUSD": {"binance": "TUSDUSDT", "coinbase": "TUSD-USD"},
    "BUSD": {"binance": "BUSDUSDT", "coinbase": None},
}

# CoinGecko coin IDs for the tracked symbols — used for a free batch price
# fallback (one request for all symbols) when the exchanges return nothing.
COINGECKO_IDS: dict[str, str] = {
    "USDT": "tether",
    "USDC": "usd-coin",
    "DAI":  "dai",
    "FRAX": "frax",
    "TUSD": "true-usd",
    "BUSD": "binance-usd",
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


async def _coingecko_prices(ids: list[str]) -> dict[str, float]:
    """Batch USD prices for CoinGecko coin IDs in a single free request.

    Uses CoinGecko's public /simple/price endpoint (no API key, no cost). One
    call covers every requested id, satisfying the "prefer batch requests" rule.
    Returns {coingecko_id: usd_price}; missing/failed ids are simply absent.
    """
    if not ids:
        return {}
    try:
        data = await tracked_get(
            provider="coingecko",
            endpoint="simple_price",
            url=f"{COINGECKO_BASE}/simple/price",
            params={"ids": ",".join(sorted(ids)), "vs_currencies": "usd"},
            ttl=300,
        )
    except Exception:
        return {}

    out: dict[str, float] = {}
    for cg_id, payload in (data or {}).items():
        usd = payload.get("usd") if isinstance(payload, dict) else None
        if usd is not None:
            try:
                out[cg_id] = float(usd)
            except (TypeError, ValueError):
                continue
    return out


PRIMARY_PROVIDER = "binance"
FALLBACK_PROVIDER = "coinbase"


async def get_peg_prices(symbols: list[str] | None = None) -> dict[str, dict]:
    """Return price + order book depth for each symbol, with provider provenance.

    Tries Binance (primary) first; falls back to Coinbase if unavailable. Each
    entry records *which* provider actually served the price so callers no longer
    have to assume it was Binance:

    - ``price_source``   : provider that served the price ("binance"/"coinbase"), or None
    - ``source_type``    : "primary" | "fallback" | "unavailable"
    - ``fallback_used``  : True when the price came from the fallback provider
    - ``fallback_reason``: why the primary was skipped/failed (None on the happy path)

    Order-book depth is Binance-only, so it is present only when the primary
    served the price.
    """
    symbols = symbols or list(SYMBOL_MAP.keys())
    out: dict[str, dict] = {}

    for sym in symbols:
        mapping = SYMBOL_MAP.get(sym, {})
        price: float | None = None
        depth: dict | None = None
        price_source: str | None = None
        source_type = "unavailable"
        fallback_used = False
        fallback_reason: str | None = None

        binance_pair = mapping.get("binance")
        coinbase_pair = mapping.get("coinbase")

        if binance_pair:
            price = await _binance_price(binance_pair)
            if price is not None:
                price_source = PRIMARY_PROVIDER
                source_type = "primary"
                depth = await _binance_depth(binance_pair)
            else:
                fallback_reason = "Binance request failed"
        else:
            fallback_reason = "no Binance pair configured"

        if price is None and coinbase_pair:
            cb_price = await _coinbase_price(coinbase_pair)
            if cb_price is not None:
                price = cb_price
                price_source = FALLBACK_PROVIDER
                source_type = "fallback"
                fallback_used = True
                logger.info("coinbase_fallback symbol=%s reason=%s", sym, fallback_reason)
            else:
                fallback_reason = (
                    f"{fallback_reason}; Coinbase request failed"
                    if fallback_reason else "Coinbase request failed"
                )

        if price is None:
            if coinbase_pair is None:
                fallback_reason = (
                    f"{fallback_reason}; no Coinbase fallback configured"
                    if fallback_reason else "no Coinbase fallback configured"
                )
            source_type = "unavailable"
            logger.warning("no_price_available symbol=%s reason=%s", sym, fallback_reason)

        out[sym] = {
            "price": price,
            "peg_deviation_bps": round(abs(price - 1.0) * 10_000, 2) if price is not None else None,
            "price_source": price_source,
            "source_type": source_type,
            "fallback_used": fallback_used,
            "fallback_reason": fallback_reason,
            **(depth or {}),
        }

    # Final fallback: one free CoinGecko batch call for any symbol still unpriced
    # (e.g. Binance geo-blocked and no Coinbase pair). Order-book depth is
    # exchange-only, so these entries carry price/peg but no depth.
    missing = {sym: COINGECKO_IDS[sym] for sym in symbols
               if out[sym]["price"] is None and sym in COINGECKO_IDS}
    if missing:
        cg_prices = await _coingecko_prices(list(missing.values()))
        for sym, cg_id in missing.items():
            price = cg_prices.get(cg_id)
            if price is None:
                continue
            prior = out[sym]["fallback_reason"]
            out[sym].update({
                "price": price,
                "peg_deviation_bps": round(abs(price - 1.0) * 10_000, 2),
                "price_source": "coingecko",
                "source_type": "fallback",
                "fallback_used": True,
                "fallback_reason": f"{prior}; served by CoinGecko" if prior else "served by CoinGecko",
            })
            logger.info("coingecko_fallback symbol=%s", sym)

    return out
