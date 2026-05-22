"""Service: stablecoin market dominance and market-share momentum.

Stablecoins compete for the same role — the dollar of crypto — so *market share*
(an asset's supply as a fraction of all tracked stablecoin supply) and how that
share is moving says more about competitive momentum than raw supply alone. This
service turns the existing ``supply_snapshots`` history into:

- total tracked stablecoin supply and per-asset market share (the "dominance" of
  each coin, like BTC dominance for the stablecoin market),
- 7-day and 30-day market-share change in percentage points,
- a ranked table plus a gainers/losers view of competitive momentum.

Read-only over ``supply_snapshots`` — no new table or pipeline, consistent with
the read-time approach used for liquidity (#7) and chain concentration (#8).
Shared by the FastAPI ``/stablecoins/rankings`` endpoint and the Streamlit
dashboard so the two never drift apart.

Two data-quality guards mirror the rest of the codebase:

- *Ticker collisions.* Several DefiLlama assets share a ticker and land at the
  same timestamp, so same-(symbol, timestamp) rows are collapsed to the dominant
  (largest) value — identical to ``services.market_changes`` / ``services.profile``.
- *Insufficient history.* A window's "share N days ago" is only computed from a
  snapshot at least half the window old, so a 30-day move is never claimed from a
  few days of data; otherwise the field is ``None`` (insufficient history) rather
  than a misleading number.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from db.models import Stablecoin, SupplySnapshot, get_session

# How far back to load supply history. The widest window is 30 days; a little
# margin lets the "nearest snapshot to T-30d" search land on a real point.
LOOKBACK_DAYS = 35

WINDOW_7D = timedelta(days=7)
WINDOW_30D = timedelta(days=30)


@dataclass
class _Point:
    ts: datetime
    value: float


def _load_series(session, t_now: datetime) -> dict[str, list[_Point]]:
    """Return ``{symbol: [_Point, ...]}`` ascending by time, collisions collapsed.

    Loads the last ``LOOKBACK_DAYS`` of supply relative to the newest snapshot
    (``t_now``), drops non-positive supply, and collapses same-timestamp ticker
    collisions to the dominant (largest) value so a symbol tracks one asset
    across snapshots instead of an arbitrary collision row.
    """
    cutoff = t_now - timedelta(days=LOOKBACK_DAYS)
    rows = (
        session.execute(
            select(SupplySnapshot)
            .where(SupplySnapshot.recorded_at >= cutoff)
            .order_by(SupplySnapshot.recorded_at.asc())
        )
        .scalars()
        .all()
    )
    grouped: dict[str, dict[datetime, float]] = {}
    for r in rows:
        value = r.circulating_supply
        if value is None or value <= 0:
            continue
        per_ts = grouped.setdefault(r.symbol, {})
        if r.recorded_at not in per_ts or value > per_ts[r.recorded_at]:
            per_ts[r.recorded_at] = value
    return {
        symbol: [_Point(ts, val) for ts, val in sorted(per_ts.items())]
        for symbol, per_ts in grouped.items()
    }


def _value_window_ago(points: list[_Point], t_now: datetime, window: timedelta) -> float | None:
    """Supply at roughly ``t_now - window``, or ``None`` without enough history.

    Considers only points at least *half* the window old, then picks the one
    closest to the target time. This refuses to claim a 30-day-ago value from a
    snapshot only a few days old.
    """
    target = t_now - window
    min_age_cutoff = t_now - window / 2
    candidates = [p for p in points if p.ts <= min_age_cutoff]
    if not candidates:
        return None
    return min(candidates, key=lambda p: abs((p.ts - target).total_seconds())).value


def _share(value: float | None, total: float) -> float | None:
    if value is None or total <= 0:
        return None
    return value / total * 100.0


def compute_dominance(limit: int | None = None) -> dict[str, Any]:
    """Market dominance and share-momentum across all tracked stablecoins.

    Returns a structured object: total tracked supply, asset count, the dominant
    asset and its share, and a ``rankings`` list (market share descending) where
    each asset carries its current share plus 7-day and 30-day share-ago and
    share-change (percentage points). On a brand-new database returns the empty
    shape (``total_tracked_supply`` 0.0, ``rankings`` []).
    """
    now = datetime.utcnow()

    with get_session() as session:
        t_now = session.execute(
            select(func.max(SupplySnapshot.recorded_at))
        ).scalar_one_or_none()
        if t_now is None:
            return {
                "generated_at": now.isoformat(),
                "recorded_at": None,
                "total_tracked_supply": 0.0,
                "asset_count": 0,
                "top_asset": None,
                "top_asset_share": None,
                "rankings": [],
            }
        series = _load_series(session, t_now)
        names = dict(
            session.execute(select(Stablecoin.symbol, Stablecoin.name)).all()
        )

    # Current supply per asset = its newest in-window point.
    latest: dict[str, _Point] = {
        sym: pts[-1] for sym, pts in series.items() if pts
    }
    total_now = sum(p.value for p in latest.values())

    if total_now <= 0:
        return {
            "generated_at": now.isoformat(),
            "recorded_at": t_now.isoformat(),
            "total_tracked_supply": 0.0,
            "asset_count": 0,
            "top_asset": None,
            "top_asset_share": None,
            "rankings": [],
        }

    # Past supply per asset for each window; the past total is summed only over
    # assets that actually have sufficiently-old history, so each past share is
    # measured against the market as it was tracked then.
    past7 = {sym: _value_window_ago(pts, t_now, WINDOW_7D) for sym, pts in series.items()}
    past30 = {sym: _value_window_ago(pts, t_now, WINDOW_30D) for sym, pts in series.items()}
    total_past7 = sum(v for v in past7.values() if v is not None)
    total_past30 = sum(v for v in past30.values() if v is not None)

    rankings: list[dict[str, Any]] = []
    for sym, pt in latest.items():
        share_now = pt.value / total_now * 100.0
        share7 = _share(past7.get(sym), total_past7)
        share30 = _share(past30.get(sym), total_past30)
        rankings.append({
            "asset": sym,
            "name": names.get(sym),
            "circulating_supply": round(pt.value, 2),
            "market_share": round(share_now, 4),
            "recorded_at": pt.ts.isoformat(),
            "market_share_7d_ago": round(share7, 4) if share7 is not None else None,
            "market_share_change_7d": round(share_now - share7, 4) if share7 is not None else None,
            "market_share_30d_ago": round(share30, 4) if share30 is not None else None,
            "market_share_change_30d": round(share_now - share30, 4) if share30 is not None else None,
        })

    rankings.sort(key=lambda r: r["market_share"], reverse=True)
    top = rankings[0]
    result = {
        "generated_at": now.isoformat(),
        "recorded_at": t_now.isoformat(),
        "total_tracked_supply": round(total_now, 2),
        "asset_count": len(rankings),
        "top_asset": top["asset"],
        "top_asset_share": top["market_share"],
        "rankings": rankings[:limit] if limit is not None else rankings,
    }
    return result


def market_share_movers(window: str = "7d", limit: int = 10) -> dict[str, Any]:
    """Gainers and losers of market share over ``window`` ("7d" or "30d").

    Derived from :func:`compute_dominance` so the two never disagree. ``gainers``
    are assets whose share rose (largest first); ``losers`` are assets whose
    share fell (largest drop first). Assets without enough history for the
    window, or whose change rounds to zero, are excluded. Returns empty lists
    when nothing qualifies.
    """
    if window not in ("7d", "30d"):
        raise ValueError("window must be '7d' or '30d'")
    change_key = f"market_share_change_{window}"
    ago_key = f"market_share_{window}_ago"

    dominance = compute_dominance()
    moved = [
        {
            "asset": r["asset"],
            "name": r["name"],
            "market_share": r["market_share"],
            "market_share_ago": r[ago_key],
            "market_share_change": r[change_key],
            "circulating_supply": r["circulating_supply"],
        }
        for r in dominance["rankings"]
        if r[change_key] is not None and round(r[change_key], 4) != 0.0
    ]
    gainers = sorted(
        (m for m in moved if m["market_share_change"] > 0),
        key=lambda m: m["market_share_change"],
        reverse=True,
    )[:limit]
    losers = sorted(
        (m for m in moved if m["market_share_change"] < 0),
        key=lambda m: m["market_share_change"],
    )[:limit]
    return {"window": window, "gainers": gainers, "losers": losers}
