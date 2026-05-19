"""Tests for budget limits and the tracked_get gate."""

import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch

from core.budget import BudgetExceeded, check_budget, record_spend, usage, _BUDGETS, _calls
from core import cache as cache_mod
from core.http import tracked_get


@pytest.fixture(autouse=True)
def reset_state():
    cache_mod.clear()
    _calls.clear()
    yield
    cache_mod.clear()
    _calls.clear()


# ── budget module ────────────────────────────────────────────────────────────

def test_zero_budget_always_passes():
    _BUDGETS["__zero__"] = 0
    check_budget("__zero__")  # should not raise
    del _BUDGETS["__zero__"]


def test_budget_exceeded_raises():
    _BUDGETS["__tight__"] = 2
    _calls["__tight__"] = [time.monotonic(), time.monotonic()]
    with pytest.raises(BudgetExceeded, match="__tight__"):
        check_budget("__tight__")
    del _BUDGETS["__tight__"]
    del _calls["__tight__"]


def test_budget_resets_after_window():
    _BUDGETS["__window__"] = 1
    # Inject a timestamp older than 24h window
    _calls["__window__"] = [time.monotonic() - 90_000]
    check_budget("__window__")  # should not raise — old call is outside window
    del _BUDGETS["__window__"]
    del _calls["__window__"]


def test_record_spend_increments_usage():
    provider = "__usage__"
    before = usage(provider)
    record_spend(provider)
    record_spend(provider)
    assert usage(provider) == before + 2
    del _calls[provider]


def test_unknown_provider_has_zero_budget():
    check_budget("__unknown_provider_xyz__")  # budget=0, should not raise


# ── tracked_get enforces budget ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tracked_get_raises_on_budget_exceeded():
    _BUDGETS["__blocked__"] = 1
    _calls["__blocked__"] = [time.monotonic()]  # already at limit

    with pytest.raises(BudgetExceeded):
        await tracked_get("__blocked__", "ep", "https://example.com")

    del _BUDGETS["__blocked__"]
    del _calls["__blocked__"]


# ── no paid API calls without explicit config ────────────────────────────────

@pytest.mark.asyncio
async def test_thegraph_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("THEGRAPH_API_KEY", raising=False)
    import importlib
    import ingestion.thegraph as tg
    importlib.reload(tg)

    with pytest.raises(RuntimeError, match="THEGRAPH_API_KEY"):
        tg._post("fake_id", "{ test }", {})


# ── cache prevents duplicate calls ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_tracked_get_does_not_double_call_cached_endpoint():
    url = "https://example.com/once"
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '{"n": 1}'
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value={"n": 1})

    with patch("core.http.httpx.AsyncClient") as cls:
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock(return_value=mock_resp)
        cls.return_value = client

        await tracked_get("defillama", "ep", url)
        await tracked_get("defillama", "ep", url)

        assert client.get.await_count == 1  # second call served from cache
