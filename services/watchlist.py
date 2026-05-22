"""Watchlist service — the operator's pinned set of stablecoins to monitor.

The watchlist is a single global list for the deployment (there is no per-user
auth). It is purely a focus aid: pinning an asset does not change ingestion,
scoring, or any data — it only changes what the dashboard highlights. All write
helpers normalise the symbol to upper-case and only accept assets that exist in
``stablecoins`` (unknown symbols are rejected, never invented), so the watchlist
can never reference an asset the dashboard cannot render.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy import func, select

from db.models import (
    PriceSnapshot,
    RiskScore,
    Stablecoin,
    SupplySnapshot,
    WatchlistItem,
    get_session,
)


def _normalize_symbol(symbol: str | None) -> str:
    return (symbol or "").strip().upper()


def watchlist_symbols() -> set[str]:
    """Return the set of currently-watched symbols (cheap membership lookup)."""
    with get_session() as session:
        rows = session.execute(select(WatchlistItem.symbol)).scalars().all()
    return set(rows)


def add_to_watchlist(symbol: str, note: str | None = None) -> dict | None:
    """Add ``symbol`` to the watchlist (idempotent).

    Returns the stored item as a dict, or ``None`` when ``symbol`` is not a
    tracked stablecoin. Adding an already-watched symbol is a no-op except that a
    non-null ``note`` updates the existing note.
    """
    sym = _normalize_symbol(symbol)
    if not sym:
        return None
    with get_session() as session:
        known = session.execute(
            select(Stablecoin.symbol).where(Stablecoin.symbol == sym)
        ).scalar_one_or_none()
        if known is None:
            return None

        item = session.execute(
            select(WatchlistItem).where(WatchlistItem.symbol == sym)
        ).scalar_one_or_none()
        if item is None:
            item = WatchlistItem(symbol=sym, note=note, added_at=datetime.utcnow())
            session.add(item)
            session.commit()
            session.refresh(item)
        elif note is not None:
            item.note = note
            session.commit()
            session.refresh(item)
        return {
            "symbol": item.symbol,
            "note": item.note,
            "added_at": item.added_at.isoformat(),
        }


def remove_from_watchlist(symbol: str) -> bool:
    """Remove ``symbol`` from the watchlist. Returns True if a row was removed."""
    sym = _normalize_symbol(symbol)
    if not sym:
        return False
    with get_session() as session:
        item = session.execute(
            select(WatchlistItem).where(WatchlistItem.symbol == sym)
        ).scalar_one_or_none()
        if item is None:
            return False
        session.delete(item)
        session.commit()
        return True


def set_watchlist(symbols: Iterable[str]) -> dict:
    """Sync the watchlist to exactly ``symbols`` (add missing, remove extra).

    Used by the dashboard's multiselect editor. Unknown symbols are skipped (not
    added) and reported in ``skipped`` rather than raising. Returns the symbols
    that were ``added``, ``removed``, and ``skipped``.
    """
    desired = {_normalize_symbol(s) for s in symbols if _normalize_symbol(s)}
    current = watchlist_symbols()

    added: list[str] = []
    skipped: list[str] = []
    for sym in desired - current:
        if add_to_watchlist(sym) is not None:
            added.append(sym)
        else:
            skipped.append(sym)

    removed: list[str] = []
    for sym in current - desired:
        if remove_from_watchlist(sym):
            removed.append(sym)

    return {
        "added": sorted(added),
        "removed": sorted(removed),
        "skipped": sorted(skipped),
    }


def get_watchlist() -> list[dict]:
    """Return watched assets (newest first) enriched with key monitoring metrics.

    Each entry carries the asset name plus its latest price, peg deviation,
    circulating supply, and overall risk score — or ``None`` for any metric with
    no stored data, so missing values are shown explicitly rather than guessed.
    """
    with get_session() as session:
        items = session.execute(
            select(WatchlistItem).order_by(
                WatchlistItem.added_at.desc(), WatchlistItem.symbol
            )
        ).scalars().all()
        if not items:
            return []

        symbols = [it.symbol for it in items]

        names = {
            r.symbol: r.name
            for r in session.execute(
                select(Stablecoin).where(Stablecoin.symbol.in_(symbols))
            ).scalars().all()
        }

        price_sq = (
            select(PriceSnapshot.symbol, func.max(PriceSnapshot.recorded_at).label("ts"))
            .where(PriceSnapshot.symbol.in_(symbols))
            .group_by(PriceSnapshot.symbol)
            .subquery()
        )
        prices = {
            row.symbol: row
            for row in session.execute(
                select(PriceSnapshot).join(
                    price_sq,
                    (PriceSnapshot.symbol == price_sq.c.symbol)
                    & (PriceSnapshot.recorded_at == price_sq.c.ts),
                )
            ).scalars().all()
        }

        score_sq = (
            select(RiskScore.symbol, func.max(RiskScore.scored_at).label("ts"))
            .where(RiskScore.symbol.in_(symbols))
            .group_by(RiskScore.symbol)
            .subquery()
        )
        scores = {
            row.symbol: row
            for row in session.execute(
                select(RiskScore).join(
                    score_sq,
                    (RiskScore.symbol == score_sq.c.symbol)
                    & (RiskScore.scored_at == score_sq.c.ts),
                )
            ).scalars().all()
        }

        supply_sq = (
            select(SupplySnapshot.symbol, func.max(SupplySnapshot.recorded_at).label("ts"))
            .where(SupplySnapshot.symbol.in_(symbols))
            .group_by(SupplySnapshot.symbol)
            .subquery()
        )
        supplies = {
            row.symbol: row.circulating_supply
            for row in session.execute(
                select(SupplySnapshot).join(
                    supply_sq,
                    (SupplySnapshot.symbol == supply_sq.c.symbol)
                    & (SupplySnapshot.recorded_at == supply_sq.c.ts),
                )
            ).scalars().all()
        }

        result = []
        for it in items:
            p = prices.get(it.symbol)
            s = scores.get(it.symbol)
            result.append({
                "symbol": it.symbol,
                "name": names.get(it.symbol),
                "note": it.note,
                "added_at": it.added_at.isoformat(),
                "price": p.price if p else None,
                "peg_deviation_bps": p.peg_deviation_bps if p else None,
                "circulating_supply": supplies.get(it.symbol),
                "overall_score": s.overall_score if s else None,
            })
        return result
