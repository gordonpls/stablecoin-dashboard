"""The Graph — protocol-level data. Enabled post-MVP only."""

import os
import time
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ingestion._base import log_request, rate_limited

PROVIDER = "thegraph"
GATEWAY = "https://gateway.thegraph.com/api"
API_KEY = os.getenv("THEGRAPH_API_KEY", "")
CACHE_TTL = 1800
_cache: dict[str, tuple[float, Any]] = {}

SUBGRAPH_IDS: dict[str, str] = {
    "uniswap_v3": "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV",
    "aave_v3":    "JCNWRypm7FYwV8fx5HhzZPSFaMxgkPuw4TnWm8FP6QEF",
    "curve":      "3fy93eAT56UJsRCEht8iFhfi6wjHWXtZ9dnnbQmvFopF",
}


def _query(subgraph_id: str, gql: str, variables: dict | None = None) -> dict:
    cache_key = subgraph_id + gql + str(variables)
    now = time.monotonic()
    if cache_key in _cache:
        ts, data = _cache[cache_key]
        if now - ts < CACHE_TTL:
            return data
    result = _post(subgraph_id, gql, variables or {})
    _cache[cache_key] = (now, result)
    return result


@retry(
    retry=retry_if_exception_type(httpx.RequestError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=4, max=20),
)
@rate_limited(PROVIDER)
def _post(subgraph_id: str, gql: str, variables: dict) -> dict:
    if not API_KEY:
        raise RuntimeError("THEGRAPH_API_KEY not set — The Graph is disabled until post-MVP")
    url = f"{GATEWAY}/{API_KEY}/subgraphs/id/{subgraph_id}"
    log_request(PROVIDER, url)
    with httpx.Client(timeout=20) as client:
        resp = client.post(url, json={"query": gql, "variables": variables})
        resp.raise_for_status()
        return resp.json()


def get_uniswap_stablecoin_pools(symbols: list[str] | None = None) -> list[dict]:
    gql = """
    query StablePools($tokens: [String!]) {
      pools(
        where: {token0_in: $tokens, token1_in: $tokens}
        orderBy: totalValueLockedUSD
        orderByDirection: desc
        first: 20
      ) {
        id token0 { symbol } token1 { symbol }
        totalValueLockedUSD volumeUSD feeTier
      }
    }
    """
    tokens = [s.lower() for s in (symbols or ["usdt", "usdc", "dai", "frax"])]
    data = _query(SUBGRAPH_IDS["uniswap_v3"], gql, {"tokens": tokens})
    return data.get("data", {}).get("pools", [])
