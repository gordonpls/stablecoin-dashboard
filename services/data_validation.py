"""Service: detect, persist, and query data-quality warnings.

A *data-quality warning* flags stored data that looks **wrong, implausible, or
incomplete** — as opposed to ``services/risk_events`` (which marks genuine
*market* moves like a peg break) or ``services/freshness`` (which tracks how
*current* each source is). The rules here mirror the validation rules in the
backlog:

* ``IMPOSSIBLE_PRICE``            — latest stablecoin price is outside [0.90, 1.10]
* ``NON_POSITIVE_SUPPLY``         — latest circulating supply is <= 0
* ``PEG_DEVIATION_MISMATCH``      — stored ``peg_deviation_bps`` disagrees with the
  value implied by ``price`` (``|price - 1| * 10_000``)
* ``SUPPLY_JUMP``                 — supply moved implausibly far between snapshots
  (a data error / ticker collision, not a normal market move)
* ``DUPLICATE_SNAPSHOT``          — a symbol has more than one supply row at its
  latest timestamp (the documented DefiLlama ticker-collision problem)
* ``MISSING_CHAIN_DISTRIBUTION``  — latest supply snapshot has no chain breakdown

Warnings have a lifecycle. A row is *opened* (``resolved_at`` NULL) when a
problem is first detected, and *resolved* (``resolved_at`` set) on the first run
where the underlying data no longer trips the rule. Identity is
``(symbol, metric_name, warning_type)`` among the open rows, so re-running
``run_validation`` over an unchanged problem inserts nothing and resolves
nothing — it is idempotent.

``run_validation`` is called by the scheduled scoring pipeline; ``query_warnings``
and ``warning_summary`` back the FastAPI ``/data-quality`` endpoint and the
Streamlit "Data Quality" panel, so detection logic and the UI never drift apart.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from db.models import (
    DataQualityWarning,
    PriceSnapshot,
    SupplySnapshot,
    get_session,
)

logger = logging.getLogger(__name__)

# ── warning types ─────────────────────────────────────────────────────────────

IMPOSSIBLE_PRICE = "IMPOSSIBLE_PRICE"
NON_POSITIVE_SUPPLY = "NON_POSITIVE_SUPPLY"
PEG_DEVIATION_MISMATCH = "PEG_DEVIATION_MISMATCH"
SUPPLY_JUMP = "SUPPLY_JUMP"
DUPLICATE_SNAPSHOT = "DUPLICATE_SNAPSHOT"
MISSING_CHAIN_DISTRIBUTION = "MISSING_CHAIN_DISTRIBUTION"

WARNING_TYPES = (
    IMPOSSIBLE_PRICE, NON_POSITIVE_SUPPLY, PEG_DEVIATION_MISMATCH,
    SUPPLY_JUMP, DUPLICATE_SNAPSHOT, MISSING_CHAIN_DISTRIBUTION,
)
SEVERITIES = ("low", "medium", "high")

# ── thresholds ────────────────────────────────────────────────────────────────

# A fiat stablecoin trading outside this band is almost certainly bad data.
PRICE_MIN = 0.90
PRICE_MAX = 1.10

# peg_deviation_bps is computed as round(|price - 1| * 10_000, 2); allow a small
# tolerance for that rounding before flagging the two as inconsistent.
PEG_TOLERANCE_BPS = 2.0

# Single-interval supply move large enough to imply a data error rather than a
# market move. Deliberately far above the SUPPLY_SHOCK risk-event band (5–10%)
# so the two surfaces do not flag the same ordinary fluctuation.
SUPPLY_JUMP_PCT = 50.0
SUPPLY_JUMP_HIGH_PCT = 100.0


@dataclass(frozen=True)
class _Candidate:
    """A currently-detected problem, before it is reconciled with open rows."""

    symbol: str | None
    provider: str | None
    metric_name: str
    warning_type: str
    severity: str
    message: str

    @property
    def key(self) -> tuple[str | None, str, str]:
        return (self.symbol, self.metric_name, self.warning_type)


# ── helpers ───────────────────────────────────────────────────────────────────

def _symbols(session, symbol_col) -> list[str]:
    return session.execute(select(symbol_col).distinct()).scalars().all()


def _latest_price(session, symbol: str) -> PriceSnapshot | None:
    return session.execute(
        select(PriceSnapshot)
        .where(PriceSnapshot.symbol == symbol)
        .order_by(PriceSnapshot.recorded_at.desc())
        .limit(1)
    ).scalars().first()


def _latest_supply_rows(session, symbol: str) -> list[SupplySnapshot]:
    """All supply rows sharing the symbol's most recent timestamp.

    More than one means a ticker collision (several DefiLlama assets reported
    under the same ticker at the same instant) — see DUPLICATE_SNAPSHOT.
    """
    max_ts = session.execute(
        select(func.max(SupplySnapshot.recorded_at)).where(SupplySnapshot.symbol == symbol)
    ).scalar_one_or_none()
    if max_ts is None:
        return []
    return session.execute(
        select(SupplySnapshot)
        .where(SupplySnapshot.symbol == symbol, SupplySnapshot.recorded_at == max_ts)
    ).scalars().all()


def _dominant(rows: list[SupplySnapshot]) -> SupplySnapshot | None:
    """The largest-supply row in a group — the asset a shared ticker mostly is."""
    if not rows:
        return None
    return max(rows, key=lambda r: r.circulating_supply or 0)


def _supply_points(session, symbol: str) -> list[tuple[datetime, float]]:
    """(timestamp, dominant supply) newest-first, one entry per distinct time.

    Same-timestamp collisions collapse to the largest value so a SUPPLY_JUMP
    comparison tracks the dominant asset, mirroring ``services/risk_events``.
    """
    rows = session.execute(
        select(SupplySnapshot)
        .where(SupplySnapshot.symbol == symbol)
        .order_by(SupplySnapshot.recorded_at.desc())
        .limit(200)
    ).scalars().all()
    by_ts: dict[datetime, float] = {}
    for r in rows:
        value = r.circulating_supply
        if value is None:
            continue
        if r.recorded_at not in by_ts or value > by_ts[r.recorded_at]:
            by_ts[r.recorded_at] = value
    return [(ts, by_ts[ts]) for ts in sorted(by_ts, reverse=True)]


def _has_chain_distribution(raw: str | None) -> bool:
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return isinstance(data, dict) and len(data) > 0


# ── detectors ─────────────────────────────────────────────────────────────────
# Each returns _Candidate rows for currently-failing checks. Lifecycle handling
# (open vs already-open vs resolve) is done once in run_validation.

def _detect_price_issues(session) -> list[_Candidate]:
    out: list[_Candidate] = []
    for symbol in _symbols(session, PriceSnapshot.symbol):
        row = _latest_price(session, symbol)
        if row is None or row.price is None:
            continue
        provider = (row.source or "").title() or None

        if row.price < PRICE_MIN or row.price > PRICE_MAX:
            out.append(_Candidate(
                symbol=symbol, provider=provider, metric_name="price",
                warning_type=IMPOSSIBLE_PRICE, severity="high",
                message=(
                    f"{symbol} latest price ${row.price:,.4f} is outside the "
                    f"plausible stablecoin band [{PRICE_MIN:.2f}, {PRICE_MAX:.2f}]."
                ),
            ))

        if row.peg_deviation_bps is not None:
            expected = abs(row.price - 1.0) * 10_000
            if abs(row.peg_deviation_bps - expected) > PEG_TOLERANCE_BPS:
                out.append(_Candidate(
                    symbol=symbol, provider=provider, metric_name="peg_deviation_bps",
                    warning_type=PEG_DEVIATION_MISMATCH, severity="medium",
                    message=(
                        f"{symbol} stored peg deviation {row.peg_deviation_bps:.1f} bps "
                        f"disagrees with the {expected:.1f} bps implied by price "
                        f"${row.price:,.4f}."
                    ),
                ))
    return out


def _detect_supply_issues(session) -> list[_Candidate]:
    out: list[_Candidate] = []
    for symbol in _symbols(session, SupplySnapshot.symbol):
        rows = _latest_supply_rows(session, symbol)
        dominant = _dominant(rows)
        if dominant is None:
            continue

        # Non-positive supply — even the dominant collision row is <= 0.
        if dominant.circulating_supply is None or dominant.circulating_supply <= 0:
            out.append(_Candidate(
                symbol=symbol, provider="DefiLlama", metric_name="circulating_supply",
                warning_type=NON_POSITIVE_SUPPLY, severity="high",
                message=(
                    f"{symbol} latest circulating supply is "
                    f"{dominant.circulating_supply}, which is not positive."
                ),
            ))

        # Duplicate snapshots at the same instant — a ticker collision.
        if len(rows) > 1:
            out.append(_Candidate(
                symbol=symbol, provider="DefiLlama", metric_name="supply_snapshots",
                warning_type=DUPLICATE_SNAPSHOT, severity="medium",
                message=(
                    f"{symbol} has {len(rows)} supply snapshots at the same timestamp "
                    f"({dominant.recorded_at.isoformat()}); multiple assets likely share "
                    f"this ticker, so period-over-period supply moves may be misleading."
                ),
            ))

        # Missing chain distribution — degrades chain-concentration analysis.
        if not _has_chain_distribution(dominant.supply_by_chain):
            out.append(_Candidate(
                symbol=symbol, provider="DefiLlama", metric_name="supply_by_chain",
                warning_type=MISSING_CHAIN_DISTRIBUTION, severity="low",
                message=(
                    f"{symbol} latest supply snapshot has no chain breakdown; "
                    f"chain-concentration metrics are unavailable for this asset."
                ),
            ))

        # Implausible supply jump between the two most recent snapshots.
        pts = _supply_points(session, symbol)
        if len(pts) >= 2:
            (_, cur), (_, prev) = pts[0], pts[1]
            if prev > 0:
                pct = (cur - prev) / prev * 100.0
                if abs(pct) >= SUPPLY_JUMP_PCT:
                    out.append(_Candidate(
                        symbol=symbol, provider="DefiLlama",
                        metric_name="circulating_supply",
                        warning_type=SUPPLY_JUMP,
                        severity="high" if abs(pct) >= SUPPLY_JUMP_HIGH_PCT else "medium",
                        message=(
                            f"{symbol} circulating supply moved {pct:+.0f}% between the "
                            f"two latest snapshots (${prev:,.0f} → ${cur:,.0f}) — an "
                            f"implausibly large change that may indicate bad data."
                        ),
                    ))
    return out


# ── lifecycle ─────────────────────────────────────────────────────────────────

def run_validation(now: datetime | None = None) -> dict[str, list[dict]]:
    """Detect data-quality problems, opening new warnings and resolving cleared ones.

    Idempotent: opens a row only for a problem with no currently-open warning of
    the same (symbol, metric_name, warning_type), and resolves an open warning
    only when its problem is no longer detected. Returns the rows actually
    ``opened`` and ``resolved`` on this run (as dicts). Safe to call repeatedly.
    """
    now = now or datetime.utcnow()
    opened: list[dict] = []
    resolved: list[dict] = []

    with get_session() as session:
        candidates = _detect_price_issues(session) + _detect_supply_issues(session)

        open_rows = session.execute(
            select(DataQualityWarning).where(DataQualityWarning.resolved_at.is_(None))
        ).scalars().all()
        open_by_key = {
            (r.symbol, r.metric_name, r.warning_type): r for r in open_rows
        }

        detected_keys: set[tuple[str | None, str, str]] = set()
        for c in candidates:
            if c.key in detected_keys:
                continue  # collapse duplicate candidates within a single run
            detected_keys.add(c.key)
            if c.key in open_by_key:
                continue  # already an open warning — nothing to do
            row = DataQualityWarning(
                symbol=c.symbol, provider=c.provider, metric_name=c.metric_name,
                warning_type=c.warning_type, severity=c.severity, message=c.message,
                detected_at=now, resolved_at=None,
            )
            session.add(row)
            session.flush()  # populate id before snapshotting to a dict
            opened.append(row.to_dict())

        for key, row in open_by_key.items():
            if key not in detected_keys:
                row.resolved_at = now
                resolved.append(row.to_dict())

        session.commit()

    return {"opened": opened, "resolved": resolved}


# ── queries ───────────────────────────────────────────────────────────────────

def query_warnings(
    symbol: str | None = None,
    severity: str | None = None,
    warning_type: str | None = None,
    active_only: bool = True,
    limit: int = 200,
) -> list[dict]:
    """Return data-quality warnings, newest-first, optionally filtered.

    By default only *active* (unresolved) warnings are returned; pass
    ``active_only=False`` to include resolved history. Filters are applied in
    the database. Returns an empty list when nothing matches.
    """
    with get_session() as session:
        q = select(DataQualityWarning).order_by(
            DataQualityWarning.detected_at.desc(), DataQualityWarning.id.desc()
        )
        if active_only:
            q = q.where(DataQualityWarning.resolved_at.is_(None))
        if symbol:
            q = q.where(DataQualityWarning.symbol == symbol.upper())
        if severity:
            q = q.where(DataQualityWarning.severity == severity.lower())
        if warning_type:
            q = q.where(DataQualityWarning.warning_type == warning_type.upper())
        rows = session.execute(q.limit(limit)).scalars().all()
        return [r.to_dict() for r in rows]


def warning_summary(now: datetime | None = None) -> dict[str, Any]:
    """At-a-glance counts of currently-active warnings.

    Reports the total active count plus breakdowns by severity and warning type
    — enough for a headline ("3 active data-quality warnings") and the panel
    badges. Always returns a structured object, even on an empty database.
    """
    by_severity = {s: 0 for s in SEVERITIES}
    by_type = {t: 0 for t in WARNING_TYPES}

    with get_session() as session:
        rows = session.execute(
            select(
                DataQualityWarning.severity,
                DataQualityWarning.warning_type,
                func.count().label("cnt"),
            )
            .where(DataQualityWarning.resolved_at.is_(None))
            .group_by(DataQualityWarning.severity, DataQualityWarning.warning_type)
        ).all()

    total = 0
    for severity, warning_type, cnt in rows:
        total += cnt
        if severity in by_severity:
            by_severity[severity] += cnt
        if warning_type in by_type:
            by_type[warning_type] += cnt

    return {
        "active_total": total,
        "by_severity": by_severity,
        "by_type": by_type,
    }
