"""Tests for the alerts feature: services/alerts.py + /alerts endpoints.

The autouse `in_memory_db` fixture (conftest.py) gives each test a fresh
in-memory SQLite, so there are no alerts to start with.
"""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.server import app
from db.models import (
    Alert,
    PriceSnapshot,
    RiskScore,
    Stablecoin,
    SupplySnapshot,
    get_session,
)
from services.alerts import (
    create_alert,
    delete_alert,
    evaluate_alerts,
    get_alert,
    list_alerts,
    update_alert,
)

client = TestClient(app)


# ── helpers ─────────────────────────────────────────────────────────────────────

def _add_stablecoin(symbol: str = "USDT", name: str = "Tether") -> None:
    with get_session() as s:
        s.add(Stablecoin(id=symbol.lower(), symbol=symbol, name=name, issuer="fiat-backed"))
        s.commit()


def _add_price(symbol: str, price: float = 1.0, dev: float = 1.0,
               bid: float = 1_000_000.0, ask: float = 1_000_000.0,
               when: datetime | None = None) -> None:
    with get_session() as s:
        s.add(PriceSnapshot(
            symbol=symbol, price=price, peg_deviation_bps=dev,
            bid_depth_usd=bid, ask_depth_usd=ask,
            source="binance", recorded_at=when or datetime.utcnow(),
        ))
        s.commit()


def _add_score(symbol: str, overall: float = 88.0, when: datetime | None = None) -> None:
    with get_session() as s:
        s.add(RiskScore(
            symbol=symbol, peg_score=95.0, liquidity_score=80.0,
            reserve_score=75.0, adoption_score=70.0,
            overall_score=overall, scored_at=when or datetime.utcnow(),
        ))
        s.commit()


def _add_supply(symbol: str, supply: float = 1_000_000.0) -> None:
    with get_session() as s:
        s.add(SupplySnapshot(
            symbol=symbol, circulating_supply=supply,
            recorded_at=datetime.utcnow(),
        ))
        s.commit()


# ── service: create ───────────────────────────────────────────────────────────────

def test_create_known_symbol(in_memory_db):
    _add_stablecoin("USDT")
    a = create_alert("usdt", "peg_deviation_bps", 50.0, comparator="above")
    assert a is not None
    assert a["symbol"] == "USDT"          # normalised upper-case
    assert a["metric"] == "peg_deviation_bps"
    assert a["comparator"] == "above"
    assert a["threshold"] == 50.0
    assert a["severity"] == "medium"      # default
    assert a["active"] is True
    assert a["condition"] == "Peg deviation (bps) ≥ 50"


def test_create_unknown_symbol_returns_none(in_memory_db):
    assert create_alert("FAKE", "price", 0.99) is None
    assert list_alerts() == []


def test_create_defaults_comparator_to_metric_direction(in_memory_db):
    _add_stablecoin("USDC", name="USD Coin")
    # overall_score defaults to "below"; price defaults to "below"; peg to "above".
    assert create_alert("USDC", "overall_score", 70.0)["comparator"] == "below"
    assert create_alert("USDC", "peg_deviation_bps", 40.0)["comparator"] == "above"


def test_create_invalid_metric_raises(in_memory_db):
    _add_stablecoin("USDT")
    with pytest.raises(ValueError):
        create_alert("USDT", "not_a_metric", 1.0)


def test_create_invalid_comparator_raises(in_memory_db):
    _add_stablecoin("USDT")
    with pytest.raises(ValueError):
        create_alert("USDT", "price", 1.0, comparator="sideways")


def test_create_invalid_severity_raises(in_memory_db):
    _add_stablecoin("USDT")
    with pytest.raises(ValueError):
        create_alert("USDT", "price", 1.0, severity="critical")


def test_create_non_finite_threshold_raises(in_memory_db):
    _add_stablecoin("USDT")
    with pytest.raises(ValueError):
        create_alert("USDT", "price", float("inf"))


# ── service: evaluation semantics ──────────────────────────────────────────────────

def test_above_triggers_at_or_above_threshold(in_memory_db):
    _add_stablecoin("USDT")
    _add_price("USDT", dev=60.0)
    a = create_alert("USDT", "peg_deviation_bps", 50.0, comparator="above")
    assert a["triggered"] is True
    assert a["status"] == "triggered"
    assert a["current_value"] == pytest.approx(60.0)


def test_above_not_triggered_below_threshold(in_memory_db):
    _add_stablecoin("USDT")
    _add_price("USDT", dev=10.0)
    a = create_alert("USDT", "peg_deviation_bps", 50.0, comparator="above")
    assert a["triggered"] is False
    assert a["status"] == "ok"


