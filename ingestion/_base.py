"""Shared utilities: rate limiting, request logging, budget guard."""

import logging
import os
import time
from functools import wraps
from threading import Lock

import structlog
from db.models import get_session, ApiRequestLog

logger = structlog.get_logger(__name__)

_counters: dict[str, list[float]] = {}
_lock = Lock()

_RATE_LIMITS: dict[str, int] = {
    "defillama": int(os.getenv("RATE_LIMIT_DEFILLAMA", "30")),
    "coingecko": int(os.getenv("RATE_LIMIT_COINGECKO", "10")),
    "exchanges": int(os.getenv("RATE_LIMIT_EXCHANGES", "60")),
}


def rate_limited(provider: str):
    """Decorator: enforce per-provider requests-per-minute limit."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            limit = _RATE_LIMITS.get(provider, 60)
            now = time.monotonic()
            with _lock:
                timestamps = _counters.setdefault(provider, [])
                # drop timestamps older than 60 s
                _counters[provider] = [t for t in timestamps if now - t < 60]
                if len(_counters[provider]) >= limit:
                    sleep_for = 60 - (now - _counters[provider][0])
                    logger.warning("rate_limit_hit", provider=provider, sleep=sleep_for)
                    time.sleep(max(sleep_for, 0))
                _counters[provider].append(time.monotonic())
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def log_request(provider: str, url: str) -> None:
    try:
        with get_session() as session:
            session.add(ApiRequestLog(provider=provider, url=url))
            session.commit()
    except Exception:
        logger.warning("failed_to_log_request", provider=provider)
