"""Service: compute market-change summaries across stablecoins.

Compares the latest snapshot of each key metric to a prior snapshot and
produces ranked, plain-language change objects. Read-only over existing
tables — shared by the FastAPI ``/stablecoins/changes`` endpoint and the
Streamlit dashboard so the two never drift apart.

Each change object has the shape::

    {
        "asset", "metric", "previous_value", "current_value",
        "absolute_change", "percent_change", "severity",
        "comparison_window", "timestamp", "summary"
    }
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import InstrumentedAttribute

from db.models import PriceSnapshot, RiskScore, SupplySnapshot, get_session

# Higher rank = more urgent. Used to order heterogeneous metrics consistently.
SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3}

SUPPLY_WINDOW = timedelta(days=7)
PRICE_WINDOW = timedelta(hours=24)
SCORE_WINDOW = timedelta(hours=24)


@dataclass
class _Point:
    ts: datetime
    value: float | None


def _series(
    session,
    model,
    ts_attr: InstrumentedAttribute,
    lookback: timedelta,
    value_fn: Callable,
) -> dict[str, list[_Point]]:
    """Return {symbol: [_Point, ...]} sorted ascending by timestamp.

    Several DefiLlama assets can share a ticker, so a single supply run writes
    multiple rows per symbol at the same timestamp. Collapse same-timestamp
    duplicates to the dominant (largest) value so comparisons track the same
    asset across snapshots instead of an arbitrary collision row.
    """
    cutoff = datetime.utcnow() - lookback
    rows = (
        session.execute(select(model).where(ts_attr >= cutoff).order_by(ts_attr.asc()))
        .scalars()
        .all()
    )
    grouped: dict[str, dict[datetime, float]] = {}
    for r in rows:
        value = value_fn(r)
        if value is None:
            continue
        ts = getattr(r, ts_attr.key)
        per_ts = grouped.setdefault(r.symbol, {})
        if ts not in per_ts or value > per_ts[ts]:
            per_ts[ts] = value
    return {
        symbol: [_Point(ts, value) for ts, value in sorted(per_ts.items())]
        for symbol, per_ts in grouped.items()
    }


def _pick_pair(points: list[_Point], window: timedelta) -> tuple[_Point, _Point] | None:
    """Return (previous, current) or None when there is no usable prior snapshot.

    ``current`` is the newest point with a value; ``previous`` is the point
    closest to ``current.ts - window`` that is strictly older than current.
    Missing prior data is handled by returning None (the caller skips it).
    """
    pts = [p for p in points if p.value is not None]
    if len(pts) < 2:
        return None
    current = pts[-1]
    prior = [p for p in pts[:-1] if p.ts < current.ts]
    if not prior:
        return None
    target = current.ts - window
    previous = min(prior, key=lambda p: abs((p.ts - target).total_seconds()))
    return previous, current


def _severity_pct(pct: float, low: float, medium: float, high: float) -> str:
    a = abs(pct)
    if a >= high:
        return "high"
    if a >= medium:
        return "medium"
    if a >= low:
        return "low"
    return "info"


def _severity_abs(change: float, low: float, medium: float, high: float) -> str:
    a = abs(change)
    if a >= high:
        return "high"
    if a >= medium:
        return "medium"
    if a >= low:
        return "low"
    return "info"


def _make_change(
    symbol: str,
    metric: str,
    prev: _Point,
    curr: _Point,
    window_label: str,
    severity: str,
    summary: str,
) -> dict:
    abs_change = curr.value - prev.value
    pct = (abs_change / prev.value * 100.0) if prev.value else None
    return {
        "asset": symbol,
        "metric": metric,
        "previous_value": round(prev.value, 4),
        "current_value": round(curr.value, 4),
        "absolute_change": round(abs_change, 4),
        "percent_change": round(pct, 2) if pct is not None else None,
        "severity": severity,
        "comparison_window": window_label,
        "timestamp": curr.ts.isoformat(),
        "summary": summary,
    }


def _depth(row: PriceSnapshot) -> float | None:
    total = (row.bid_depth_usd or 0) + (row.ask_depth_usd or 0)
    return total if total > 0 else None


def _sort_key(change: dict) -> tuple[int, float]:
    magnitude = change["percent_change"]
    if magnitude is None:
        magnitude = change["absolute_change"]
    return SEVERITY_RANK[change["severity"]], abs(magnitude or 0)


def compute_market_changes(limit: int | None = None) -> list[dict]:
    """Compute ranked market-change objects across all tracked stablecoins.

    Returns an empty list when there is not enough history to compare. Results
    are ordered by severity, then by the magnitude of the move.
    """
    changes: list[dict] = []

    with get_session() as session:
        supply_series = _series(
            session, SupplySnapshot, SupplySnapshot.recorded_at,
            SUPPLY_WINDOW * 2, lambda r: r.circulating_supply,
        )
        peg_series = _series(
            session, PriceSnapshot, PriceSnapshot.recorded_at,
            PRICE_WINDOW * 2, lambda r: r.peg_deviation_bps,
        )
        liq_series = _series(
            session, PriceSnapshot, PriceSnapshot.recorded_at,
            PRICE_WINDOW * 2, _depth,
        )
        score_series = _series(
            session, RiskScore, RiskScore.scored_at,
            SCORE_WINDOW * 2, lambda r: r.overall_score,
        )

    for symbol, pts in supply_series.items():
        pair = _pick_pair(pts, SUPPLY_WINDOW)
        if pair is None:
            continue
        prev, curr = pair
        if prev.value <= 0:
            continue
        pct = (curr.value - prev.value) / prev.value * 100.0
        if abs(pct) < 0.5:  # ignore sub-half-percent noise
            continue
        severity = _severity_pct(pct, low=2, medium=5, high=10)
        direction = "increased" if pct >= 0 else "decreased"
        summary = f"{symbol} supply {direction} {abs(pct):.1f}% over 7 days."
        changes.append(_make_change(symbol, "supply", prev, curr, "7d", severity, summary))

    for symbol, pts in peg_series.items():
        pair = _pick_pair(pts, PRICE_WINDOW)
        if pair is None:
            continue
        prev, curr = pair
        abs_change = curr.value - prev.value
        if abs(abs_change) < 1.0:  # < 1 bps move = noise
            continue
        severity = _severity_abs(abs_change, low=5, medium=15, high=30)
        if curr.value >= 50:
            severity = "high"
        elif curr.value >= 20 and SEVERITY_RANK[severity] < SEVERITY_RANK["medium"]:
            severity = "medium"
        summary = (
            f"{symbol} peg deviation moved from {prev.value:.0f} bps "
            f"to {curr.value:.0f} bps."
        )
        changes.append(
            _make_change(symbol, "peg_deviation_bps", prev, curr, "24h", severity, summary)
        )

    for symbol, pts in liq_series.items():
        pair = _pick_pair(pts, PRICE_WINDOW)
        if pair is None:
            continue
        prev, curr = pair
        if prev.value <= 0:
            continue
        pct = (curr.value - prev.value) / prev.value * 100.0
        if abs(pct) < 1.0:
            continue
        severity = _severity_pct(pct, low=8, medium=15, high=25)
        direction = "rose" if pct >= 0 else "fell"
        summary = f"{symbol} liquidity depth {direction} {abs(pct):.0f}% over 24 hours."
        changes.append(
            _make_change(symbol, "liquidity_usd", prev, curr, "24h", severity, summary)
        )

    for symbol, pts in score_series.items():
        pair = _pick_pair(pts, SCORE_WINDOW)
        if pair is None:
            continue
        prev, curr = pair
        abs_change = curr.value - prev.value
        if abs(abs_change) < 1.0:
            continue
        severity = _severity_abs(abs_change, low=2, medium=5, high=10)
        direction = "rose" if abs_change >= 0 else "fell"
        summary = (
            f"{symbol} overall risk score {direction} "
            f"{abs(abs_change):.0f} points over 24 hours."
        )
        changes.append(
            _make_change(symbol, "overall_score", prev, curr, "24h", severity, summary)
        )

    changes.sort(key=_sort_key, reverse=True)
    if limit is not None:
        changes = changes[:limit]
    return changes
