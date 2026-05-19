"""Central HTTP client. All external API calls must go through tracked_get.

No ingestion module may call httpx.get, requests.get, or aiohttp directly.
"""

import json
import logging
from typing import Any

import httpx

from core.budget import BudgetExceeded, check_budget, record_spend
from core.cache import get_cached, make_key, set_cached

logger = logging.getLogger(__name__)

_TIMEOUTS: dict[str, float] = {
    "defillama": 20.0,
    "binance": 10.0,
    "coinbase": 10.0,
}

_RAW_TRUNCATE = 4096  # chars stored in DB per response


async def tracked_get(
    provider: str,
    endpoint: str,
    url: str,
    params: dict | None = None,
    ttl: int | None = None,
) -> Any:
    """
    All external API requests must go through this function.

    Responsibilities:
    - check provider budget
    - check cache
    - make request if allowed
    - log request
    - store raw response
    - return normalized JSON
    """
    # 1. budget check
    try:
        check_budget(provider)
    except BudgetExceeded:
        logger.error("budget_exceeded provider=%s endpoint=%s", provider, endpoint)
        raise

    # 2. cache check
    cache_key = make_key(url, params)
    cached = get_cached(provider, cache_key)
    if cached is not None:
        logger.debug("cache_hit provider=%s endpoint=%s", provider, endpoint)
        return cached

    # 3. make request
    timeout = _TIMEOUTS.get(provider, 15.0)
    status_code: int | None = None
    raw: str | None = None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, params=params)
            status_code = resp.status_code
            raw = resp.text[:_RAW_TRUNCATE]
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        _log_request(provider, endpoint, url, status_code, raw)
        logger.warning("http_error provider=%s endpoint=%s status=%s", provider, endpoint, status_code)
        raise
    except httpx.RequestError as exc:
        _log_request(provider, endpoint, url, None, str(exc)[:_RAW_TRUNCATE])
        logger.warning("request_error provider=%s endpoint=%s error=%s", provider, endpoint, exc)
        raise

    # 4. log + record spend
    _log_request(provider, endpoint, url, status_code, raw)
    record_spend(provider)

    # 5. cache + return
    set_cached(provider, cache_key, data, ttl=ttl)
    return data


def _log_request(
    provider: str, endpoint: str, url: str, status: int | None, raw: str | None
) -> None:
    try:
        from db.models import ApiRequestLog, get_session

        with get_session() as session:
            session.add(
                ApiRequestLog(
                    provider=provider,
                    endpoint=endpoint,
                    url=url,
                    status_code=status,
                    raw_response=raw,
                )
            )
            session.commit()
    except Exception as exc:
        logger.warning("log_request_failed error=%s", exc)
