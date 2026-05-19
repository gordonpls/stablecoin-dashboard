"""Etherscan — on-chain token supply reads. Free tier only."""

import os
import time
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from ingestion._base import log_request, rate_limited

PROVIDER = "etherscan"
BASE = "https://api.etherscan.io/api"
API_KEY = os.getenv("ETHERSCAN_API_KEY", "")
CACHE_TTL = 600  # 10 minutes
_cache: dict[str, tuple[float, Any]] = {}

TOKEN_CONTRACTS: dict[str, str] = {
    "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "DAI":  "0x6B175474E89094C44Da98b954EedeAC495271d0F",
    "FRAX": "0x853d955aCEf822Db058eb8505911ED77F175b99e",
    "BUSD": "0x4Fabb145d64652a948d72533023f6E7A623C7C53",
}


def _cached_get(params: dict) -> Any:
    key = str(sorted(params.items()))
    now = time.monotonic()
    if key in _cache:
        ts, data = _cache[key]
        if now - ts < CACHE_TTL:
            return data
    data = _get(params)
    _cache[key] = (now, data)
    return data


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
@rate_limited(PROVIDER)
def _get(params: dict) -> Any:
    log_request(PROVIDER, BASE)
    p = {**params, "apikey": API_KEY} if API_KEY else params
    with httpx.Client(timeout=15) as client:
        resp = client.get(BASE, params=p)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "0":
            raise ValueError(f"Etherscan error: {data.get('message')}")
        return data


def get_token_supply(symbol: str) -> int | None:
    """Return total supply (raw integer, divide by decimals yourself)."""
    contract = TOKEN_CONTRACTS.get(symbol.upper())
    if not contract:
        return None
    try:
        data = _cached_get({"module": "stats", "action": "tokensupply", "contractaddress": contract})
        return int(data["result"])
    except Exception:
        return None


def get_token_holder_count(symbol: str) -> int | None:
    contract = TOKEN_CONTRACTS.get(symbol.upper())
    if not contract:
        return None
    try:
        data = _cached_get({"module": "token", "action": "tokenholdercount", "contractaddress": contract})
        return int(data["result"])
    except Exception:
        return None
