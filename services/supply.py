"""Service: per-asset circulating-supply detail and history.

Supply is the headline DefiLlama metric for the whole project, yet it was the
only core per-asset metric without its own endpoint (prices, liquidity,
chain-supply, scores, events, and regime all have one). This service backs
``GET /stablecoins/{symbol}/supply``: the latest supply + chain breakdown, 7-day
and 30-day supply change, and a deduplicated supply time series for charting.

Read-only over ``supply_snapshots`` — no new table or pipeline, consistent with
the read-time approach used for liquidity (#7), chain concentration (#8), and
dominance (#9). Shared by the FastAPI endpoint and (later) the dashboard so the
two never drift apart. The chain breakdown is parsed by the single canonical
``services.profile._parse_chains`` so this view can never disagree with the
profile / chain-concentration pages.

Two data-quality guards mirror the rest of the codebase:

- *Ticker collisions.* Several DefiLlama assets share a ticker and land at the
  same ``recorded_at``, so same-timestamp rows are collapsed to the dominant
  (largest) supply — identical to ``services.market_changes`` /
  ``services.dominance`` / ``services.profile``. A symbol therefore tracks one
  asset across snapshots instead of an arbitrary collision row.
- *Insufficient history.* A window's "supply N days ago" is only computed from a
  snapshot at least *half* the window old, so a 30-day change is never claimed
  from a few days of data; otherwise the change is ``None`` (insufficient
  history) rather than a misleading number — identical to ``services.dominance``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from db.models import Stablecoin, SupplySnapshot, get_session
from services.profile import _parse_chains

WINDOW_7D = timedelta(days=7)
WINDOW_30D = timedelta(days=30)

# 30-day change needs at least 30 days of history; load a little more than the
# requested chart window so the change windows always have data to search even
# when the caller asks for a short chart.
MIN_LOOKBACK_DAYS = 35


@dataclass
class _Point:
    ts: datetime
    value: float


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _load_series(session, sym: str, cutoff: datetime) -> list[_Point]:
    """Return the deduped supply series for ``sym`` since ``cutoff``, ascending.

    Drops non-positive supply and collapses same-timestamp ticker collisions to
    the dominant (largest) value, so the series tracks one asset over time.
    """
    rows = (
        session.execute(
            select(SupplySnapshot)
            .where(
                SupplySnapshot.symbol == sym,
                SupplySnapshot.recorded_at >= cutoff,
            )
            .order_by(SupplySnapshot.recorded_at.asc())
        )
        .scalars()
        .all()
    )
    per_ts: dict[datetime, float] = {}
    for r in rows:
        value = r.circulating_supply
        if value is None or value <= 0:
            continue
        if r.recorded_at not in per_ts or value > per_ts[r.recorded_at]:
            per_ts[r.recorded_at] = value
    return [_Point(ts, val) for ts, val in sorted(per_ts.items())]


def _point_window_ago(points: list[_Point], t_now: datetime, window: timedelta) -> _Point | None:
    """Point at roughly ``t_now - window``, or ``None`` without enough history.

    Considers only points at least *half* the window old, then picks the one
    closest to the target time. Refuses to claim a 30-day-ago value from a
    snapshot only a few days old.
    """
    target = t_now - window
    min_age_cutoff = t_now - window / 2
    candidates = [p for p in points if p.ts <= min_age_cutoff]
    if not candidates:
        return None
    return min(candidates, key=lambda p: abs((p.ts - target).total_seconds()))


def _change(prev: _Point | None, curr: _Point) -> dict[str, Any] | None:
    """Structured change object, or ``None`` when there is no usable prior point."""
    if prev is None or prev.value <= 0:
        return None
    abs_change = curr.value - prev.value
    return {
        "previous_value": round(prev.value, 2),
        "previous_recorded_at": _iso(prev.ts),
        "current_value": round(curr.value, 2),
        "absolute_change": round(abs_change, 2),
        "percent_change": round(abs_change / prev.value * 100.0, 2),
    }


def get_supply_detail(
    symbol: str,
    history_days: int = 90,
    history_limit: int | None = None,
) -> dict[str, Any] | None:
    """Assemble circulating-supply detail and history for ``symbol``.

    Returns ``None`` only when the symbol is completely unknown (no registry row
    *and* no supply snapshots), so callers can return a clean 404. Otherwise:

    - ``current`` is the latest supply + chain breakdown (``None`` if the asset
      has no supply data, or its latest supply is non-positive),
    - ``change_7d`` / ``change_30d`` compare the latest in-window point to one
      roughly a window earlier (``None`` when there is not enough history),
    - ``history`` is the deduped supply time series within ``history_days``,
      ascending, optionally capped to the newest ``history_limit`` points.
    """
    sym = symbol.upper()
    now = datetime.utcnow()
    # Load enough history for the 30-day change even if the caller wants a short
    # chart; the chart history is filtered to ``history_days`` afterwards.
    lookback_days = max(history_days, MIN_LOOKBACK_DAYS)
    cutoff = now - timedelta(days=lookback_days)

    with get_session() as session:
        meta = session.execute(
            select(Stablecoin).where(Stablecoin.symbol == sym)
        ).scalar_one_or_none()

        latest_ts = session.execute(
            select(func.max(SupplySnapshot.recorded_at)).where(
                SupplySnapshot.symbol == sym
            )
        ).scalar_one_or_none()

        if meta is None and latest_ts is None:
            return None

        points = _load_series(session, sym, cutoff)

        # The latest dominant row (may be older than the lookback window if the
        # supply pipeline has stalled) — used for the "current" snapshot so a
        # stale asset still shows its last known supply, flagged by recorded_at.
        latest_value: float | None = None
        latest_chains_json: str | None = None
        if latest_ts is not None:
            latest_row = (
                session.execute(
                    select(SupplySnapshot)
                    .where(
                        SupplySnapshot.symbol == sym,
                        SupplySnapshot.recorded_at == latest_ts,
                    )
                    .order_by(SupplySnapshot.circulating_supply.desc())
                    .limit(1)
                )
                .scalars()
                .first()
            )
            if latest_row is not None:
                latest_value = latest_row.circulating_supply
                latest_chains_json = latest_row.supply_by_chain

    name = meta.name if meta is not None else None

    current: dict[str, Any] | None = None
    if latest_ts is not None and latest_value is not None and latest_value > 0:
        chains = _parse_chains(latest_chains_json)
        top = chains[0] if chains else None
        current = {
            "circulating_supply": round(latest_value, 2),
            "recorded_at": _iso(latest_ts),
            "top_chain": top["chain"] if top else None,
            "top_chain_pct": top["supply_pct"] if top else None,
            "chain_count": len(chains),
            "chains": chains,
        }

    change_7d = change_30d = None
    if points:
        curr = points[-1]
        change_7d = _change(_point_window_ago(points, curr.ts, WINDOW_7D), curr)
        change_30d = _change(_point_window_ago(points, curr.ts, WINDOW_30D), curr)

    history_cutoff = now - timedelta(days=history_days)
    hist_points = [p for p in points if p.ts >= history_cutoff]
    if history_limit is not None and len(hist_points) > history_limit:
        hist_points = hist_points[-history_limit:]
    history = [
        {"recorded_at": _iso(p.ts), "circulating_supply": round(p.value, 2)}
        for p in hist_points
    ]

    return {
        "symbol": sym,
        "name": name,
        "current": current,
        "change_7d": change_7d,
        "change_30d": change_30d,
        "history": history,
        "history_days": history_days,
        "generated_at": now.isoformat(),
    }