def test_below_triggers_at_or_below_threshold(in_memory_db):
    _add_stablecoin("USDC", name="USD Coin")
    _add_score("USDC", overall=65.0)
    a = create_alert("USDC", "overall_score", 70.0, comparator="below")
    assert a["triggered"] is True
    assert a["current_value"] == pytest.approx(65.0)


def test_liquidity_uses_total_depth(in_memory_db):
    _add_stablecoin("USDT")
    _add_price("USDT", bid=400_000.0, ask=300_000.0)  # total 700k
    a = create_alert("USDT", "liquidity_usd", 1_000_000.0, comparator="below")
    assert a["current_value"] == pytest.approx(700_000.0)
    assert a["triggered"] is True


def test_no_data_does_not_trigger(in_memory_db):
    _add_stablecoin("DAI")
    a = create_alert("DAI", "price", 0.99, comparator="below")
    assert a["current_value"] is None
    assert a["triggered"] is False
    assert a["status"] == "no_data"


def test_paused_rule_never_triggers(in_memory_db):
    _add_stablecoin("USDT")
    _add_price("USDT", dev=99.0)
    a = create_alert("USDT", "peg_deviation_bps", 50.0, comparator="above", active=False)
    assert a["active"] is False
    assert a["triggered"] is False
    assert a["status"] == "paused"


def test_evaluation_uses_latest_snapshot(in_memory_db):
    _add_stablecoin("USDT")
    _add_price("USDT", dev=5.0, when=datetime.utcnow() - timedelta(hours=2))
    _add_price("USDT", dev=80.0, when=datetime.utcnow())
    a = create_alert("USDT", "peg_deviation_bps", 50.0, comparator="above")
    assert a["current_value"] == pytest.approx(80.0)
    assert a["triggered"] is True


# ── service: list / get / filters ─────────────────────────────────────────────────

def test_list_newest_first(in_memory_db):
    _add_stablecoin("USDT")
    with get_session() as s:
        base = datetime.utcnow()
        s.add(Alert(symbol="USDT", metric="price", comparator="below", threshold=0.99,
                    severity="low", active=True,
                    created_at=base - timedelta(minutes=10), updated_at=base))
        s.add(Alert(symbol="USDT", metric="peg_deviation_bps", comparator="above", threshold=50,
                    severity="high", active=True, created_at=base, updated_at=base))
        s.commit()
    metrics = [a["metric"] for a in list_alerts()]
    assert metrics == ["peg_deviation_bps", "price"]


def test_list_filters_by_symbol_and_active(in_memory_db):
    _add_stablecoin("USDT")
    _add_stablecoin("USDC", name="USD Coin")
    create_alert("USDT", "price", 0.99)
    create_alert("USDC", "price", 0.99)
    create_alert("USDC", "overall_score", 70.0, active=False)

    assert {a["symbol"] for a in list_alerts(symbol="usdc")} == {"USDC"}
    assert len(list_alerts(symbol="USDC")) == 2
    assert all(a["active"] for a in list_alerts(active_only=True))
    assert len(list_alerts(active_only=True)) == 2


def test_get_alert_and_missing(in_memory_db):
    _add_stablecoin("USDT")
    created = create_alert("USDT", "price", 0.99)
    assert get_alert(created["id"])["id"] == created["id"]
    assert get_alert(999999) is None


# ── service: update ───────────────────────────────────────────────────────────────

def test_update_threshold_and_severity(in_memory_db):
    _add_stablecoin("USDT")
    _add_price("USDT", dev=40.0)
    created = create_alert("USDT", "peg_deviation_bps", 50.0, comparator="above")
    assert created["triggered"] is False
    updated = update_alert(created["id"], threshold=30.0, severity="high")
    assert updated["threshold"] == 30.0
    assert updated["severity"] == "high"
    assert updated["triggered"] is True  # 40 >= 30 now


def test_update_toggle_active(in_memory_db):
    _add_stablecoin("USDT")
    created = create_alert("USDT", "price", 0.99)
    updated = update_alert(created["id"], active=False)
    assert updated["active"] is False
    assert updated["status"] == "paused"


def test_update_can_clear_note(in_memory_db):
    _add_stablecoin("USDT")
    created = create_alert("USDT", "price", 0.99, note="keep an eye")
    assert created["note"] == "keep an eye"
    updated = update_alert(created["id"], note=None)
    assert updated["note"] is None


def test_update_missing_returns_none(in_memory_db):
    assert update_alert(123456, threshold=1.0) is None


