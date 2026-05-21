"""Service: assemble a complete per-asset profile for one stablecoin.

Aggregates the latest price, supply, chain breakdown, liquidity depth, reserve
composition, risk-score breakdown, and per-source data freshness into a single
structured object. Read-only over existing tables — shared by the FastAPI
``/stablecoins/{symbol}/profile`` endpoint and the Streamlit profile view so the
two never drift apart.

``get_stablecoin_profile`` returns ``None`` when an asset has no registry row
*and* no data of any kind, so callers can return a clean 404. Otherwise every
section that has no data is returned as ``None`` rather than guessed, keeping
"missing" distinct from "zero".
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from db.models import (
    PriceSnapshot,
    ReserveReport,
    RiskScore,
    Stablecoin,
    SupplySnapshot,
    get_session,
)

# Pipeline cadences (seconds) — mirror app/dashboard/main.py. Used to classify
# how fresh each source is relative to how often it is expected to refresh.
PRICE_CADENCE_SECS = 600     # prices + scores refresh every 10 minutes
SUPPLY_CADENCE_SECS = 3600   # supply + reserves refresh hourly
RESERVE_STALE_DAYS = 90      # a reserve attestation older than this is "stale"


def risk_label(score: float | None) -> str:
    """Plain-language risk band — identical thresholds to the dashboard."""
    if score is None:
        return "—"
    if score >= 80:
        return "Low Risk"
    if score >= 60:
        return "Moderate"
    if score >= 40:
        return "Elevated"
    return "High Risk"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _freshness(last_updated: datetime | None, cadence_secs: int, *, now: datetime) -> dict[str, Any]:
    """Classify a source's freshness against its expected refresh cadence.

    fresh: within one cadence · delayed: missed one · stale: missed two or more.
    """
    if last_updated is None:
        return {"last_updated": None, "age_seconds": None, "status": "missing"}
    age = max(0.0, (now - last_updated).total_seconds())
    if age <= cadence_secs:
        status = "fresh"
    elif age <= cadence_secs * 2:
        status = "delayed"
    else:
        status = "stale"
    return {"last_updated": _iso(last_updated), "age_seconds": round(age), "status": status}


def _parse_chains(supply_by_chain_json: str | None) -> list[dict[str, Any]]:
    """Normalise DefiLlama's nested chain JSON into sorted rows with shares.

    Returns ``[{chain, supply, supply_pct}, ...]`` sorted by supply descending.
    Missing or malformed data yields an empty list.
    """
    if not supply_by_chain_json:
        return []
    try:
        data = json.loads(supply_by_chain_json)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict) or not data:
        return []

    def _current_usd(entry: Any) -> float:
        if not isinstance(entry, dict):
            return 0.0
        current = entry.get("current") or {}
        if isinstance(current, dict):
            return float(current.get("peggedUSD") or 0.0)
        # Some snapshots store a flat number rather than a {peggedUSD: ...} dict.
        try:
            return float(current)
        except (ValueError, TypeError):
            return 0.0

    rows = [{"chain": chain, "supply": _current_usd(entry)} for chain, entry in data.items()]
    rows = [r for r in rows if r["supply"] > 0]
    total = sum(r["supply"] for r in rows)
    for r in rows:
        r["supply_pct"] = round(r["supply"] / total * 100.0, 2) if total > 0 else None
    rows.sort(key=lambda r: r["supply"], reverse=True)
    return rows


def get_stablecoin_profile(symbol: str) -> dict[str, Any] | None:
    """Assemble the full profile for ``symbol`` (case-insensitive).

    Returns ``None`` only when the symbol is completely unknown (no registry row
    and no snapshots of any kind). Individual sections are ``None`` when their
    source has no data.
    """
    sym = symbol.upper()
    now = datetime.utcnow()

    with get_session() as session:
        meta_row = session.execute(
            select(Stablecoin).where(Stablecoin.symbol == sym)
        ).scalar_one_or_none()

        price_row = session.execute(
            select(PriceSnapshot)
            .where(PriceSnapshot.symbol == sym)
            .order_by(PriceSnapshot.recorded_at.desc())
            .limit(1)
        ).scalars().first()

        # Latest supply snapshot. Several DefiLlama assets can share a ticker and
        # land at the same timestamp, so collapse to the dominant (largest) row —
        # consistent with services/market_changes.py.
        latest_supply_ts = session.execute(
            select(func.max(SupplySnapshot.recorded_at)).where(SupplySnapshot.symbol == sym)
        ).scalar_one_or_none()
        supply_row = None
        if latest_supply_ts is not None:
            supply_row = session.execute(
                select(SupplySnapshot)
                .where(
                    SupplySnapshot.symbol == sym,
                    SupplySnapshot.recorded_at == latest_supply_ts,
                )
                .order_by(SupplySnapshot.circulating_supply.desc())
                .limit(1)
            ).scalars().first()

        score_row = session.execute(
            select(RiskScore)
            .where(RiskScore.symbol == sym)
            .order_by(RiskScore.scored_at.desc())
            .limit(1)
        ).scalars().first()

        reserve_row = session.execute(
            select(ReserveReport)
            .where(ReserveReport.symbol == sym)
            .order_by(ReserveReport.ingested_at.desc())
            .limit(1)
        ).scalars().first()

    if not any((meta_row, price_row, supply_row, score_row, reserve_row)):
        return None

    # ── price + liquidity ───────────────────────────────────────────────────
    price: dict[str, Any] | None = None
    if price_row is not None:
        bid = price_row.bid_depth_usd
        ask = price_row.ask_depth_usd
        total_depth = (bid or 0.0) + (ask or 0.0)
        price = {
            "price": price_row.price,
            "peg_deviation_bps": price_row.peg_deviation_bps,
            "bid_depth_usd": bid,
            "ask_depth_usd": ask,
            "total_depth_usd": total_depth if (bid is not None or ask is not None) else None,
            "source": price_row.source,
            "recorded_at": _iso(price_row.recorded_at),
        }

    # ── supply + chain breakdown ──────────────────────────────────────────────
    supply: dict[str, Any] | None = None
    if supply_row is not None:
        chains = _parse_chains(supply_row.supply_by_chain)
        top = chains[0] if chains else None
        supply = {
            "circulating_supply": supply_row.circulating_supply,
            "recorded_at": _iso(supply_row.recorded_at),
            "top_chain": top["chain"] if top else None,
            "top_chain_pct": top["supply_pct"] if top else None,
            "chains": chains,
        }

    # ── risk scores ───────────────────────────────────────────────────────────
    scores: dict[str, Any] | None = None
    if score_row is not None:
        scores = {
            "peg_score": score_row.peg_score,
            "liquidity_score": score_row.liquidity_score,
            "reserve_score": score_row.reserve_score,
            "adoption_score": score_row.adoption_score,
            "overall_score": score_row.overall_score,
            "risk_label": risk_label(score_row.overall_score),
            "scored_at": _iso(score_row.scored_at),
        }

    # ── reserve report ────────────────────────────────────────────────────────
    reserve: dict[str, Any] | None = None
    if reserve_row is not None:
        composition: dict[str, Any] | None = None
        if reserve_row.composition:
            try:
                composition = json.loads(reserve_row.composition)
            except (ValueError, TypeError):
                composition = None
        age_days = None
        is_stale = None
        if reserve_row.report_date is not None:
            age_days = (now.date() - reserve_row.report_date).days
            is_stale = age_days > RESERVE_STALE_DAYS
        reserve = {
            "report_url": reserve_row.report_url,
            "report_date": reserve_row.report_date.isoformat() if reserve_row.report_date else None,
            "auditor": reserve_row.auditor,
            "composition": composition,
            "ingested_at": _iso(reserve_row.ingested_at),
            "age_days": age_days,
            "is_stale": is_stale,
        }

    # ── per-source freshness ──────────────────────────────────────────────────
    freshness = {
        "price": _freshness(
            price_row.recorded_at if price_row else None, PRICE_CADENCE_SECS, now=now
        ),
        "supply": _freshness(
            supply_row.recorded_at if supply_row else None, SUPPLY_CADENCE_SECS, now=now
        ),
        "scores": _freshness(
            score_row.scored_at if score_row else None, PRICE_CADENCE_SECS, now=now
        ),
        "reserve": _freshness(
            reserve_row.ingested_at if reserve_row else None, SUPPLY_CADENCE_SECS, now=now
        ),
    }

    return {
        "symbol": sym,
        "name": meta_row.name if meta_row else None,
        "issuer": meta_row.issuer if meta_row else None,
        "peg_mechanism": meta_row.peg_mechanism if meta_row else None,
        "registered": meta_row is not None,
        "price": price,
        "supply": supply,
        "scores": scores,
        "reserve": reserve,
        "freshness": freshness,
        "generated_at": now.isoformat(),
    }
