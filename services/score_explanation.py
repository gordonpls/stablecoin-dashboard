"""Service: explain *why* a stablecoin has the risk score it does.

The scoring pipeline (``pipelines.score_stablecoins``) stores four 0–100
dimension scores and a weighted overall. This service turns those stored numbers
back into a human-readable drilldown:

- the raw inputs that drove each dimension (peg deviation, order-book depth,
  reserve age / auditor, circulating supply),
- each dimension's weight and how many points it contributes to the overall,
- which dimension is the biggest drag on the score, and
- a plain-language explanation of how the overall score moved versus the prior
  snapshot.

Read-only over existing tables. Shared by the FastAPI
``/stablecoins/{symbol}/score-explanation`` endpoint and the Streamlit dashboard
so the two never drift apart.

``explain_scores`` returns ``None`` when the asset has no risk score yet, so
callers can return a clean 404. The headline numbers come straight from the
latest stored ``RiskScore`` (identical to what the rest of the dashboard shows);
the inputs come from the latest price / supply / reserve snapshots and carry
their own timestamps, so a user can see when inputs are newer than the score.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from db.models import (
    PriceSnapshot,
    ReserveReport,
    RiskScore,
    SupplySnapshot,
    get_session,
)
from pipelines.score_stablecoins import (
    LARGE_CAP_THRESHOLD,
    MAX_LIQUIDITY_USD,
    MAX_PEG_DEVIATION_BPS,
    SCORE_WEIGHTS,
    STALE_RESERVE_DAYS,
)

# How each dimension reads when its score moves. Index 0 = score went *down*
# (the underlying condition worsened); index 1 = score went *up* (improved).
_PHRASING: dict[str, tuple[str, str]] = {
    "peg":       ("peg deviation widened", "peg deviation narrowed"),
    "liquidity": ("liquidity depth fell", "liquidity depth rose"),
    "reserve":   ("reserve freshness weakened", "reserve freshness improved"),
    "adoption":  ("circulating supply shrank", "circulating supply grew"),
}

_LABELS: dict[str, str] = {
    "peg":       "Peg Stability",
    "liquidity": "Liquidity Depth",
    "reserve":   "Reserve Quality",
    "adoption":  "Adoption / Size",
}

# A dimension score must move by at least this much to be called out in the
# delta narrative — keeps sub-point rounding noise out of the explanation.
_MATERIAL_DELTA = 0.5


def risk_label(score: float | None) -> str:
    """Stability grade (S&P-style) — identical thresholds to the dashboard."""
    if score is None:
        return "—"
    if score >= 80:
        return "Strong"
    if score >= 60:
        return "Adequate"
    if score >= 40:
        return "Constrained"
    return "Weak"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _fmt_usd(v: float | None) -> str:
    if v is None:
        return "no data"
    if v >= 1e9:
        return f"${v / 1e9:.2f}B"
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    return f"${v:,.0f}"


def _peg_detail(bps: float | None) -> str:
    if bps is None:
        return (
            "No recent price snapshot, so peg stability defaults to a neutral 50. "
            f"A perfect $1.00 peg scores 100; the score reaches 0 at "
            f"{MAX_PEG_DEVIATION_BPS:.0f} bps of deviation."
        )
    return (
        f"Peg deviation is {bps:.1f} bps from $1.00. The score is "
        f"100 − (deviation ÷ {MAX_PEG_DEVIATION_BPS:.0f} bps) × 100, hitting 0 at "
        f"{MAX_PEG_DEVIATION_BPS:.0f} bps."
    )


def _liquidity_detail(bid: float | None, ask: float | None) -> str:
    if bid is None and ask is None:
        return (
            "No order-book depth recorded, so liquidity defaults to a neutral 50. "
            f"Full marks need {_fmt_usd(MAX_LIQUIDITY_USD)} of combined bid + ask depth."
        )
    depth = (bid or 0.0) + (ask or 0.0)
    return (
        f"Order-book depth totals {_fmt_usd(depth)} (bid {_fmt_usd(bid)} + "
        f"ask {_fmt_usd(ask)}). The score scales linearly to 100 at "
        f"{_fmt_usd(MAX_LIQUIDITY_USD)} of combined depth."
    )


def _reserve_detail(report_date, auditor: str | None, age_days: int | None) -> str:
    if report_date is None and auditor is None:
        return (
            "No reserve attestation on file, so the reserve dimension scores a "
            "low default. Fresh, independently audited reserves score highest."
        )
    parts: list[str] = []
    if age_days is not None:
        if age_days > STALE_RESERVE_DAYS:
            parts.append(
                f"the latest attestation is {age_days} days old (past the "
                f"{STALE_RESERVE_DAYS}-day staleness limit, so freshness scores 0)"
            )
        else:
            parts.append(
                f"the latest attestation is {age_days} days old; freshness fades "
                f"linearly to 0 at {STALE_RESERVE_DAYS} days"
            )
    else:
        parts.append("no attestation date is recorded")
    parts.append(
        f"an independent auditor ({auditor}) adds a 10-point bonus" if auditor
        else "no independent auditor is recorded, so no audit bonus applies"
    )
    return "Reserve quality: " + "; ".join(parts) + "."


def _adoption_detail(supply: float | None) -> str:
    if supply is None:
        return (
            "No circulating-supply snapshot, so adoption scores 0. The score "
            f"scales linearly to 100 at {_fmt_usd(LARGE_CAP_THRESHOLD)} of supply."
        )
    return (
        f"Circulating supply is {_fmt_usd(supply)}. Adoption scales linearly to "
        f"100 at {_fmt_usd(LARGE_CAP_THRESHOLD)} of supply."
    )


def _components(
    score_row: RiskScore,
    price_row: PriceSnapshot | None,
    supply_row: SupplySnapshot | None,
    reserve_row: ReserveReport | None,
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    """Build the per-dimension explanation cards for the latest score."""
    bid = price_row.bid_depth_usd if price_row else None
    ask = price_row.ask_depth_usd if price_row else None
    bps = price_row.peg_deviation_bps if price_row else None
    supply_val = supply_row.circulating_supply if supply_row else None

    report_date = reserve_row.report_date if reserve_row else None
    auditor = reserve_row.auditor if reserve_row else None
    reserve_age_days = (now.date() - report_date).days if report_date is not None else None

    specs = [
        {
            "key": "peg",
            "score": score_row.peg_score,
            "inputs": {
                "peg_deviation_bps": bps,
                "recorded_at": _iso(price_row.recorded_at) if price_row else None,
            },
            "formula": "100 − (peg_deviation_bps ÷ 100) × 100, floored at 0; 50 when no price",
            "detail": _peg_detail(bps),
        },
        {
            "key": "liquidity",
            "score": score_row.liquidity_score,
            "inputs": {
                "bid_depth_usd": bid,
                "ask_depth_usd": ask,
                "total_depth_usd": ((bid or 0.0) + (ask or 0.0)) if (bid is not None or ask is not None) else None,
                "recorded_at": _iso(price_row.recorded_at) if price_row else None,
            },
            "formula": "min(100, total_depth_usd ÷ 50M × 100); 50 when no depth",
            "detail": _liquidity_detail(bid, ask),
        },
        {
            "key": "reserve",
            "score": score_row.reserve_score,
            "inputs": {
                "report_date": report_date.isoformat() if report_date is not None else None,
                "age_days": reserve_age_days,
                "auditor": auditor,
            },
            "formula": "freshness(0→1 over 90 days) × 90 + 10 if audited; low default when no report",
            "detail": _reserve_detail(report_date, auditor, reserve_age_days),
        },
        {
            "key": "adoption",
            "score": score_row.adoption_score,
            "inputs": {
                "circulating_supply": supply_val,
                "recorded_at": _iso(supply_row.recorded_at) if supply_row else None,
            },
            "formula": "min(100, circulating_supply ÷ 5B × 100); 0 when no supply",
            "detail": _adoption_detail(supply_val),
        },
    ]

    components: list[dict[str, Any]] = []
    for spec in specs:
        key = spec["key"]
        score = float(spec["score"])
        weight = SCORE_WEIGHTS[key]
        components.append({
            "key": key,
            "label": _LABELS[key],
            "score": round(score, 2),
            "weight": weight,
            "weight_pct": round(weight * 100, 0),
            # How many overall points this dimension actually contributes …
            "weighted_contribution": round(score * weight, 2),
            # … and how many it gives up versus a perfect 100 in this dimension.
            # The largest value here is the biggest drag on the overall score.
            "points_lost": round((100.0 - score) * weight, 2),
            "inputs": spec["inputs"],
            "formula": spec["formula"],
            "detail": spec["detail"],
        })
    return components


def _delta(symbol: str, current: RiskScore, session) -> dict[str, Any]:
    """Explain how the score moved versus the immediately prior snapshot."""
    previous = session.execute(
        select(RiskScore)
        .where(RiskScore.symbol == symbol, RiskScore.scored_at < current.scored_at)
        .order_by(RiskScore.scored_at.desc())
        .limit(1)
    ).scalars().first()

    if previous is None:
        return {
            "available": False,
            "summary": f"{symbol} has only one risk-score snapshot, so there is "
                       "no prior score to compare against yet.",
        }

    fields = {
        "peg": "peg_score",
        "liquidity": "liquidity_score",
        "reserve": "reserve_score",
        "adoption": "adoption_score",
    }
    comp_deltas = {
        key: round(float(getattr(current, attr)) - float(getattr(previous, attr)), 2)
        for key, attr in fields.items()
    }
    overall_change = round(current.overall_score - previous.overall_score, 2)

    # Build the narrative from the dimensions that moved materially, biggest first.
    movers = sorted(
        (k for k, d in comp_deltas.items() if abs(d) >= _MATERIAL_DELTA),
        key=lambda k: abs(comp_deltas[k]),
        reverse=True,
    )
    phrases = [_PHRASING[k][1 if comp_deltas[k] > 0 else 0] for k in movers]

    if abs(overall_change) < _MATERIAL_DELTA and not phrases:
        summary = f"{symbol} overall score held steady at {current.overall_score:.0f}."
    else:
        direction = "rose" if overall_change > 0 else "fell" if overall_change < 0 else "was unchanged"
        summary = (
            f"{symbol} overall score {direction} from {previous.overall_score:.0f} "
            f"to {current.overall_score:.0f}"
        )
        if phrases:
            if len(phrases) == 1:
                reason = phrases[0]
            elif len(phrases) == 2:
                reason = f"{phrases[0]} and {phrases[1]}"
            else:
                reason = ", ".join(phrases[:-1]) + f", and {phrases[-1]}"
            summary += f" because {reason}"
        summary += "."

    return {
        "available": True,
        "previous_scored_at": _iso(previous.scored_at),
        "previous_overall": previous.overall_score,
        "current_overall": current.overall_score,
        "overall_change": overall_change,
        "components": comp_deltas,
        "summary": summary,
    }


def explain_scores(symbol: str) -> dict[str, Any] | None:
    """Explain the latest risk score for ``symbol`` (case-insensitive).

    Returns ``None`` when the asset has no risk score yet. Otherwise returns a
    structured drilldown: per-dimension inputs / weights / contributions, the
    weakest dimension, and a plain-language delta versus the prior snapshot.
    """
    sym = symbol.upper()
    now = datetime.utcnow()

    with get_session() as session:
        score_row = session.execute(
            select(RiskScore)
            .where(RiskScore.symbol == sym)
            .order_by(RiskScore.scored_at.desc())
            .limit(1)
        ).scalars().first()

        if score_row is None:
            return None

        price_row = session.execute(
            select(PriceSnapshot)
            .where(PriceSnapshot.symbol == sym)
            .order_by(PriceSnapshot.recorded_at.desc())
            .limit(1)
        ).scalars().first()

        # Several DefiLlama assets can share a ticker at the same timestamp;
        # collapse to the dominant (largest) row, consistent with services/profile.py.
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

        reserve_row = session.execute(
            select(ReserveReport)
            .where(ReserveReport.symbol == sym)
            .order_by(ReserveReport.ingested_at.desc())
            .limit(1)
        ).scalars().first()

        components = _components(score_row, price_row, supply_row, reserve_row, now=now)
        delta = _delta(sym, score_row, session)

    weakest = max(components, key=lambda c: c["points_lost"])
    # When every dimension is already at 100 there is nothing dragging the score.
    if weakest["points_lost"] <= 0:
        weakest_key: str | None = None
        weakest_explanation = (
            f"{sym} scores a perfect 100 across every dimension — nothing is "
            "dragging the overall score down."
        )
    else:
        weakest_key = weakest["key"]
        max_points = round(weakest["weight"] * 100, 1)
        weakest_explanation = (
            f"{weakest['label']} is the biggest drag on {sym}'s overall score, "
            f"giving up {weakest['points_lost']:.1f} of its {max_points:.0f} possible points."
        )

    return {
        "symbol": sym,
        "scored_at": _iso(score_row.scored_at),
        "overall_score": score_row.overall_score,
        "risk_label": risk_label(score_row.overall_score),
        "weights": dict(SCORE_WEIGHTS),
        "components": components,
        "weakest_component": weakest_key,
        "weakest_explanation": weakest_explanation,
        "delta": delta,
        "generated_at": now.isoformat(),
    }
