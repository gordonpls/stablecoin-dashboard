"""Tests for the deployment readiness service and the /health + /ready endpoints.

The autouse `in_memory_db` fixture (conftest.py) gives each test a fresh
in-memory SQLite shared by the service and the FastAPI client.
"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.api.server import app
from db.models import ApiRequestLog, PipelineRun, get_session
from services import readiness

client = TestClient(app)


# ── helpers ─────────────────────────────────────────────────────────────────────

def _add_pipeline_run(name, status, *, error=None):
    now = datetime(2026, 5, 22, 12, 0, 0)
    with get_session() as s:
        s.add(PipelineRun(
            pipeline_name=name,
            started_at=now,
            finished_at=now,
            status=status,
            rows_written=1,
            error_message=error,
            duration_seconds=0.5,
        ))
        s.commit()


def _add_api_log(provider, status_code):
    with get_session() as s:
        s.add(ApiRequestLog(
            provider=provider,
            endpoint="ep",
            url="http://example/ep",
            status_code=status_code,
            requested_at=datetime(2026, 5, 22, 12, 0, 0),
        ))
        s.commit()


def _force_not_production(monkeypatch):
    for var in ("APP_ENV", "ENVIRONMENT", "ENV"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("REQUIRED_ENV_VARS", raising=False)


# ── app_version ──────────────────────────────────────────────────────────────────

def test_app_version_is_nonempty_string(in_memory_db):
    v = readiness.app_version()
    assert isinstance(v, str) and v


# ── individual checks ─────────────────────────────────────────────────────────

def test_check_database_passes_on_live_db(in_memory_db):
    chk = readiness.check_database()
    assert chk["name"] == "database"
    assert chk["status"] == "pass"
    assert chk["critical"] is True


def test_check_database_fails_when_session_raises(in_memory_db, monkeypatch):
    def _boom():
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(readiness, "get_session", _boom)
    chk = readiness.check_database()
    assert chk["status"] == "fail"
    assert chk["critical"] is True
    assert "db unreachable" in chk["detail"]


def test_check_disk_passes(in_memory_db):
    chk = readiness.check_disk()
    assert chk["name"] == "disk"
    assert chk["status"] == "pass"
    assert chk["critical"] is False


def test_check_environment_no_requirements(in_memory_db, monkeypatch):
    monkeypatch.delenv("REQUIRED_ENV_VARS", raising=False)
    chk = readiness.check_environment()
    assert chk["status"] == "pass"
    assert chk["missing"] == []


def test_check_environment_missing_required_var_fails(in_memory_db, monkeypatch):
    monkeypatch.setenv("REQUIRED_ENV_VARS", "MY_REQUIRED_X, MY_REQUIRED_Y")
    monkeypatch.delenv("MY_REQUIRED_X", raising=False)
    monkeypatch.setenv("MY_REQUIRED_Y", "set")

    chk = readiness.check_environment()
    assert chk["status"] == "fail"
    assert chk["critical"] is True
    assert chk["missing"] == ["MY_REQUIRED_X"]


def test_check_configuration_not_production_is_pass(in_memory_db, monkeypatch):
    _force_not_production(monkeypatch)
    chk = readiness.check_configuration()
    assert chk["status"] == "pass"
    assert chk["production"] is False
    assert chk["warnings"] == []


def test_check_configuration_warns_on_sqlite_in_production(in_memory_db, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    chk = readiness.check_configuration()
    assert chk["status"] == "warn"
    assert chk["critical"] is False        # production misconfig never blocks traffic
    assert any("SQLite" in w for w in chk["warnings"])


def test_check_pipelines_no_runs_warns(in_memory_db):
    chk = readiness.check_pipelines()
    assert chk["status"] == "warn"
    assert chk["last_success_at"] is None


def test_check_pipelines_all_succeeded_passes(in_memory_db):
    _add_pipeline_run("update_supply", "success")
    chk = readiness.check_pipelines()
    assert chk["status"] == "pass"
    assert chk["currently_failing"] == []
    assert chk["last_success_at"] is not None


def test_check_pipelines_flags_failing(in_memory_db):
    _add_pipeline_run("update_prices", "error", error="boom")
    chk = readiness.check_pipelines()
    assert chk["status"] == "warn"
    assert chk["currently_failing"] == ["update_prices"]
    assert chk["never_succeeded"] == ["update_prices"]


def test_check_providers_no_logs_warns(in_memory_db):
    chk = readiness.check_providers()
    assert chk["status"] == "warn"
    assert chk["providers"] == []


def test_check_providers_flags_failing_provider(in_memory_db):
    _add_api_log("binance", 500)
    chk = readiness.check_providers()
    assert chk["status"] == "warn"
    assert "binance" in chk["failing"]


def test_check_providers_healthy_passes(in_memory_db):
    _add_api_log("defillama", 200)
    chk = readiness.check_providers()
    assert chk["status"] == "pass"
    assert chk["failing"] == []


# ── get_readiness rollup ──────────────────────────────────────────────────────

def test_get_readiness_ready_when_everything_passes(in_memory_db, monkeypatch):
    _force_not_production(monkeypatch)
    _add_pipeline_run("update_supply", "success")
    _add_api_log("defillama", 200)

    result = readiness.get_readiness()
    assert result["ready"] is True
    assert result["status"] == "ready"
    assert {c["name"] for c in result["checks"]} == {
        "database", "disk", "environment", "configuration", "pipelines", "providers",
    }


def test_get_readiness_degraded_with_warnings_but_still_ready(in_memory_db, monkeypatch):
    _force_not_production(monkeypatch)
    # Fresh DB: no pipeline runs and no provider logs → warnings, but no
    # critical failure, so the app is still ready to serve.
    result = readiness.get_readiness()
    assert result["ready"] is True
    assert result["status"] == "degraded"


def test_get_readiness_not_ready_when_required_var_missing(in_memory_db, monkeypatch):
    monkeypatch.setenv("REQUIRED_ENV_VARS", "MUST_BE_SET")
    monkeypatch.delenv("MUST_BE_SET", raising=False)

    result = readiness.get_readiness()
    assert result["ready"] is False
    assert result["status"] == "not_ready"


# ── /health endpoint (liveness) ─────────────────────────────────────────────────

def test_health_always_ok(in_memory_db):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert isinstance(body["ready"], bool)
    assert "version" in body
    assert isinstance(body["checks"], list) and body["checks"]


def test_health_stays_200_even_when_not_ready(in_memory_db, monkeypatch):
    monkeypatch.setenv("REQUIRED_ENV_VARS", "MUST_BE_SET")
    monkeypatch.delenv("MUST_BE_SET", raising=False)

    resp = client.get("/health")
    assert resp.status_code == 200       # liveness is unaffected by readiness
    assert resp.json()["ready"] is False
    assert resp.json()["readiness_status"] == "not_ready"


# ── /ready endpoint (readiness) ──────────────────────────────────────────────────

def test_ready_returns_200_when_ready(in_memory_db, monkeypatch):
    _force_not_production(monkeypatch)
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json()["ready"] is True


def test_ready_returns_503_when_required_var_missing(in_memory_db, monkeypatch):
    monkeypatch.setenv("REQUIRED_ENV_VARS", "MUST_BE_SET")
    monkeypatch.delenv("MUST_BE_SET", raising=False)

    resp = client.get("/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["ready"] is False
    env_check = next(c for c in body["checks"] if c["name"] == "environment")
    assert env_check["status"] == "fail"


def test_ready_returns_503_when_database_down(in_memory_db, monkeypatch):
    def _boom():
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(readiness, "get_session", _boom)
    resp = client.get("/ready")
    assert resp.status_code == 503
    assert resp.json()["ready"] is False
