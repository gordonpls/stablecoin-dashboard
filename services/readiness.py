"""Service: deployment readiness and health checks.

Separates the two questions a hosting platform asks before and during a
deployment:

* **liveness** ("is the process up?") — answered by ``GET /health``, which is
  always 200 while the app can respond. It additionally carries a diagnostic
  ``checks`` block (database, disk, environment, configuration, pipelines,
  providers) so operators can see *why* the app might not be ready without it
  taking the app out of rotation.
* **readiness** ("should traffic be routed here?") — answered by ``GET /ready``,
  which returns 200 only when every *critical* check passes and 503 otherwise,
  so an orchestrator can hold traffic until the app can actually serve data.

Only two things are treated as **critical** (a failure blocks readiness):

* database connectivity — a down database means no data can be served;
* required environment variables — if an operator declares a var required via
  ``REQUIRED_ENV_VARS`` and it is unset, the deployment is misconfigured.

Everything else is a non-blocking **warning**: a read-only disk, stale or
never-run pipelines, a failing upstream provider, or a production
misconfiguration (SQLite or an ephemeral temp-dir database / working directory
in production). The data may be stale or at risk, but the app can still serve
what it has.

Read-only and dependency-light so both endpoints stay cheap to poll.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from typing import Any

from sqlalchemy import text

import db.models as _models
from db.models import get_session
from services.freshness import compute_data_freshness
from services.pipeline_runs import pipeline_status_summary

# Fallback used only when the package is not installed (e.g. running from a
# source checkout in tests/CI); mirrors pyproject's ``version``.
_FALLBACK_VERSION = "0.1.0"

# Environment variable values that mark a production deployment.
_PROD_ENV_VALUES = {"prod", "production"}
_ENV_VARS_SIGNALLING_ENV = ("APP_ENV", "ENVIRONMENT", "ENV")

# Check status values, worst last. Used to pick a single headline status.
PASS, WARN, FAIL = "pass", "warn", "fail"
_STATUS_SEVERITY = {PASS: 0, WARN: 1, FAIL: 2}


def app_version() -> str:
    """Return the running application version.

    Prefers the installed package metadata so the value tracks ``pyproject``;
    falls back to a module constant when the package is not installed.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("stablecoin-dashboard")
        except PackageNotFoundError:
            return _FALLBACK_VERSION
    except Exception:  # noqa: BLE001 - version lookup must never raise
        return _FALLBACK_VERSION


def _is_production() -> bool:
    """True when any environment marker names a production deployment."""
    return any(
        os.getenv(var, "").strip().lower() in _PROD_ENV_VALUES
        for var in _ENV_VARS_SIGNALLING_ENV
    )


def _required_env_vars() -> list[str]:
    """Operator-declared required environment variables (comma-separated).

    Empty by default — the project has no hard-required variables today, so an
    opt-in allowlist (``REQUIRED_ENV_VARS=A,B``) is the honest primitive rather
    than inventing fake requirements that would break the current free-tier
    setup.
    """
    raw = os.getenv("REQUIRED_ENV_VARS", "")
    return [v.strip() for v in raw.split(",") if v.strip()]


def _db_url():
    """Current engine URL, read lazily so tests' monkeypatched engine is seen."""
    return _models.engine.url


def check_database() -> dict[str, Any]:
    """Critical: can we connect to the database and run a trivial query?"""
    try:
        with get_session() as session:
            session.execute(text("SELECT 1"))
        url = _db_url()
        return {
            "name": "database",
            "status": PASS,
            "critical": True,
            "detail": "Database connection OK.",
            "driver": url.drivername,
            "database": url.database,
        }
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return {
            "name": "database",
            "status": FAIL,
            "critical": True,
            "detail": f"Database connection failed: {type(exc).__name__}: {exc}",
        }


def check_disk() -> dict[str, Any]:
    """Non-critical: can we write to the directory backing the database?

    Serving reads can still work on a read-only mount, but the ingestion
    pipelines cannot persist new data, so this is surfaced as a warning rather
    than blocking traffic.
    """
    url = _db_url()
    db_path = url.database
    if not db_path or db_path == ":memory:":
        target_dir = tempfile.gettempdir()
        note = " (in-memory database; checked temp dir)"
    else:
        target_dir = os.path.dirname(os.path.abspath(db_path)) or "."
        note = ""

    try:
        with tempfile.NamedTemporaryFile(dir=target_dir, prefix=".readiness_"):
            pass
        return {
            "name": "disk",
            "status": PASS,
            "critical": False,
            "detail": f"Disk writable at {target_dir}.{note}",
            "path": target_dir,
        }
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return {
            "name": "disk",
            "status": WARN,
            "critical": False,
            "detail": (
                f"Disk not writable at {target_dir}: {type(exc).__name__}: {exc}. "
                "Ingestion pipelines cannot persist new data."
            ),
            "path": target_dir,
        }


