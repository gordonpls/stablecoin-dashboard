"""Service: classify each stablecoin into a plain-language *risk regime*.

A regime turns the raw 0–100 scores and basis-point peg readings into one of a
small set of human-readable states a user can grasp at a glance:

- ``Stable`` — strong score and a calm peg.
- ``Mild stress`` — a middling score or a peg drifting modestly off $1.00.
- ``Data quality concern`` — the underlying numbers look suspect (an active
  data-quality warning is open), so the score can't be fully trusted.
- ``Liquidity stress`` — order-book depth is thin enough to make the peg fragile.
- ``Peg stress`` — the peg has deviated into the stress band but not yet broken.
- ``High risk`` — a real peg break, or a broadly failing overall score.

``classify_regime`` is a pure, deterministic function of the latest score, peg
deviation, liquidity-dimension score, and whether a data-quality warning is
open. Its thresholds mirror the dashboard's ``risk_label`` bands and the
``services.risk_events`` peg thresholds, so the regime never disagrees with the
score and peg logic shown elsewhere.

``record_regimes`` is called by the scoring pipeline. It appends a
``RegimeSnapshot`` **only when an asset's regime changes**, so the table is a
compact transition history (the newest row per symbol is the current regime).
``services.risk_events`` reads those transitions and emits ``REGIME_CHANGE``
events. The read helpers (``current_regimes``, ``get_regime``,
``get_regime_detail``) back the FastAPI endpoints and the Streamlit dashboard so
detection and display can never drift apart.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from db.models import (
    DataQualityWarning,
    PriceSnapshot,
    RegimeSnapshot,
    RiskScore,
    get_session,
)

# ── regime labels ─────────────────────────────────────────────────────────────

STABLE = "Stable"
MILD_STRESS = "Mild stress"
DATA_QUALITY_CONCERN = "Data quality concern"
LIQUIDITY_STRESS = "Liquidity stress"
PEG_STRESS = "Peg stress"
HIGH_RISK = "High risk"

# Ordered from calmest to most severe. The rank doubles as the stored numeric
# value on a RiskEvent so transitions are comparable, and orders the dashboard.
REGIMES = (
    STABLE,
    MILD_STRESS,
    DATA_QUALITY_CONCERN,
    LIQUIDITY_STRESS,
    PEG_STRESS,
    HIGH_RISK,
)
REGIME_RANK: dict[str, int] = {regime: i for i, regime in enumerate(REGIMES)}

# Inherent severity of each regime (for badge colouring), in the low/medium/high
# vocabulary shared with risk events and data-quality warnings.
REGIME_SEVERITY: dict[str, str] = {
    STABLE: "low",
    MILD_STRESS: "medium",
    DATA_QUALITY_CONCERN: "medium",
    LIQUIDITY_STRESS: "medium",
    PEG_STRESS: "medium",
    HIGH_RISK: "high",
}

# ── thresholds (mirror risk_label bands and risk_events peg thresholds) ─────────

STABLE_SCORE = 80.0       # overall score at/above this (with a calm peg) → Stable
HIGH_RISK_SCORE = 60.0    # overall score below this → High risk

PEG_CALM_BPS = 10.0       # peg deviation below this is "calm"
PEG_STRESS_BPS = 25.0     # peg deviation at/above this (below high) → Peg stress
PEG_HIGH_BPS = 50.0       # peg deviation above this → High risk

# Liquidity *dimension* score (0–100) below this → Liquidity stress.
LIQUIDITY_STRESS_SCORE = 30.0

# Data-quality warning severities that are serious enough to flag the regime.
_DQ_CONCERN_SEVERITIES = ("medium", "high")


def _result(regime: str, reason: str) -> dict[str, Any]:
    return {"regime": regime, "severity": REGIME_SEVERITY[regime], "reason": reason}


def classify_regime(
    overall_score: float | None,
    peg_bps: float | None,
    liquidity_score: float | None,
    dq_concern: bool,
) -> dict[str, Any]:
    """Classify one asset's condition into a regime. Pure and deterministic.

    Rules are applied most-severe first so a single condition can decide the
    regime. Returns ``{regime, severity, reason}``; ``reason`` is a short
    plain-language justification suitable for a tooltip or callout.
    """
    # 1. High risk — a real de-peg or a broadly failing score dominates everything.
    if peg_bps is not None and peg_bps > PEG_HIGH_BPS:
        return _result(
            HIGH_RISK,
            f"peg deviation {peg_bps:.0f} bps is past the {PEG_HIGH_BPS:.0f} bps break level",
        )
    if overall_score is not None and overall_score < HIGH_RISK_SCORE:
        return _result(
            HIGH_RISK,
            f"overall score {overall_score:.0f} is below {HIGH_RISK_SCORE:.0f}",
        )

    # 2. Peg stress — peg elevated into the stress band but not yet a break.
    if peg_bps is not None and peg_bps >= PEG_STRESS_BPS:
        return _result(
            PEG_STRESS,
            f"peg deviation {peg_bps:.0f} bps is in the "
            f"{PEG_STRESS_BPS:.0f}–{PEG_HIGH_BPS:.0f} bps stress band",
        )

    # 3. Liquidity stress — order-book depth is thin enough to make the peg fragile.
    if liquidity_score is not None and liquidity_score < LIQUIDITY_STRESS_SCORE:
        return _result(
            LIQUIDITY_STRESS,
            f"liquidity score {liquidity_score:.0f} is below {LIQUIDITY_STRESS_SCORE:.0f}",
        )

    # 4. Data quality concern — the inputs look suspect, so the score can't be trusted.
    if dq_concern:
        return _result(
            DATA_QUALITY_CONCERN,
            "an active data-quality warning is open for this asset",
        )

    # 5. Mild stress — middling score or a peg drifting modestly off $1.00.
    bits: list[str] = []
    if overall_score is not None and overall_score < STABLE_SCORE:
        bits.append(f"overall score {overall_score:.0f} is below {STABLE_SCORE:.0f}")
    if peg_bps is not None and peg_bps >= PEG_CALM_BPS:
        bits.append(f"peg deviation {peg_bps:.0f} bps is above {PEG_CALM_BPS:.0f}")
    if bits:
        return _result(MILD_STRESS, " and ".join(bits))

    # 6. Stable — strong score and a calm (or unknown but neutral) peg.
    if peg_bps is None:
        return _result(STABLE, "overall score is strong; no recent peg reading")
    return _result(STABLE, "overall score is strong and the peg is calm")


# ── detection / persistence ─────────────────────────────────────────────────────

def _active_dq_symbols(session) -> set[str]:
    """Symbols with an open (unresolved) medium/high data-quality warning."""
    rows = session.execute(
        select(DataQualityWarning.symbol)
        .where(
            DataQualityWarning.resolved_at.is_(None),
            DataQualityWarning.severity.in_(_DQ_CONCERN_SEVERITIES),
            DataQualityWarning.symbol.is_not(None),
        )
        .distinct()
    ).scalars().all()
    return set(rows)


def _latest_scores(session) -> list[RiskScore]:
    score_sq = (
        select(RiskScore.symbol, func.max(RiskScore.scored_at).label("ts"))
        .group_by(RiskScore.symbol)
        .subquery()
    )
    return session.execute(
        select(RiskScore).join(
            score_sq,
            (RiskScore.symbol == score_sq.c.symbol)
            & (RiskScore.scored_at == score_sq.c.ts),
        )
    ).scalars().all()


def _latest_peg_bps(session, symbol: str) -> float | None:
    row = session.execute(
        select(PriceSnapshot.peg_deviation_bps)
        .where(PriceSnapshot.symbol == symbol)
        .order_by(PriceSnapshot.recorded_at.desc())
        .limit(1)
    ).first()
    return row[0] if row is not None else None


def record_regimes(now: datetime | None = None) -> list[dict]:
    """Classify every scored asset and append a snapshot only when its regime changes.

    Idempotent: re-running over unchanged data inserts nothing, because a snapshot
    is written only when the freshly-computed regime differs from the most recent
    stored one. Returns the rows it inserted (as dicts); each carries
    ``from_regime`` (``None`` for an asset's first-ever classification) so callers
    can distinguish a genuine transition from an initial label.
    """
    now = now or datetime.utcnow()
    inserted: list[dict] = []

    with get_session() as session:
        dq_symbols = _active_dq_symbols(session)

        for score in _latest_scores(session):
            symbol = score.symbol
            peg_bps = _latest_peg_bps(session, symbol)
            result = classify_regime(
                score.overall_score, peg_bps, score.liquidity_score,
                symbol in dq_symbols,
            )

            last = session.execute(
                select(RegimeSnapshot)
                .where(RegimeSnapshot.symbol == symbol)
                .order_by(RegimeSnapshot.classified_at.desc(), RegimeSnapshot.id.desc())
                .limit(1)
            ).scalars().first()

            if last is not None and last.regime == result["regime"]:
                continue  # unchanged — keep history compact and detection idempotent

            snap = RegimeSnapshot(
                symbol=symbol,
                regime=result["regime"],
                severity=result["severity"],
                reason=result["reason"],
                overall_score=score.overall_score,
                peg_deviation_bps=peg_bps,
                classified_at=now,
            )
            session.add(snap)
            session.flush()  # populate id before snapshotting to a dict
            row = snap.to_dict()
            row["from_regime"] = last.regime if last is not None else None
            inserted.append(row)

        session.commit()

    return inserted


# ── queries ─────────────────────────────────────────────────────────────────────

def current_regimes() -> list[dict]:
    """Latest regime per asset, most severe first then by symbol.

    Returns an empty list when no regimes have been recorded yet.
    """
    with get_session() as session:
        latest_sq = (
            select(
                RegimeSnapshot.symbol,
                func.max(RegimeSnapshot.classified_at).label("ts"),
            )
            .group_by(RegimeSnapshot.symbol)
            .subquery()
        )
        rows = session.execute(
            select(RegimeSnapshot).join(
                latest_sq,
                (RegimeSnapshot.symbol == latest_sq.c.symbol)
                & (RegimeSnapshot.classified_at == latest_sq.c.ts),
            )
        ).scalars().all()

    result = [r.to_dict() for r in rows]
    result.sort(key=lambda r: (-REGIME_RANK.get(r["regime"], 0), r["symbol"]))
    return result


def get_regime(symbol: str) -> dict | None:
    """Current regime for one asset (case-insensitive), or ``None`` if unclassified."""
    sym = symbol.upper()
    with get_session() as session:
        row = session.execute(
            select(RegimeSnapshot)
            .where(RegimeSnapshot.symbol == sym)
            .order_by(RegimeSnapshot.classified_at.desc(), RegimeSnapshot.id.desc())
            .limit(1)
        ).scalars().first()
        return row.to_dict() if row is not None else None


def regime_history(symbol: str, limit: int = 100) -> list[dict]:
    """Regime transitions for one asset, newest first (each row is a change)."""
    sym = symbol.upper()
    with get_session() as session:
        rows = session.execute(
            select(RegimeSnapshot)
            .where(RegimeSnapshot.symbol == sym)
            .order_by(RegimeSnapshot.classified_at.desc(), RegimeSnapshot.id.desc())
            .limit(limit)
        ).scalars().all()
        return [r.to_dict() for r in rows]


def get_regime_detail(symbol: str, history_limit: int = 100) -> dict:
    """Current regime plus the transition history for one asset (always 200-safe)."""
    sym = symbol.upper()
    history = regime_history(sym, limit=history_limit)
    return {
        "symbol": sym,
        "current": history[0] if history else None,
        "history": history,
    }
