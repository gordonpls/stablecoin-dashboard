"""CoinGecko — fallback market data only. Rate-limited; use sparingly."""

import os
import time
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from ingestion._base import log_request, rate_limited

PROVIDER = "coingecko"
BASE = "https://api.coingecko.com/api/v3"
CACHE_TTL = 300  # 5 minutes
_cache: dict[str, tuple[float, Any]] = {}

API_KEY = os.getenv("COINGECKO_API_KEY", "")
HEADERS = {"x-cg-demo-api-key": API_KEY} if API_KEY else {}


def _cached_get(url: str, params: dict | None = None) -> Any:
    cache_key = url + str(sorted((params or {}).items()))
    now = time.monotonic()
    if cache_key in _cache:
        ts, data = _cache[cache_key]
        if now - ts < CACHE_TTL:
            return data
    data = _get(url, params)
    _cache[cache_key] = (now, data)
    return data


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=5, max=30))
@rate_limited(PROVIDER)
def _get(url: str, params: dict | None = None) -> Any:
    log_request(PROVIDER, url)
    with httpx.Client(timeout=15, headers=HEADERS) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


STABLECOIN_IDS = [
    "tether", "usd-coin", "dai", "frax", "true-usd", "paxos-standard",
    "gemini-dollar", "liquity-usd", "magic-internet-money",
]


def get_market_data(coin_ids: list[str] | None = None) -> list[dict]:
    """Batch fetch market data. Prefer DefiLlama; call this only as fallback."""
    ids = ",".join(coin_ids or STABLECOIN_IDS)
    return _cached_get(
        f"{BASE}/coins/markets",
        params={
            "vs_currency": "usd",
            "ids": ids,
            "order": "market_cap_desc",
            "per_page": 50,
            "page": 1,
            "price_change_percentage": "7d,30d",
        },
    )


def get_coin_detail(coin_id: str) -> dict:
    return _cached_get(f"{BASE}/coins/{coin_id}", params={"localization": "false", "tickers": "false"})