def test_update_invalid_comparator_raises(in_memory_db):
    _add_stablecoin("USDT")
    created = create_alert("USDT", "price", 0.99)
    with pytest.raises(ValueError):
        update_alert(created["id"], comparator="bogus")


# ── service: delete ───────────────────────────────────────────────────────────────

def test_delete_existing_and_absent(in_memory_db):
    _add_stablecoin("USDT")
    created = create_alert("USDT", "price", 0.99)
    assert delete_alert(created["id"]) is True
    assert get_alert(created["id"]) is None
    assert delete_alert(created["id"]) is False


# ── service: evaluate_alerts (pipeline step) ───────────────────────────────────────

def test_evaluate_persists_triggered_state(in_memory_db):
    _add_stablecoin("USDT")
    _add_price("USDT", dev=70.0)
    created = create_alert("USDT", "peg_deviation_bps", 50.0, comparator="above")
    fired = evaluate_alerts()
    assert [a["id"] for a in fired] == [created["id"]]
    with get_session() as s:
        row = s.get(Alert, created["id"])
        assert row.last_triggered_at is not None
        assert row.last_evaluated_at is not None
        assert row.last_value == pytest.approx(70.0)


def test_evaluate_skips_paused_and_untriggered(in_memory_db):
    _add_stablecoin("USDT")
    _add_price("USDT", dev=5.0)
    create_alert("USDT", "peg_deviation_bps", 50.0, comparator="above")          # not triggered
    create_alert("USDT", "peg_deviation_bps", 1.0, comparator="above", active=False)  # paused
    assert evaluate_alerts() == []


def test_evaluate_is_idempotent(in_memory_db):
    _add_stablecoin("USDT")
    _add_price("USDT", dev=70.0)
    create_alert("USDT", "peg_deviation_bps", 50.0, comparator="above")
    first = evaluate_alerts()
    second = evaluate_alerts()
    assert len(first) == len(second) == 1  # no duplicate rows created


# ── API endpoints ─────────────────────────────────────────────────────────────────

def test_get_alerts_empty(in_memory_db):
    resp = client.get("/alerts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["alerts"] == []
    assert body["triggered_count"] == 0
    assert "peg_deviation_bps" in body["metrics"]["supported"]
    assert "above" in body["metrics"]["comparators"]


def test_post_alert_known_symbol(in_memory_db):
    _add_stablecoin("USDT")
    _add_price("USDT", dev=60.0)
    resp = client.post("/alerts", json={
        "symbol": "usdt", "metric": "peg_deviation_bps",
        "threshold": 50.0, "comparator": "above", "severity": "high",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["symbol"] == "USDT"
    assert body["triggered"] is True
    listed = client.get("/alerts").json()
    assert listed["triggered_count"] == 1


def test_post_alert_unknown_symbol_404(in_memory_db):
    resp = client.post("/alerts", json={"symbol": "FAKE", "metric": "price", "threshold": 1.0})
    assert resp.status_code == 404


def test_post_alert_invalid_metric_422(in_memory_db):
    _add_stablecoin("USDT")
    resp = client.post("/alerts", json={"symbol": "USDT", "metric": "nope", "threshold": 1.0})
    assert resp.status_code == 422


def test_patch_alert(in_memory_db):
    _add_stablecoin("USDT")
    created = client.post("/alerts", json={
        "symbol": "USDT", "metric": "price", "threshold": 0.99, "comparator": "below",
    }).json()
    resp = client.patch(f"/alerts/{created['id']}", json={"threshold": 0.95, "active": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["threshold"] == 0.95
    assert body["active"] is False


def test_patch_alert_empty_body_422(in_memory_db):
    _add_stablecoin("USDT")
    created = client.post(
        "/alerts", json={"symbol": "USDT", "metric": "price", "threshold": 0.99}
    ).json()
    resp = client.patch(f"/alerts/{created['id']}", json={})
    assert resp.status_code == 422


def test_patch_alert_missing_404(in_memory_db):
    resp = client.patch("/alerts/424242", json={"threshold": 1.0})
    assert resp.status_code == 404


def test_delete_alert_endpoint(in_memory_db):
    _add_stablecoin("USDT")
    created = client.post(
        "/alerts", json={"symbol": "USDT", "metric": "price", "threshold": 0.99}
    ).json()
    resp = client.delete(f"/alerts/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    assert client.get("/alerts").json()["alerts"] == []


def test_delete_alert_absent_404(in_memory_db):
    resp = client.delete("/alerts/999")
    assert resp.status_code == 404
