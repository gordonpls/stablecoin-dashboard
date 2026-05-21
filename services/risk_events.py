"""Service: detect and persist notable risk events, and query the timeline.

A *risk event* marks the moment a tracked metric crossed into (or moved
sharply within) a stressed state — a peg widening past a threshold, liquidity
draining, a supply shock, a sharp risk-score move, a stale reserve report, or a
provider that keeps failing. Events give users *context over time* rather than
just the current snapshot.

Detection compares the two most recent snapshots of each metric (newest vs the
prior one) so events represent *step changes* between consecutive snapshots
rather than sustained states — a peg sitting at 60 bps for days produces one
event when it crosses, not one every refresh. Idempotency comes from
de-duplicating on (symbol, event_type, triggered_at, metric_name): re-running
detection over unchanged data inserts nothing, because ``triggered_at`` is
derived from the underlying data point's timestamp, not the wall clock.

``log_new_events`` is called by the scheduled scoring pipeline; ``query_events``
backs the FastAPI ``/risk-events`` and ``/stablecoins/{symbol}/events``
endpoints and the Streamlit "Risk Events" tab, so detection logic and the UI
never drift apart.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import func, select

from db.models import (
    ApiRequestLog,
    PriceSnapshot,
    ReserveReport,
    RiskEvent,
    RiskScore,
    SupplySnapshot,
    get_session,
)

# ── event types ───────────────────────────────────────────────────────────────

PEG_DEVIATION = "PEG_DEVIATION"
LIQUIDITY_DROP = "LIQUIDITY_DROP"
SUPPLY_SHOCK = "SUPPLY_SHOCK"
SCORE_CHANGE = "SCORE_CHANGE"
RESERVE_STALE = "RESERVE_STALE"
API_FAILURE = "API_FAILURE"

EVENT_TYPES = (
    PEG_DEVIATION, LIQUIDITY_DROP, SUPPLY_SHOCK,
    SCORE_CHANGE, RESERVE_STALE, API_FAILURE,
)
SEVERITIES = ("low", "medium", "high")

# Subject of system-wide events that are not tied to a single asset.
SYSTEM_SYMBOL = "SYSTEM"

# ── thresholds ──────────────────────────────────────────────────────────────────

# Peg deviation (bps): crossing UP through the medium band is the event.
PEG_MEDIUM_BPS = 25.0
PEG_HIGH_BPS = 50.0

# Liquidity depth: percent drop between consecutive snapshots.
LIQ_DROP_LOW = 15.0
LIQ_DROP_MEDIUM = 20.0
LIQ_DROP_HIGH = 30.0

# Circulating supply: percent change between consecutive snapshots.
SUPPLY_SHOCK_LOW = 5.0
SUPPLY_SHOCK_MEDIUM = 7.0
SUPPLY_SHOCK_HIGH = 10.0

# Overall risk score: point change between consecutive snapshots.
SCORE_CHANGE_LOW = 10.0
SCORE_CHANGE_MEDIUM = 15.0
SCORE_CHANGE_HIGH = 20.0

# Reserve report age (days) — mirrors the scoring pipeline's staleness window.
STALE_RESERVE_DAYS = 90
RESERVE_STALE_HIGH_DAYS = 180

# Provider failures: count within the window that constitutes "repeatedly failing".
API_FAILURE_WINDOW = timedelta(hours=24)
API_FAILURE_MIN = 3
API_FAILURE_HIGH = 10


def _depth(row: PriceSnapshot) -> float | None:
    total = (row.bid_depth_usd or 0) + (row.ask_depth_usd or 0)
    return total if total > 0 else None


def _symbols(session, symbol_col) -> list[str]:
    return session.execute(select(symbol_col).distinct()).scalars().all()


def _recent_points(
    session, model, ts_attr, symbol: str, value_fn: Callable, limit: int = 200,
) -> list[tuple[datetime, float]]:
    """Return (timestamp, value) pairs newest-first, with nulls dropped.

    Same-timestamp duplicates are collapsed to the largest value: several
    DefiLlama assets can share a ticker, so one supply run writes multiple rows
    per symbol at the same instant. Collapsing keeps comparisons on the dominant
    asset instead of an arbitrary collision row (mirrors services/market_changes).
    """
    rows = (
        session.execute(
            select(model).where(model.symbol == symbol).order_by(ts_attr.desc()).limit(limit)
        )
        .scalars()
        .all()
    )
    by_ts: dict[datetime, float] = {}
    for r in rows:
        value = value_fn(r)
        if value is None:
            continue
        ts = getattr(r, ts_attr.key)
        if ts not in by_ts or value > by_ts[ts]:
            by_ts[ts] = value
    return [(ts, by_ts[ts]) for ts in sorted(by_ts, reverse=True)]


def _band(magnitude: float, low: float, medium: float, high: float) -> str:
    if magnitude >= high:
        return "high"
    if magnitude >= medium:
        return "medium"
    return "low"


# ── detectors ─────────────────────────────────────────────────────────────────
# Each returns unsaved RiskEvent rows; de-duplication happens in log_new_events.

def _detect_peg(session) -> list[RiskEvent]:
    events: list[RiskEvent] = []
    for symbol in _symbols(session, PriceSnapshot.symbol):
        pts = _recent_points(session, PriceSnapshot, PriceSnapshot.recorded_at,
                             symbol, lambda r: r.peg_deviation_bps)
        if len(pts) < 2:
            continue
        (cur_ts, cur), (_, prev) = pts[0], pts[1]
        # Event only on the upward crossing into the stress band.
        if not (cur >= PEG_MEDIUM_BPS > prev):
            continue
        severity = "high" if cur >= PEG_HIGH_BPS else "medium"
        events.append(RiskEvent(
            symbol=symbol, event_type=PEG_DEVIATION, severity=severity,
            title=f"{symbol} peg deviation crossed {cur:.0f} bps",
            description=(
                f"Peg deviation widened from {prev:.0f} bps to {cur:.0f} bps, "
                f"crossing the {PEG_MEDIUM_BPS:.0f} bps stress threshold."
            ),
            metric_name="peg_deviation_bps",
            previous_value=round(prev, 2), current_value=round(cur, 2),
            triggered_at=cur_ts,
        ))
    return events


def _detect_liquidity(session) -> list[RiskEvent]:
    events: list[RiskEvent] = []
    for symbol in _symbols(session, PriceSnapshot.symbol):
        pts = _recent_points(session, PriceSnapshot, PriceSnapshot.recorded_at, symbol, _depth)
        if len(pts) < 2:
            continue
        (cur_ts, cur), (_, prev) = pts[0], pts[1]
        if prev <= 0:
            continue
        pct = (cur - prev) / prev * 100.0
        if pct > -LIQ_DROP_LOW:  # not a large enough drop
            continue
        drop = abs(pct)
        events.append(RiskEvent(
            symbol=symbol, event_type=LIQUIDITY_DROP,
            severity=_band(drop, LIQ_DROP_LOW, LIQ_DROP_MEDIUM, LIQ_DROP_HIGH),
            title=f"{symbol} liquidity dropped {drop:.0f}%",
            description=(
                f"Order book depth fell {drop:.0f}% from ${prev:,.0f} to ${cur:,.0f} "
                f"between snapshots."
            ),
            metric_name="liquidity_usd",
            previous_value=round(prev, 2), current_value=round(cur, 2),
            triggered_at=cur_ts,
        ))
    return events


def _detect_supply(session) -> list[RiskEvent]:
    events: list[RiskEvent] = []
    for symbol in _symbols(session, SupplySnapshot.symbol):
        pts = _recent_points(session, SupplySnapshot, SupplySnapshot.recorded_at,
                             symbol, lambda r: r.circulating_supply)
        if len(pts) < 2:
            continue
        (cur_ts, cur), (_, prev) = pts[0], pts[1]
        if prev <= 0:
            continue
        pct = (cur - prev) / prev * 100.0
        if abs(pct) < SUPPLY_SHOCK_LOW:
            continue
        direction = "surged" if pct >= 0 else "contracted"
        events.append(RiskEvent(
            symbol=symbol, event_type=SUPPLY_SHOCK,
            severity=_band(abs(pct), SUPPLY_SHOCK_LOW, SUPPLY_SHOCK_MEDIUM, SUPPLY_SHOCK_HIGH),
            title=f"{symbol} supply {direction} {abs(pct):.1f}%",
            description=(
                f"Circulating supply moved {pct:+.1f}% from ${prev:,.0f} to ${cur:,.0f} "
                f"between snapshots."
            ),
            metric_name="circulating_supply",
            previous_value=round(prev, 2), current_value=round(cur, 2),
            triggered_at=cur_ts,
        ))
    return events


def _detect_score(session) -> list[RiskEvent]:
    events: list[RiskEvent] = []
    for symbol in _symbols(session, RiskScore.symbol):
        pts = _recent_points(session, RiskScore, RiskScore.scored_at,
                             symbol, lambda r: r.overall_score)
        if len(pts) < 2:
            continue
        (cur_ts, cur), (_, prev) = pts[0], pts[1]
        delta = cur - prev
        if abs(delta) < SCORE_CHANGE_LOW:
            continue
        direction = "rose" if delta >= 0 else "fell"
        events.append(RiskEvent(
            symbol=symbol, event_type=SCORE_CHANGE,
            severity=_band(abs(delta), SCORE_CHANGE_LOW, SCORE_CHANGE_MEDIUM, SCORE_CHANGE_HIGH),
            title=f"{symbol} risk score {direction} {abs(delta):.0f} points",
            description=(
                f"Overall risk score moved from {prev:.0f} to {cur:.0f} between snapshots."
            ),
            metric_name="overall_score",
            previous_value=round(prev, 2), current_value=round(cur, 2),
            triggered_at=cur_ts,
        ))
    return events


def _detect_reserve_stale(session, now: datetime) -> list[RiskEvent]:
    events: list[RiskEvent] = []
    for symbol in _symbols(session, ReserveReport.symbol):
        report = (
            session.execute(
                select(ReserveReport)
                .where(ReserveReport.symbol == symbol)
                .order_by(ReserveReport.ingested_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if report is None or report.report_date is None:
            continue
        age = (now.date() - report.report_date).days
        if age < STALE_RESERVE_DAYS:
            continue
        auditor = report.auditor or "an undisclosed attestor"
        events.append(RiskEvent(
            symbol=symbol, event_type=RESERVE_STALE,
            severity="high" if age >= RESERVE_STALE_HIGH_DAYS else "medium",
            title=f"{symbol} reserve report is {age} days old",
            description=(
                f"Latest reserve attestation is dated {report.report_date.isoformat()} "
                f"({age} days old), past the {STALE_RESERVE_DAYS}-day freshness window. "
                f"Attested by {auditor}."
            ),
            metric_name="reserve_age_days",
            previous_value=None, current_value=float(age),
            # One event per report: ingested_at is stable for a given report.
            triggered_at=report.ingested_at or now,
        ))
    return events


def _detect_api_failures(session, now: datetime) -> list[RiskEvent]:
    cutoff = now - API_FAILURE_WINDOW
    failing = (ApiRequestLog.status_code.is_(None)) | (ApiRequestLog.status_code >= 400)
    rows = session.execute(
        select(
            ApiRequestLog.provider,
            func.count().label("cnt"),
            func.max(ApiRequestLog.requested_at).label("last_at"),
        )
        .where(ApiRequestLog.requested_at >= cutoff, failing)
        .group_by(ApiRequestLog.provider)
    ).all()

    hours = int(API_FAILURE_WINDOW.total_seconds() // 3600)
    events: list[RiskEvent] = []
    for provider, cnt, last_at in rows:
        if cnt < API_FAILURE_MIN:
            continue
        events.append(RiskEvent(
            symbol=SYSTEM_SYMBOL, event_type=API_FAILURE,
            severity="high" if cnt >= API_FAILURE_HIGH else "medium",
            title=f"{provider} API repeatedly failing",
            description=f"{cnt} failed {provider} requests in the last {hours}h.",
            metric_name=provider,
            previous_value=None, current_value=float(cnt),
            triggered_at=last_at,
        ))
    return events


def _already_logged(session, ev: RiskEvent) -> bool:
    """True if an identical event (same key) has already been persisted."""
    q = select(RiskEvent.id).where(
        RiskEvent.symbol == ev.symbol,
        RiskEvent.event_type == ev.event_type,
        RiskEvent.triggered_at == ev.triggered_at,
    )
    if ev.metric_name is not None:
        q = q.where(RiskEvent.metric_name == ev.metric_name)
    return session.execute(q.limit(1)).first() is not None


def log_new_events(now: datetime | None = None) -> list[dict]:
    """Detect events from current data and persist the ones not already logged.

    Idempotent: returns only the rows it actually inserted (as dicts). Safe to
    call from any scheduled job, including repeatedly on unchanged data.
    """
    now = now or datetime.utcnow()
    inserted: list[dict] = []

    with get_session() as session:
        candidates = (
            _detect_peg(session)
            + _detect_liquidity(session)
            + _detect_supply(session)
            + _detect_score(session)
            + _detect_reserve_stale(session, now)
            + _detect_api_failures(session, now)
        )
        for ev in candidates:
            if _already_logged(session, ev):
                continue
            session.add(ev)
            session.flush()  # populate id before snapshotting to a dict
            inserted.append(ev.to_dict())
        session.commit()

    return inserted


def query_events(
    symbol: str | None = None,
    severity: str | None = None,
    event_type: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return risk events newest-first, optionally filtered.

    Filters are applied in the database. ``symbol`` is matched upper-case;
    asset queries therefore exclude system-wide events (``symbol = "SYSTEM"``).
    """
    with get_session() as session:
        q = select(RiskEvent).order_by(RiskEvent.triggered_at.desc(), RiskEvent.id.desc())
        if symbol:
            q = q.where(RiskEvent.symbol == symbol.upper())
        if severity:
            q = q.where(RiskEvent.severity == severity.lower())
        if event_type:
            q = q.where(RiskEvent.event_type == event_type.upper())
        rows = session.execute(q.limit(limit)).scalars().all()
        return [r.to_dict() for r in rows]
