"""Service: user-defined alert rules — thresholds on one metric for one asset.

An *alert* is an explicit, persistent rule the operator creates to watch a
single metric on a single asset — e.g. "USDT peg deviation at or above 50 bps"
or "USDC overall risk score at or below 70". This is deliberately different from
two adjacent surfaces:

* ``services.risk_events`` *auto-detects* notable step changes from the data
  (a peg widening, a liquidity drop). Alerts are operator-defined thresholds,
  not detected events.
* ``services.data_validation`` flags data that looks *wrong*. Alerts watch data
  that looks *fine but undesirable*.

Rather than re-implement a parallel detector, evaluation reuses the exact
latest-value primitives risk_events uses (``_recent_points`` / ``_depth``), so
an alert and the risk-event timeline can never read a metric differently — the
same same-timestamp ticker-collision collapse applies to both.

A rule's ``comparator`` is "above" (fires when ``value >= threshold``) or
"below" (fires when ``value <= threshold``). A rule with no data for its metric
never fires (``status = "no_data"``); a paused rule (``active = False``) is
never evaluated as triggered.

Write helpers normalise the symbol and only accept assets present in
``stablecoins`` (unknown symbols are rejected, never invented), mirroring
``services.watchlist``. Editing in the dashboard is gated behind the dashboard
password (anonymous controls that change app behaviour are not allowed).
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from sqlalchemy import select

from db.models import (
    Alert,
    PriceSnapshot,
    RiskScore,
    Stablecoin,
    SupplySnapshot,
    get_session,
)

# Reuse the canonical latest-value primitives so an alert reads a metric exactly
# as the risk-event detector does (same null-dropping + same-timestamp collapse).
from services.risk_events import _depth, _recent_points

# ── metric registry ─────────────────────────────────────────────────────────────

# Each metric maps to the snapshot it is read from: (model, timestamp column,
# value extractor). The extractor returns None when the snapshot lacks the value.
_METRIC_SOURCES: dict[str, tuple[Any, Any, Any]] = {
    "peg_deviation_bps": (PriceSnapshot, PriceSnapshot.recorded_at, lambda r: r.peg_deviation_bps),
    "price": (PriceSnapshot, PriceSnapshot.recorded_at, lambda r: r.price),
    "liquidity_usd": (PriceSnapshot, PriceSnapshot.recorded_at, _depth),
    "overall_score": (RiskScore, RiskScore.scored_at, lambda r: r.overall_score),
    "circulating_supply": (
        SupplySnapshot, SupplySnapshot.recorded_at, lambda r: r.circulating_supply,
    ),
}

# Catalogue surfaced to the UI / API clients building an alert form. Each entry's
# default_comparator is the direction that signals *trouble* for that metric.
SUPPORTED_METRICS: dict[str, dict[str, str]] = {
    "peg_deviation_bps": {
        "label": "Peg deviation (bps)", "unit": "bps", "default_comparator": "above",
    },
    "price": {
        "label": "Price (USD)", "unit": "USD", "default_comparator": "below",
    },
    "liquidity_usd": {
        "label": "Order-book depth (USD)", "unit": "USD", "default_comparator": "below",
    },
    "overall_score": {
        "label": "Overall risk score", "unit": "pts", "default_comparator": "below",
    },
    "circulating_supply": {
        "label": "Circulating supply (USD)", "unit": "USD", "default_comparator": "below",
    },
}

# "above" fires when value >= threshold; "below" fires when value <= threshold.
COMPARATORS = ("above", "below")
SEVERITIES = ("low", "medium", "high")

# Sentinel so update_alert can tell "field omitted" from "field set to None".
_UNSET: Any = object()


# ── helpers ──────────────────────────────────────────────────────────────────────

def _normalize_symbol(symbol: str | None) -> str:
    return (symbol or "").strip().upper()


def _validate(metric: str, comparator: str, severity: str, threshold: Any) -> float:
    """Validate rule fields, returning the coerced threshold. Raises ValueError."""
    if metric not in SUPPORTED_METRICS:
        raise ValueError(
            f"unsupported metric '{metric}'; choose one of {sorted(SUPPORTED_METRICS)}"
        )
    if comparator not in COMPARATORS:
        raise ValueError(f"comparator must be one of {list(COMPARATORS)}")
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {list(SEVERITIES)}")
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        raise ValueError("threshold must be a number")
    if not math.isfinite(threshold):
        raise ValueError("threshold must be a finite number")
    return threshold


def _latest_value(session, symbol: str, metric: str) -> tuple[float | None, datetime | None]:
    """Return the (value, timestamp) of the most recent snapshot for the metric."""
    model, ts_attr, value_fn = _METRIC_SOURCES[metric]
    pts = _recent_points(session, model, ts_attr, symbol, value_fn, limit=5)
    if not pts:
        return None, None
    ts, value = pts[0]
    return value, ts


def _is_triggered(comparator: str, value: float | None, threshold: float) -> bool:
    if value is None:
        return False
    if comparator == "above":
        return value >= threshold
    return value <= threshold  # "below"


def _condition_text(alert: Alert) -> str:
    label = SUPPORTED_METRICS.get(alert.metric, {}).get("label", alert.metric)
    rel = "≥" if alert.comparator == "above" else "≤"  # ≥ / ≤
    return f"{label} {rel} {alert.threshold:g}"


def _serialize(
    alert: Alert,
    *,
    current_value: float | None,
    triggered: bool,
    metric_ts: datetime | None,
) -> dict:
    if not alert.active:
        status = "paused"
    elif current_value is None:
        status = "no_data"
    elif triggered:
        status = "triggered"
    else:
        status = "ok"
    info = SUPPORTED_METRICS.get(alert.metric, {})
    last_eval = alert.last_evaluated_at.isoformat() if alert.last_evaluated_at else None
    last_trig = alert.last_triggered_at.isoformat() if alert.last_triggered_at else None
    return {
        "id": alert.id,
        "symbol": alert.symbol,
        "metric": alert.metric,
        "metric_label": info.get("label", alert.metric),
        "metric_unit": info.get("unit"),
        "comparator": alert.comparator,
        "threshold": alert.threshold,
        "severity": alert.severity,
        "note": alert.note,
        "active": bool(alert.active),
        "condition": _condition_text(alert),
        "current_value": current_value,
        "metric_recorded_at": metric_ts.isoformat() if metric_ts else None,
        "triggered": triggered,
        "status": status,
        "created_at": alert.created_at.isoformat() if alert.created_at else None,
        "updated_at": alert.updated_at.isoformat() if alert.updated_at else None,
        "last_evaluated_at": last_eval,
        "last_triggered_at": last_trig,
        "last_value": alert.last_value,
    }


# ── CRUD ─────────────────────────────────────────────────────────────────────────

def create_alert(
    symbol: str,
    metric: str,
    threshold: Any,
    comparator: str | None = None,
    severity: str = "medium",
    note: str | None = None,
    active: bool = True,
) -> dict | None:
    """Create an alert rule.

    Returns the stored rule (with a live evaluation) as a dict, or ``None`` when
    ``symbol`` is not a tracked stablecoin. When ``comparator`` is omitted, the
    metric's default direction is used. Raises ``ValueError`` for an unsupported
    metric/comparator/severity or a non-finite threshold.
    """
    comparator = comparator or SUPPORTED_METRICS.get(metric, {}).get("default_comparator", "above")
    threshold = _validate(metric, comparator, severity, threshold)
    sym = _normalize_symbol(symbol)
    if not sym:
        return None
    with get_session() as session:
        known = session.execute(
            select(Stablecoin.symbol).where(Stablecoin.symbol == sym)
        ).scalar_one_or_none()
        if known is None:
            return None

        now = datetime.utcnow()
        alert = Alert(
            symbol=sym, metric=metric, comparator=comparator, threshold=threshold,
            severity=severity, note=note, active=bool(active),
            created_at=now, updated_at=now,
        )
        session.add(alert)
        session.commit()
        session.refresh(alert)

        value, ts = _latest_value(session, sym, metric)
        triggered = bool(alert.active) and _is_triggered(comparator, value, threshold)
        return _serialize(alert, current_value=value, triggered=triggered, metric_ts=ts)


def list_alerts(
    symbol: str | None = None,
    active_only: bool = False,
    evaluate: bool = True,
) -> list[dict]:
    """Return alert rules newest-first, each with a live evaluation.

    Filter by ``symbol`` and/or ``active_only``. When ``evaluate`` is True
    (default) each rule carries its current metric value and triggered status,
    computed read-only from the latest snapshots (no writes). Latest values are
    cached per (symbol, metric) within the call so many rules on one asset cost
    one lookup.
    """
    with get_session() as session:
        q = select(Alert).order_by(Alert.created_at.desc(), Alert.id.desc())
        if symbol:
            q = q.where(Alert.symbol == _normalize_symbol(symbol))
        if active_only:
            q = q.where(Alert.active.is_(True))
        rows = session.execute(q).scalars().all()

        cache: dict[tuple[str, str], tuple[float | None, datetime | None]] = {}
        result = []
        for a in rows:
            if evaluate:
                key = (a.symbol, a.metric)
                if key not in cache:
                    cache[key] = _latest_value(session, a.symbol, a.metric)
                value, ts = cache[key]
                triggered = bool(a.active) and _is_triggered(a.comparator, value, a.threshold)
            else:
                value, ts, triggered = None, None, False
            result.append(_serialize(a, current_value=value, triggered=triggered, metric_ts=ts))
        return result


def get_alert(alert_id: int) -> dict | None:
    """Return a single alert rule (with a live evaluation), or ``None`` if absent."""
    with get_session() as session:
        a = session.get(Alert, alert_id)
        if a is None:
            return None
        value, ts = _latest_value(session, a.symbol, a.metric)
        triggered = bool(a.active) and _is_triggered(a.comparator, value, a.threshold)
        return _serialize(a, current_value=value, triggered=triggered, metric_ts=ts)


def update_alert(
    alert_id: int,
    *,
    threshold: Any = _UNSET,
    comparator: Any = _UNSET,
    severity: Any = _UNSET,
    note: Any = _UNSET,
    active: Any = _UNSET,
) -> dict | None:
    """Partially update an alert rule.

    Only the provided fields change; ``metric`` and ``symbol`` are immutable
    (delete and recreate to change them, keeping rule identity stable). Returns
    the updated rule, or ``None`` when no rule has ``alert_id``. Raises
    ``ValueError`` for an invalid comparator/severity/threshold.
    """
    with get_session() as session:
        a = session.get(Alert, alert_id)
        if a is None:
            return None

        new_comparator = a.comparator if comparator is _UNSET else comparator
        new_severity = a.severity if severity is _UNSET else severity
        new_threshold = a.threshold if threshold is _UNSET else threshold
        # Validate the resulting rule against its (unchangeable) metric.
        new_threshold = _validate(a.metric, new_comparator, new_severity, new_threshold)

        a.comparator = new_comparator
        a.severity = new_severity
        a.threshold = new_threshold
        if note is not _UNSET:
            a.note = note
        if active is not _UNSET:
            a.active = bool(active)
        a.updated_at = datetime.utcnow()
        session.commit()
        session.refresh(a)

        value, ts = _latest_value(session, a.symbol, a.metric)
        triggered = bool(a.active) and _is_triggered(a.comparator, value, a.threshold)
        return _serialize(a, current_value=value, triggered=triggered, metric_ts=ts)


def delete_alert(alert_id: int) -> bool:
    """Delete an alert rule. Returns True if a row was removed."""
    with get_session() as session:
        a = session.get(Alert, alert_id)
        if a is None:
            return False
        session.delete(a)
        session.commit()
        return True


def evaluate_alerts(now: datetime | None = None) -> list[dict]:
    """Evaluate every active rule against the latest data and persist its state.

    For each active alert this records ``last_value`` and ``last_evaluated_at``,
    and stamps ``last_triggered_at`` when the rule is currently in breach.
    Returns the currently-triggered rules (as dicts). Idempotent and
    best-effort-safe to call from the scoring pipeline on every run; paused
    rules are skipped entirely.
    """
    now = now or datetime.utcnow()
    triggered_out: list[dict] = []
    with get_session() as session:
        rows = session.execute(
            select(Alert).where(Alert.active.is_(True))
        ).scalars().all()

        cache: dict[tuple[str, str], tuple[float | None, datetime | None]] = {}
        for a in rows:
            key = (a.symbol, a.metric)
            if key not in cache:
                cache[key] = _latest_value(session, a.symbol, a.metric)
            value, _ts = cache[key]
            a.last_value = value
            a.last_evaluated_at = now
            if _is_triggered(a.comparator, value, a.threshold):
                a.last_triggered_at = now
        session.commit()

        for a in rows:
            value, ts = cache[(a.symbol, a.metric)]
            if _is_triggered(a.comparator, value, a.threshold):
                session.refresh(a)
                triggered_out.append(
                    _serialize(a, current_value=value, triggered=True, metric_ts=ts)
                )
    return triggered_out
