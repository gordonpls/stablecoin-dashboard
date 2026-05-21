"""Service: record and query pipeline execution history.

Every ingestion/scoring pipeline run is logged to ``pipeline_runs`` so users
and admins can answer "are the data jobs working, and when did each last
succeed?" without reading server logs.

``record_run`` is a context manager wrapped around each pipeline's ``run()``.
It captures start/finish time, duration, status (``success``/``error``), the
error message on failure, and the number of rows the pipeline wrote (the
pipeline sets ``handle.rows_written`` inside the block). It always persists a
row — even when the pipeline raises — and then re-raises, so failures are both
recorded and still visible to the caller. Recording is best-effort: a failure
to write the history row never masks the pipeline's own outcome.

``query_runs`` backs the FastAPI ``/pipeline-runs`` endpoint and the Streamlit
job-history UI; ``pipeline_status_summary`` rolls the history up to one row per
pipeline (last run, last status, last success, recent failures) for the
at-a-glance health view and the failed-pipeline callout.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Generator

from sqlalchemy import func, select

from db.models import PipelineRun, get_session

logger = logging.getLogger(__name__)

SUCCESS = "success"
ERROR = "error"

# Window used by pipeline_status_summary to count "recent" failures.
RECENT_FAILURE_WINDOW = timedelta(hours=24)


@dataclass
class RunHandle:
    """Mutable handle yielded by :func:`record_run`.

    Pipelines set ``rows_written`` to report how many rows they persisted; it
    defaults to 0 so a pipeline that forgets to set it still records cleanly.
    """

    rows_written: int = 0


@contextmanager
def record_run(pipeline_name: str) -> Generator[RunHandle, None, None]:
    """Record one pipeline execution, capturing status, timing, and row count.

    Usage::

        with record_run("update_supply") as run:
            run.rows_written = do_the_work()

    Persists a ``pipeline_runs`` row on both success and failure, then re-raises
    any exception so the pipeline's own error handling is unchanged.
    """
    handle = RunHandle()
    started = datetime.utcnow()
    status = SUCCESS
    error_message: str | None = None

    try:
        yield handle
    except Exception as exc:  # noqa: BLE001 - re-raised below after recording
        status = ERROR
        error_message = f"{type(exc).__name__}: {exc}"[:1000]
        raise
    finally:
        finished = datetime.utcnow()
        duration = (finished - started).total_seconds()
        try:
            with get_session() as session:
                session.add(PipelineRun(
                    pipeline_name=pipeline_name,
                    started_at=started,
                    finished_at=finished,
                    status=status,
                    rows_written=int(handle.rows_written or 0),
                    error_message=error_message,
                    duration_seconds=round(duration, 3),
                ))
                session.commit()
        except Exception as rec_exc:  # noqa: BLE001 - recording is best-effort
            # Never let a logging failure mask the pipeline's real outcome.
            logger.warning(
                "pipeline_run_record_failed pipeline=%s error=%s",
                pipeline_name, rec_exc,
            )


def query_runs(
    pipeline_name: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return pipeline runs newest-first, optionally filtered.

    Filters are applied in the database. ``status`` is matched case-insensitively
    (``success``/``error``). Returns an empty list when nothing matches.
    """
    with get_session() as session:
        q = select(PipelineRun).order_by(
            PipelineRun.started_at.desc(), PipelineRun.id.desc()
        )
        if pipeline_name:
            q = q.where(PipelineRun.pipeline_name == pipeline_name)
        if status:
            q = q.where(PipelineRun.status == status.lower())
        rows = session.execute(q.limit(limit)).scalars().all()
        return [r.to_dict() for r in rows]


def pipeline_status_summary(now: datetime | None = None) -> list[dict[str, Any]]:
    """One health row per pipeline, ordered by pipeline name.

    For each distinct pipeline that has ever run, reports its most recent run
    (status, time, rows, duration, error) plus the timestamp of its last
    *successful* run and a count of failures in the last 24h — enough for a
    health pill and a "X last succeeded N ago" line in the UI.
    """
    now = now or datetime.utcnow()
    cutoff = now - RECENT_FAILURE_WINDOW
    out: list[dict[str, Any]] = []

    with get_session() as session:
        names = session.execute(
            select(PipelineRun.pipeline_name).distinct()
        ).scalars().all()

        for name in sorted(names):
            latest = session.execute(
                select(PipelineRun)
                .where(PipelineRun.pipeline_name == name)
                .order_by(PipelineRun.started_at.desc(), PipelineRun.id.desc())
                .limit(1)
            ).scalars().first()

            last_success_at = session.execute(
                select(func.max(PipelineRun.started_at)).where(
                    PipelineRun.pipeline_name == name,
                    PipelineRun.status == SUCCESS,
                )
            ).scalar_one_or_none()

            recent_failures = session.execute(
                select(func.count()).where(
                    PipelineRun.pipeline_name == name,
                    PipelineRun.status == ERROR,
                    PipelineRun.started_at >= cutoff,
                )
            ).scalar_one()

            out.append({
                "pipeline_name": name,
                "last_status": latest.status if latest else None,
                "last_run_at": latest.started_at.isoformat() if latest else None,
                "last_finished_at": (
                    latest.finished_at.isoformat()
                    if latest and latest.finished_at else None
                ),
                "last_rows_written": latest.rows_written if latest else None,
                "last_duration_seconds": latest.duration_seconds if latest else None,
                "last_error": latest.error_message if latest else None,
                "last_success_at": (
                    last_success_at.isoformat() if last_success_at else None
                ),
                "recent_failures": recent_failures,
            })

    return out
