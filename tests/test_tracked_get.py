"""Tests for core.http.tracked_get — the central HTTP gate."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from core.budget import BudgetExceeded, _BUDGETS, _calls
from core import cache as cache_mod
from core.http import tracked_get


@pytest.fixture(autouse=True)
def clear_cache_and_budget():
    cache_mod.clear()
    _calls.clear()
    yield
    cache_mod.clear()
    _calls.clear()


@pytest.mark.asyncio
async def test_successful_request_returns_json():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '{"ok": true}'
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value={"ok": True})

    with patch("core.http.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        result = await tracked_get("defillama", "test_endpoint", "https://example.com/test")

    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_cache_hit_skips_http():
    cache_mod.set_cached("defillama", cache_mod.make_key("https://example.com/cached", None), {"cached": True})

    with patch("core.http.httpx.AsyncClient") as mock_client_cls:
        result = await tracked_get("defillama", "test_endpoint", "https://example.com/cached")
        mock_client_cls.assert_not_called()

    assert result == {"cached": True}


@pytest.mark.asyncio
async def test_budget_exceeded_raises():
    _BUDGETS["__test_budget__"] = 1
    _calls["__test_budget__"] = [1e10]  # future timestamp trick

    with pytest.raises(BudgetExceeded):
        await tracked_get("__test_budget__", "ep", "https://example.com")

    del _BUDGETS["__test_budget__"]
    del _calls["__test_budget__"]


@pytest.mark.asyncio
async def test_http_error_logs_and_reraises():
    mock_resp = MagicMock()
    mock_resp.status_code = 503
    mock_resp.text = "Service Unavailable"
    mock_resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("503", request=MagicMock(), response=mock_resp)
    )

    with patch("core.http.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        with pytest.raises(httpx.HTTPStatusError):
            await tracked_get("defillama", "test_endpoint", "https://example.com/fail")


@pytest.mark.asyncio
async def test_result_is_cached_after_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '{"v": 42}'
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value={"v": 42})

    url = "https://example.com/unique-cache-test"
    with patch("core.http.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        await tracked_get("defillama", "ep", url)

        # second call — should use cache, not make another HTTP request
        mock_client.get.reset_mock()
        await tracked_get("defillama", "ep", url)
        mock_client.get.assert_not_called()
