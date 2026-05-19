"""Per-provider daily call budget guard.

Budget is the maximum number of calls per rolling 24-hour window.
Set to 0 to disable the limit (default for all free-tier providers).
"""

import os
import time
from threading import Lock

_WINDOW = 86_400  # 24 hours in seconds

_BUDGETS: dict[str, int] = {
    "defillama": int(os.getenv("BUDGET_DEFILLAMA", "0")),
    "binance":   int(os.getenv("BUDGET_BINANCE",   "0")),
    "coinbase":  int(os.getenv("BUDGET_COINBASE",  "0")),
}

_calls: dict[str, list[float]] = {}
_lock = Lock()


class BudgetExceeded(Exception):
    pass


def check_budget(provider: str) -> None:
    """Raise BudgetExceeded if the provider has hit its daily call cap."""
    limit = _BUDGETS.get(provider, 0)
    if limit == 0:
        return
    now = time.monotonic()
    with _lock:
        window = _calls.setdefault(provider, [])
        _calls[provider] = [t for t in window if now - t < _WINDOW]
        if len(_calls[provider]) >= limit:
            raise BudgetExceeded(
                f"{provider} daily budget of {limit} calls exceeded"
            )


def record_spend(provider: str) -> None:
    with _lock:
        _calls.setdefault(provider, []).append(time.monotonic())


def usage(provider: str) -> int:
    now = time.monotonic()
    with _lock:
        window = _calls.get(provider, [])
        return sum(1 for t in window if now - t < _WINDOW)
