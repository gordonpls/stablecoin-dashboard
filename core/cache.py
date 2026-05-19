"""In-memory TTL cache for API responses, keyed by (provider, cache_key)."""

import hashlib
import json
import time
from threading import Lock
from typing import Any

_store: dict[tuple[str, str], tuple[float, Any]] = {}
_lock = Lock()

_DEFAULT_TTLS: dict[str, int] = {
    "defillama": 3600,
    "binance": 60,
    "coinbase": 60,
}


def make_key(url: str, params: dict | None) -> str:
    raw = url + json.dumps(params or {}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def get_cached(provider: str, key: str) -> Any | None:
    with _lock:
        entry = _store.get((provider, key))
        if entry is None:
            return None
        ts, data = entry
        if time.monotonic() - ts > _DEFAULT_TTLS.get(provider, 3600):
            del _store[(provider, key)]
            return None
        return data


def set_cached(provider: str, key: str, data: Any, ttl: int | None = None) -> None:
    with _lock:
        if ttl is not None:
            _DEFAULT_TTLS[provider] = ttl
        _store[(provider, key)] = (time.monotonic(), data)


def clear(provider: str | None = None) -> None:
    with _lock:
        if provider is None:
            _store.clear()
        else:
            for k in list(_store.keys()):
                if k[0] == provider:
                    del _store[k]
