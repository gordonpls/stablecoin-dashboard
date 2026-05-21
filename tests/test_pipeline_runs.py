"""Tests for the pipeline-run history service, its endpoint, and instrumentation.

The autouse `in_memory_db` fixture (conftest.py) gives each test a fresh
in-memory SQLite shared by the service and the FastAPI client.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.server import app
from db.models import PipelineRun, get_session
from services.pipeline_runs import (
    pipeline_status_summary,
    query_runs,
    record_run,
)

client = TestClient(app)


# ── helpers ─────────────────────────────────────────────────────────────────────

def _add_run(name, status, *, started, rows=0, duration=1.0, error=None, finished=None):
    with get_session() as s:
        s.add(PipelineRun(
            pipeline_name=name,
            started_at=started,
            finished_at=finished or started,
            status=status,
            rows_written=rows,
            error_message=error,
            duration_seconds=duration,
        ))
        s.commit()


def _all_runs():
    with get_session() as s:
        return s.execute(select(PipelineRun)).scalars().all()


# ── record_run: success path ──────────────────────────────────────────────────

def test_record_run_persists_success(in_memory_db):
    with record_run("update_supply") as rec:
        rec.rows_written = 7

    runs = _all_runs()
    assert len(runs) == 1
    row = runs[0]
    assert row.pipeline_name == "update_supply"
    assert row.status == "success"
    assert row.rows_written == 7
    assert row.error_message is None
    assert row.finished_at is not None
    assert row.duration_seconds is not None and row.duration_seconds >= 0


def test_record_run_defaults_rows_to_zero(in_memory_db):
    with record_run("update_reserves"):
        pass  # pipeline forgot to set rows_written

    row = _all_runs()[0]
    assert row.status == "success"
    assert row.rows_written == 0


# ── record_run: error path ──────────────────────────────────────────────────────

def test_record_run_records_error_and_reraises(in_memory_db):
    with pytest.raises(ValueError, match="boom"):
        with record_run("update_prices"):
            raise ValueError("boom")

    row = _all_runs()[0]
    assert row.status == "error"
    assert "ValueError" in row.error_message
    assert "boom" in row.error_message
    assert row.finished_at is not None


def test_record_run_truncates_long_error(in_memory_db):
    long_msg = "x" * 5000
    with pytest.raises(RuntimeError):
        with record_run("score_stablecoins"):
            raise RuntimeError(long_msg)

    row = _all_runs()[0]
    assert len(row.error_message) <= 1000


# ── query_runs ──────────────────────────────────────────────────────────────────

def test_query_runs_newest_first_and_limit(in_memory_db):
    base = datetime(2026, 5, 21, 12, 0, 0)
    for i in range(5):
        _add_run("update_prices", "success", started=base + timedelta(minutes=i))

    runs = query_runs(limit=3)
    assert len(runs) == 3
    times = [r["started_at"] for r in runs]
    assert times == sorted(times, reverse=True)


def test_query_runs_filters_by_pipeline_and_status(in_memory_db):
    now = datetime(2026, 5, 21, 12, 0, 0)
    _add_run("update_prices", "success", started=now)
    _add_run("update_prices", "error", started=now + timedelta(minutes=1), error="x")
    _add_run("update_supply", "success", started=now + timedelta(minutes=2))

    assert len(query_runs(pipeline_name="update_prices")) == 2
    assert len(query_runs(status="error")) == 1
    assert len(query_runs(pipeline_name="update_prices", status="success")) == 1
    assert query_runs(status="ERROR")  # status match is case-insensitive


def test_query_runs_empty_db(in_memory_db):
    assert query_runs() == []


# ── pipeline_status_summary ───────────────────────────────────────────────────

def test_summary_reports_latest_and_last_success(in_memory_db):
    now = datetime(2026, 5, 21, 12, 0, 0)
    # A successful run, then a later failed run for the same pipeline.
    _add_run("update_prices", "success", started=now, rows=10, duration=2.0)
    _add_run("update_prices", "error", started=now + timedelta(minutes=10), error="API down")

    summary = pipeline_status_summary(now=now + timedelta(minutes=11))
    assert len(summary) == 1
    s = summary[0]
    assert s["pipeline_name"] == "update_prices"
    assert s["last_status"] == "error"          # most recent run failed
    assert s["last_error"] == "API down"
    assert s["last_success_at"] is not None      # but a prior success exists
    assert s["recent_failures"] == 1


def test_summary_recent_failures_window(in_memory_db):
    now = datetime(2026, 5, 21, 12, 0, 0)
    _add_run("update_supply", "error", started=now - timedelta(hours=48), error="old")
    _add_run("update_supply", "error", started=now - timedelta(hours=1), error="recent")

    summary = pipeline_status_summary(now=now)
    s = next(x for x in summary if x["pipeline_name"] == "update_supply")
    assert s["recent_failures"] == 1   # only the failure inside the 24h window


def test_summary_one_row_per_pipeline_sorted(in_memory_db):
    now = datetime(2026, 5, 21, 12, 0, 0)
    _add_run("score_stablecoins", "success", started=now)
    _add_run("update_prices", "success", started=now)
    _add_run("update_prices", "success", started=now + timedelta(minutes=5))

    summary = pipeline_status_summary(now=now + timedelta(minutes=6))
    names = [s["pipeline_name"] for s in summary]
    assert names == sorted(names)
    assert len(names) == len(set(names)) == 2


# ── FastAPI endpoint ──────────────────────────────────────────────────────────

def test_endpoint_returns_summary_and_runs(in_memory_db):
    now = datetime(2026, 5, 21, 12, 0, 0)
    _add_run("update_prices", "success", started=now, rows=3)

    resp = client.get("/pipeline-runs")
    assert resp.status_code == 200
    body = resp.json()
    assert "summary" in body and "runs" in body
    assert len(body["summary"]) == 1
    assert len(body["runs"]) == 1
    assert body["runs"][0]["pipeline_name"] == "update_prices"


def test_endpoint_filters(in_memory_db):
    now = datetime(2026, 5, 21, 12, 0, 0)
    _add_run("update_prices", "success", started=now)
    _add_run("update_supply", "error", started=now + timedelta(minutes=1), error="x")

    resp = client.get("/pipeline-runs", params={"status": "error"})
    runs = resp.json()["runs"]
    assert len(runs) == 1
    assert runs[0]["pipeline_name"] == "update_supply"


def test_endpoint_empty_db(in_memory_db):
    resp = client.get("/pipeline-runs")
    assert resp.status_code == 200
    assert resp.json() == {"summary": [], "runs": []}


# ── pipeline instrumentation (integration) ──────────────────────────────────────

def test_reserves_pipeline_records_a_run(in_memory_db):
    from pipelines.update_reserves import run

    run()

    runs = query_runs(pipeline_name="update_reserves")
    assert len(runs) == 1
    assert runs[0]["status"] == "success"
    assert runs[0]["rows_written"] == 3   # USDT, USDC, DAI


def test_liquidity_pipeline_records_failure_and_reraises(in_memory_db):
    from pipelines.update_liquidity import run

    with patch("pipelines.update_liquidity.get_peg_prices", new_callable=AsyncMock,
               side_effect=RuntimeError("exchange unreachable")):
        with pytest.raises(RuntimeError, match="exchange unreachable"):
            run()

    runs = query_runs(pipeline_name="update_liquidity")
    assert len(runs) == 1
    assert runs[0]["status"] == "error"
    assert "exchange unreachable" in runs[0]["error_message"]
