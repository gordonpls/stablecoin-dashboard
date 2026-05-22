"""Service: provider fallback visibility for price ingestion.

Price ingestion tries Binance (primary) first and falls back to Coinbase when
the primary is unavailable (``ingestion/exchanges.py``). This service makes that
behaviour observable so a user can tell *when alternate or degraded data is in
use*, which provider served each asset, and whether the primary is repeatedly
failing.

Two data sources, used for what each is good at:

- ``price_snapshots.source`` — written by the price pipeline with the provider
  that actually served each price. This gives the healthy primary-vs-fallback
  *rate* and each asset's current source, derived on read (no extra table).
- ``provider_fallback_events`` — one row per *exceptional* outcome (a fallback
  served the price, or no price was available), carrying the reason the primary
  was skipped, which a price snapshot cannot record. ``record_fallback_events``
  writes these from the pipeline.

Read-only consumers (the FastAPI ``/provider-fallback`` endpoint and the
Streamlit "Provider Fallback" panel) share :func:`get_fallback_status` so the
two never drift apart.

``primary_status`` summarises primary-provider health over the window:

* ``healthy``  — every price point came from the primary, no unavailability
* ``degraded`` — some fallback usage, but below the failing threshold
* ``failing``  — fallback rate at/above the threshold, or any price was
  unavailable in the window
* ``unknown``  — no price points recorded in the window
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from db.models import PriceSnapshot, ProviderFallbackEvent, get_session
from ingestion.exchanges import FALLBACK_PROVIDER, PRIMARY_PROVIDER

# Which ``price_snapshots.source`` values are price-pipeline exchange providers.
# The liquidity pipeline writes source="exchanges_depth"; those rows are depth
# observations, not a price-provider choice, so they are excluded here.
PRICE_PROVIDERS = (PRIMARY_PROVIDER, FALLBACK_PROVIDER)

DEFAULT_WINDOW_HOURS = 24
RECENT_EVENTS_LIMIT = 50

# A fallback rate at/above this (percent of price points in the window) marks the
# primary as "failing". Any unavailable price in the window also marks failing.
FAILING_FALLBACK_RATE = 50.0


def record_fallback_events(
    price_data: dict[str, dict],
    *,
    recorded_at: datetime,
    data_type: str = "price",
) -> int:
    """Persist a fallback event for each symbol that fell back or was unavailable.

    ``price_data`` is the mapping returned by
    :func:`ingestion.exchanges.get_peg_prices`. A row is written only when a
    symbol's ``source_type`` is "fallback" or "unavailable" — the normal primary
    path is not logged. De-duplicates on (symbol, data_type, source_type,
    recorded_at) so recording the same run twice is a no-op. Best-effort: a
    logging failure never propagates to the caller. Returns the number of rows
    inserted.
    """
    rows: list[ProviderFallbackEvent] = []
    for symbol, data in price_data.items():
        source_type = data.get("source_type")
        if source_type not in ("fallback", "unavailable"):
            continue
        rows.append(
            ProviderFallbackEvent(
                symbol=symbol,
                data_type=data_type,
                primary_provider=PRIMARY_PROVIDER,
                fallback_provider=FALLBACK_PROVIDER,
                source_provider=data.get("price_source"),
                source_type=source_type,
                fallback_reason=data.get("fallback_reason"),
                recorded_at=recorded_at,
            )
        )

    if not rows:
        return 0

    inserted = 0
    try:
        with get_session() as session:
            for row in rows:
                exists = session.execute(
                    select(ProviderFallbackEvent.id).where(
                        ProviderFallbackEvent.symbol == row.symbol,
                        ProviderFallbackEvent.data_type == row.data_type,
                        ProviderFallbackEvent.source_type == row.source_type,
                        ProviderFallbackEvent.recorded_at == row.recorded_at,
                    )
                ).first()
                if exists is not None:
                    continue
                session.add(row)
                inserted += 1
            session.commit()
    except Exception:  # pragma: no cover - logging must never break ingestion
        import logging

        logging.getLogger(__name__).warning("record_fallback_events_failed", exc_info=True)
        return 0
    return inserted


def query_fallback_events(
    symbol: str | None = None,
    source_type: str | None = None,
    limit: int = RECENT_EVENTS_LIMIT,
) -> list[dict[str, Any]]:
    """Recent fallback events, newest first, optionally filtered.

    ``symbol`` is matched case-insensitively; ``source_type`` is "fallback" or
    "unavailable". Returns an empty list when nothing matches.
    """
    stmt = select(ProviderFallbackEvent).order_by(ProviderFallbackEvent.recorded_at.desc())
    if symbol is not None:
        stmt = stmt.where(ProviderFallbackEvent.symbol == symbol.upper())
    if source_type is not None:
        stmt = stmt.where(ProviderFallbackEvent.source_type == source_type)
    stmt = stmt.limit(limit)

    with get_session() as session:
        rows = session.execute(stmt).scalars().all()
    return [_event_dict(r) for r in rows]


def _event_dict(row: ProviderFallbackEvent) -> dict[str, Any]:
    return {
        "symbol": row.symbol,
        "data_type": row.data_type,
        "primary_provider": row.primary_provider,
        "fallback_provider": row.fallback_provider,
        "source_provider": row.source_provider,
        "source_type": row.source_type,
        "fallback_reason": row.fallback_reason,
        "recorded_at": row.recorded_at.isoformat() if row.recorded_at is not None else None,
    }


def get_fallback_status(
    window_hours: int = DEFAULT_WINDOW_HOURS,
    recent_limit: int = RECENT_EVENTS_LIMIT,
) -> dict[str, Any]:
    """Assemble provider-fallback status for the price pipeline.

    Returns a structured object: the primary/fallback providers, a window
    ``summary`` (point counts, fallback rate, event counts, whether any asset is
    currently on fallback, primary-provider health), a per-asset breakdown
    (current source + window counts + last fallback), and a list of recent
    fallback events. Always returns a structured object, even on a brand-new
    database.
    """
    now = datetime.utcnow()
    cutoff = now - timedelta(hours=window_hours)

    with get_session() as session:
        # All price-pipeline snapshots in the window (exclude depth-only rows).
        window_rows = session.execute(
            select(PriceSnapshot.symbol, PriceSnapshot.source, PriceSnapshot.recorded_at)
            .where(
                PriceSnapshot.source.in_(PRICE_PROVIDERS),
                PriceSnapshot.recorded_at >= cutoff,
            )
        ).all()

        # Latest price-pipeline source per asset (all time), for "current source".
        latest_ts_sub = (
            select(
                PriceSnapshot.symbol.label("symbol"),
                func.max(PriceSnapshot.recorded_at).label("ts"),
            )
            .where(PriceSnapshot.source.in_(PRICE_PROVIDERS))
            .group_by(PriceSnapshot.symbol)
            .subquery()
        )
        latest_rows = session.execute(
            select(PriceSnapshot.symbol, PriceSnapshot.source, PriceSnapshot.recorded_at)
            .join(
                latest_ts_sub,
                (PriceSnapshot.symbol == latest_ts_sub.c.symbol)
                & (PriceSnapshot.recorded_at == latest_ts_sub.c.ts),
            )
            .where(PriceSnapshot.source.in_(PRICE_PROVIDERS))
        ).all()

        # Fallback events in the window.
        event_rows = session.execute(
            select(ProviderFallbackEvent)
            .where(ProviderFallbackEvent.recorded_at >= cutoff)
            .order_by(ProviderFallbackEvent.recorded_at.desc())
        ).scalars().all()

        recent_events = [
            _event_dict(r)
            for r in session.execute(
                select(ProviderFallbackEvent)
                .order_by(ProviderFallbackEvent.recorded_at.desc())
                .limit(recent_limit)
            ).scalars().all()
        ]

    # ── per-asset latest source (collapse ties to the newest row deterministically)
    latest_source: dict[str, dict[str, Any]] = {}
    for symbol, source, ts in latest_rows:
        cur = latest_source.get(symbol)
        if cur is None or ts > cur["ts"]:
            latest_source[symbol] = {"ts": ts, "source": source}

    # ── window point counts, overall and per asset ──────────────────────────────
    per_asset_counts: dict[str, dict[str, int]] = {}
    primary_points = fallback_points = 0
    for symbol, source, _ts in window_rows:
        bucket = per_asset_counts.setdefault(symbol, {"primary": 0, "fallback": 0})
        if source == PRIMARY_PROVIDER:
            primary_points += 1
            bucket["primary"] += 1
        else:
            fallback_points += 1
            bucket["fallback"] += 1
    total_points = primary_points + fallback_points
    fallback_rate = round(fallback_points / total_points * 100.0, 2) if total_points else None

    # ── per-asset last fallback (from events, window-independent) ────────────────
    last_fallback_by_asset: dict[str, dict[str, Any]] = {}
    for ev in sorted(
        (e for e in recent_events if e["source_type"] == "fallback"),
        key=lambda e: e["recorded_at"] or "",
    ):
        last_fallback_by_asset[ev["symbol"]] = ev  # later (newer) overwrites

    # ── window event counts ─────────────────────────────────────────────────────
    fallback_events = sum(1 for e in event_rows if e.source_type == "fallback")
    unavailable_events = sum(1 for e in event_rows if e.source_type == "unavailable")

    assets: list[dict[str, Any]] = []
    for symbol in sorted(set(latest_source) | set(per_asset_counts)):
        cur = latest_source.get(symbol)
        cur_source = cur["source"] if cur else None
        on_fallback = cur_source in (FALLBACK_PROVIDER,) if cur_source else False
        counts = per_asset_counts.get(symbol, {"primary": 0, "fallback": 0})
        last_fb = last_fallback_by_asset.get(symbol)
        assets.append({
            "asset": symbol,
            "latest_source": cur_source,
            "latest_source_at": cur["ts"].isoformat() if cur else None,
            "on_fallback": on_fallback,
            "primary_points": counts["primary"],
            "fallback_points": counts["fallback"],
            "last_fallback_at": last_fb["recorded_at"] if last_fb else None,
            "last_fallback_reason": last_fb["fallback_reason"] if last_fb else None,
        })

    assets_on_fallback = [a["asset"] for a in assets if a["on_fallback"]]

    # most recent fallback event overall (within the window)
    last_event = next((e for e in event_rows), None)

    # ── primary-provider health ─────────────────────────────────────────────────
    rate_failing = fallback_rate is not None and fallback_rate >= FAILING_FALLBACK_RATE
    if total_points == 0:
        primary_status = "unknown"
        primary_status_reason = "No price points recorded in the window yet."
    elif unavailable_events > 0 or rate_failing:
        primary_status = "failing"
        if unavailable_events > 0:
            primary_status_reason = (
                f"{unavailable_events} price update(s) had no source available in the "
                f"last {window_hours}h; the primary provider may be down."
            )
        else:
            primary_status_reason = (
                f"{fallback_rate:.0f}% of price points came from the fallback provider "
                f"in the last {window_hours}h."
            )
    elif fallback_points > 0:
        primary_status = "degraded"
        primary_status_reason = (
            f"{fallback_points} of {total_points} price points used the fallback "
            f"provider in the last {window_hours}h."
        )
    else:
        primary_status = "healthy"
        primary_status_reason = (
            f"All {total_points} price points came from {PRIMARY_PROVIDER} in the "
            f"last {window_hours}h."
        )

    return {
        "generated_at": now.isoformat(),
        "window_hours": window_hours,
        "primary_provider": PRIMARY_PROVIDER,
        "fallback_provider": FALLBACK_PROVIDER,
        "summary": {
            "total_price_points": total_points,
            "primary_points": primary_points,
            "fallback_points": fallback_points,
            "fallback_rate": fallback_rate,
            "fallback_events": fallback_events,
            "unavailable_events": unavailable_events,
            "currently_on_fallback": bool(assets_on_fallback),
            "assets_on_fallback": assets_on_fallback,
            "last_fallback_at": last_event.recorded_at.isoformat() if last_event else None,
            "last_fallback_reason": last_event.fallback_reason if last_event else None,
            "primary_status": primary_status,
            "primary_status_reason": primary_status_reason,
        },
        "assets": assets,
        "recent_events": recent_events,
    }
