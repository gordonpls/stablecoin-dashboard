"""Service: order-book liquidity trend analysis per stablecoin.

Order-book depth is stored in ``price_snapshots`` (``bid_depth_usd`` +
``ask_depth_usd``) by both the price and liquidity pipelines. Static depth is
only half the story — a thinning book is a stronger stress signal than a thin
but stable one. This service turns the raw depth history into trend signals:

- 24h and 7d total-depth change (absolute + percent),
- the bid/ask depth imbalance and how it moved (a thin-side proxy for spread
  pressure — we store depth, not best-bid/ask, so true spread is unavailable),
- a chartable depth history series,
- a cross-asset "largest liquidity drops" ranking.

Read-only over ``price_snapshots`` — shared by the FastAPI
``/stablecoins/{symbol}/liquidity`` and ``/stablecoins/liquidity-drops``
endpoints and the Streamlit dashboard so the two never drift apart.

A change object has the shape::

    {
        "previous_value", "current_value", "absolute_change",
        "percent_change", "severity", "comparison_window",
        "previous_at", "timestamp", "summary"
    }

Missing or insufficient history is returned as ``None`` (per change / per
section) rather than guessed, keeping "no data yet" distinct from "zero depth".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from db.models import PriceSnapshot, Stablecoin, get_session

# Higher rank = more urgent. Mirrors services.market_changes for consistency.
SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3}

WINDOW_24H = timedelta(hours=24)
WINDOW_7D = timedelta(days=7)
# How far back to pull depth history: enough headroom for the 7d comparison plus
# a chartable trend. Older data is irrelevant to a 24h/7d trend signal.
HISTORY_WINDOW = timedelta(days=14)

# Depth-change severity bands (percent). Identical to the liquidity thresholds
# in services.market_changes so the two surfaces never disagree on severity.
_DEPTH_LOW, _DEPTH_MEDIUM, _DEPTH_HIGH = 8.0, 15.0, 25.0

_WINDOW_LABELS = {"24h": WINDOW_24H, "7d": WINDOW_7D}
_WINDOW_PROSE = {"24h": "24 hours", "7d": "7 days"}


@dataclass
class _Point:
    ts: datetime
    bid: float | None
    ask: float | None
    total: float
    imbalance_pct: float | None


def _total_depth(bid: float | None, ask: float | None) -> float | None:
    """Combined bid + ask depth, or ``None`` when neither side is reported."""
    if bid is None and ask is None:
        return None
    return (bid or 0.0) + (ask or 0.0)


def _imbalance_pct(bid: float | None, ask: float | None) -> float | None:
    """Signed depth skew in percent: +100 = all bids, -100 = all asks.

    A proxy for one-sided book pressure when true bid/ask spread is unavailable.
    ``None`` when total depth is zero or no side is reported.
    """
    total = _total_depth(bid, ask)
    if not total:
        return None
    return round(((bid or 0.0) - (ask or 0.0)) / total * 100.0, 2)


def _severity_pct(pct: float) -> str:
    a = abs(pct)
    if a >= _DEPTH_HIGH:
        return "high"
    if a >= _DEPTH_MEDIUM:
        return "medium"
    if a >= _DEPTH_LOW:
        return "low"
    return "info"


def _points_for(rows: list[PriceSnapshot]) -> list[_Point]:
    """Build a clean ascending depth series from raw price snapshots.

    Drops rows with no usable depth, and collapses exact-timestamp duplicates
    (the price and liquidity pipelines can both write at the same instant) to
    the larger total so a single moment maps to one point.
    """
    by_ts: dict[datetime, _Point] = {}
    for r in rows:
        total = _total_depth(r.bid_depth_usd, r.ask_depth_usd)
        if total is None or total <= 0:
            continue
        existing = by_ts.get(r.recorded_at)
        if existing is None or total > existing.total:
            by_ts[r.recorded_at] = _Point(
                ts=r.recorded_at,
                bid=r.bid_depth_usd,
                ask=r.ask_depth_usd,
                total=total,
                imbalance_pct=_imbalance_pct(r.bid_depth_usd, r.ask_depth_usd),
            )
    return [by_ts[ts] for ts in sorted(by_ts)]


def _pick_previous(points: list[_Point], current: _Point, window: timedelta) -> _Point | None:
    """Pick the point closest to ``current.ts - window`` for a window comparison.

    Requires at least half the window of separation so a 24h comparison is
    never satisfied by, say, a one-hour-old point — otherwise returns ``None``
    ("not enough history yet"). Among eligible points the one nearest the target
    age is chosen, matching the closest-prior logic in services.market_changes.
    """
    target = current.ts - window
    eligible = [
        p for p in points
        if p.ts < current.ts and (current.ts - p.ts) >= window / 2
    ]
    if not eligible:
        return None
    return min(eligible, key=lambda p: abs((p.ts - target).total_seconds()))


def _depth_change(points: list[_Point], current: _Point, label: str, *, symbol: str | None = None) -> dict | None:
    """Compute a total-depth change object over ``label`` (24h/7d), or ``None``."""
    prev = _pick_previous(points, current, _WINDOW_LABELS[label])
    if prev is None or prev.total <= 0:
        return None
    abs_change = current.total - prev.total
    pct = abs_change / prev.total * 100.0
    severity = _severity_pct(pct)
    direction = "rose" if abs_change >= 0 else "fell"
    prefix = f"{symbol} " if symbol else ""
    summary = (
        f"{prefix}liquidity depth {direction} {abs(pct):.0f}% "
        f"over {_WINDOW_PROSE[label]}."
    )
    return {
        "previous_value": round(prev.total, 2),
        "current_value": round(current.total, 2),
        "absolute_change": round(abs_change, 2),
        "percent_change": round(pct, 2),
        "severity": severity,
        "comparison_window": label,
        "previous_at": prev.ts.isoformat(),
        "timestamp": current.ts.isoformat(),
        "summary": summary,
    }


def _trend(change_24h: dict | None, change_7d: dict | None) -> str | None:
    """Plain-language direction from the 24h move (falling back to 7d).

    improving / deteriorating / stable, using a 2% dead-band so noise around a
    flat book reads as "stable". ``None`` when neither window is comparable.
    """
    basis = change_24h or change_7d
    if basis is None:
        return None
    pct = basis["percent_change"]
    if pct >= 2.0:
        return "improving"
    if pct <= -2.0:
        return "deteriorating"
    return "stable"


def get_liquidity_detail(symbol: str) -> dict[str, Any] | None:
    """Assemble liquidity trend detail for one asset (case-insensitive).

    Returns ``None`` only when the symbol is completely unknown (no registry row
    and no depth history), so the endpoint can 404 cleanly. Otherwise ``current``,
    ``change_24h``, ``change_7d``, and ``imbalance_change_24h`` are each ``None``
    when their data is missing, and ``history`` is the (possibly empty) depth
    series for charting.
    """
    sym = symbol.upper()
    now = datetime.utcnow()
    cutoff = now - HISTORY_WINDOW

    with get_session() as session:
        meta = session.execute(
            select(Stablecoin).where(Stablecoin.symbol == sym)
        ).scalar_one_or_none()
        rows = session.execute(
            select(PriceSnapshot)
            .where(PriceSnapshot.symbol == sym, PriceSnapshot.recorded_at >= cutoff)
            .order_by(PriceSnapshot.recorded_at.asc())
        ).scalars().all()

    points = _points_for(rows)

    if meta is None and not points:
        return None

    current: dict[str, Any] | None = None
    change_24h = change_7d = imbalance_change_24h = None
    trend = None

    if points:
        curr = points[-1]
        current = {
            "bid_depth_usd": curr.bid,
            "ask_depth_usd": curr.ask,
            "total_depth_usd": round(curr.total, 2),
            "imbalance_pct": curr.imbalance_pct,
            "recorded_at": curr.ts.isoformat(),
        }
        change_24h = _depth_change(points, curr, "24h")
        change_7d = _depth_change(points, curr, "7d")
        trend = _trend(change_24h, change_7d)

        # Imbalance move over 24h: how much more one-sided the book has become.
        prev_24h = _pick_previous(points, curr, WINDOW_24H)
        if (
            prev_24h is not None
            and prev_24h.imbalance_pct is not None
            and curr.imbalance_pct is not None
        ):
            imbalance_change_24h = {
                "previous_value": prev_24h.imbalance_pct,
                "current_value": curr.imbalance_pct,
                "absolute_change": round(curr.imbalance_pct - prev_24h.imbalance_pct, 2),
                "comparison_window": "24h",
                "previous_at": prev_24h.ts.isoformat(),
                "timestamp": curr.ts.isoformat(),
            }

    history = [
        {
            "recorded_at": p.ts.isoformat(),
            "bid_depth_usd": p.bid,
            "ask_depth_usd": p.ask,
            "total_depth_usd": round(p.total, 2),
            "imbalance_pct": p.imbalance_pct,
        }
        for p in points
    ]

    return {
        "symbol": sym,
        "name": meta.name if meta else None,
        "registered": meta is not None,
        "current": current,
        "change_24h": change_24h,
        "change_7d": change_7d,
        "imbalance_change_24h": imbalance_change_24h,
        "trend": trend,
        "history": history,
        "generated_at": now.isoformat(),
    }


def largest_liquidity_drops(window: str = "24h", limit: int = 10) -> list[dict]:
    """Cross-asset ranking of the sharpest order-book depth *drops*.

    For each tracked asset, computes the total-depth change over ``window``
    ("24h" or "7d") and keeps only declines, ordered by severity then magnitude
    of the drop. Returns an empty list when no asset has enough history to
    compare. Each row reuses the change-object shape plus ``asset``.
    """
    if window not in _WINDOW_LABELS:
        raise ValueError(f"window must be one of {sorted(_WINDOW_LABELS)}, got {window!r}")

    cutoff = datetime.utcnow() - HISTORY_WINDOW
    with get_session() as session:
        rows = session.execute(
            select(PriceSnapshot)
            .where(PriceSnapshot.recorded_at >= cutoff)
            .order_by(PriceSnapshot.recorded_at.asc())
        ).scalars().all()

    by_symbol: dict[str, list[PriceSnapshot]] = {}
    for r in rows:
        by_symbol.setdefault(r.symbol, []).append(r)

    drops: list[dict] = []
    for sym, sym_rows in by_symbol.items():
        points = _points_for(sym_rows)
        if not points:
            continue
        change = _depth_change(points, points[-1], window, symbol=sym)
        if change is None or change["absolute_change"] >= 0:
            continue  # only declines
        change["asset"] = sym
        drops.append(change)

    drops.sort(
        key=lambda c: (SEVERITY_RANK[c["severity"]], abs(c["percent_change"])),
        reverse=True,
    )
    return drops[:limit]