def check_environment() -> dict[str, Any]:
    """Critical: are all operator-declared required env vars present?"""
    required = _required_env_vars()
    missing = [v for v in required if not os.getenv(v)]
    if missing:
        return {
            "name": "environment",
            "status": FAIL,
            "critical": True,
            "detail": "Missing required environment variables: " + ", ".join(missing) + ".",
            "required": required,
            "missing": missing,
        }
    detail = (
        f"All {len(required)} required environment variable(s) present."
        if required
        else "No required environment variables declared."
    )
    return {
        "name": "environment",
        "status": PASS,
        "critical": False,
        "detail": detail,
        "required": required,
        "missing": [],
    }


def check_configuration() -> dict[str, Any]:
    """Non-critical: production misconfiguration warnings.

    Flags durability/persistence risks that are fine in development but
    dangerous in production: a SQLite database, a database stored under the
    temp directory, or the app running from the temp directory.
    """
    production = _is_production()
    url = _db_url()
    is_sqlite = (url.drivername or "").startswith("sqlite")
    db_path = url.database or ""
    tmp = os.path.abspath(tempfile.gettempdir())

    db_in_tmp = (
        bool(db_path)
        and db_path != ":memory:"
        and os.path.abspath(db_path).startswith(tmp)
    )
    cwd_in_tmp = os.path.abspath(os.getcwd()).startswith(tmp)

    warnings: list[str] = []
    if production:
        if is_sqlite:
            warnings.append(
                "Using SQLite in production — it is single-writer and not "
                "durable under concurrent load; use a managed database."
            )
        if db_in_tmp:
            warnings.append(
                f"Database lives under the temp directory ({tmp}) — data is "
                "ephemeral and lost when the host is recycled."
            )
        if cwd_in_tmp:
            warnings.append(
                f"App is running from the temp directory ({tmp}) — files may "
                "be wiped between restarts."
            )

    if not production:
        detail = "Not running in production mode; configuration checks skipped."
    elif warnings:
        detail = " ".join(warnings)
    else:
        detail = "No production misconfigurations detected."

    return {
        "name": "configuration",
        "status": WARN if warnings else PASS,
        "critical": False,
        "detail": detail,
        "production": production,
        "warnings": warnings,
    }


def check_pipelines() -> dict[str, Any]:
    """Non-critical: have the data pipelines run and recently succeeded?"""
    summary = pipeline_status_summary()
    if not summary:
        return {
            "name": "pipelines",
            "status": WARN,
            "critical": False,
            "detail": "No pipeline runs recorded yet — ingestion has not run.",
            "last_success_at": None,
            "never_succeeded": [],
            "currently_failing": [],
        }

    never = sorted(s["pipeline_name"] for s in summary if s["last_success_at"] is None)
    failing = sorted(s["pipeline_name"] for s in summary if s["last_status"] == "error")
    last_success_at = max(
        (s["last_success_at"] for s in summary if s["last_success_at"]),
        default=None,
    )

    if never:
        status, detail = WARN, (
            f"{len(never)} pipeline(s) have never succeeded: {', '.join(never)}."
        )
    elif failing:
        status, detail = WARN, (
            f"{len(failing)} pipeline(s) last run failed: {', '.join(failing)}."
        )
    else:
        status, detail = PASS, f"All {len(summary)} pipeline(s) have a recent success."

    return {
        "name": "pipelines",
        "status": status,
        "critical": False,
        "detail": detail,
        "last_success_at": last_success_at,
        "never_succeeded": never,
        "currently_failing": failing,
    }


def check_providers() -> dict[str, Any]:
    """Non-critical: are the external data providers responding?"""
    providers = compute_data_freshness().get("providers", [])
    if not providers:
        return {
            "name": "providers",
            "status": WARN,
            "critical": False,
            "detail": "No provider requests logged yet.",
            "failing": [],
            "providers": [],
        }

    failing = sorted(p["provider"] for p in providers if p["status"] == "failing")
    status = WARN if failing else PASS
    detail = (
        f"{len(failing)} provider(s) last returned an error: {', '.join(failing)}."
        if failing
        else f"All {len(providers)} provider(s) responding."
    )
    return {
        "name": "providers",
        "status": status,
        "critical": False,
        "detail": detail,
        "failing": failing,
        "providers": [
            {"provider": p["provider"], "status": p["status"]} for p in providers
        ],
    }


def get_readiness() -> dict[str, Any]:
    """Run every check and roll the results into an overall readiness verdict.

    ``ready`` is True unless a *critical* check failed. ``status`` adds nuance:
    ``ready`` (all pass), ``degraded`` (ready but with warnings/non-critical
    failures), or ``not_ready`` (a critical check failed). Always returns a
    structured object — individual checks capture their own errors rather than
    raising — so the endpoints stay reliable even when a dependency is down.
    """
    checks = [
        check_database(),
        check_disk(),
        check_environment(),
        check_configuration(),
        check_pipelines(),
        check_providers(),
    ]

    ready = not any(c["status"] == FAIL and c["critical"] for c in checks)
    worst = max((c["status"] for c in checks), key=lambda s: _STATUS_SEVERITY[s])
    if not ready:
        overall = "not_ready"
    elif worst == PASS:
        overall = "ready"
    else:
        overall = "degraded"

    return {
        "status": overall,
        "ready": ready,
        "version": app_version(),
        "production": _is_production(),
        "checked_at": datetime.utcnow().isoformat(),
        "checks": checks,
    }
