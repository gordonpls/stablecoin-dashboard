"""Pipeline: compute risk scores from latest supply, price, and reserve data."""

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from db.models import (
    RiskScore, SupplySnapshot, PriceSnapshot, ReserveReport, Stablecoin,
    get_session, init_db,
)

logger = logging.getLogger(__name__)

LARGE_CAP_THRESHOLD = 5_000_000_000   # $5B supply = max adoption score
MAX_PEG_DEVIATION_BPS = 100           # 100 bps = score 0
MAX_LIQUIDITY_USD = 50_000_000        # $50M depth = max score
STALE_RESERVE_DAYS = 90

# Weight of each dimension in the overall score. Single source of truth shared
# with services.score_explanation so the drilldown can never disagree with how
# the pipeline actually combines the dimensions. Must sum to 1.0.
SCORE_WEIGHTS: dict[str, float] = {
    "peg":       0.35,
    "liquidity": 0.25,
    "reserve":   0.25,
    "adoption":  0.15,
}


def _peg_score(deviation_bps: float | None) -> float:
    if deviation_bps is None:
        return 50.0
    return max(0.0, 100.0 - (deviation_bps / MAX_PEG_DEVIATION_BPS) * 100.0)


def _liquidity_score(bid: float | None, ask: float | None) -> float:
    if bid is None and ask is None:
        return 50.0
    depth = (bid or 0) + (ask or 0)
    return min(100.0, (depth / MAX_LIQUIDITY_USD) * 100.0)


def _reserve_score(report: ReserveReport | None) -> float:
    if report is None:
        return 20.0
    if report.report_date is None:
        return 40.0
    age_days = (datetime.utcnow().date() - report.report_date).days
    freshness = max(0.0, 1.0 - age_days / STALE_RESERVE_DAYS)
    auditor_bonus = 10.0 if report.auditor else 0.0
    return min(100.0, freshness * 90.0 + auditor_bonus)


def _adoption_score(supply_usd: float | None) -> float:
    if supply_usd is None:
        return 0.0
    return min(100.0, (supply_usd / LARGE_CAP_THRESHOLD) * 100.0)


def run() -> None:
    init_db()
    from services.pipeline_runs import record_run

    with record_run("score_stablecoins") as rec:
        _run_scoring(rec)


def _run_scoring(rec) -> None:
    now = datetime.utcnow()
    cutoff = now - timedelta(hours=2)

    with get_session() as session:
        symbols = session.execute(select(Stablecoin.symbol)).scalars().all()
        scores = []
        for symbol in symbols:
            price_row = session.execute(
                select(PriceSnapshot)
                .where(PriceSnapshot.symbol == symbol, PriceSnapshot.recorded_at >= cutoff)
                .order_by(PriceSnapshot.recorded_at.desc())
                .limit(1)
            ).scalars().first()

            supply_row = session.execute(
                select(SupplySnapshot)
                .where(SupplySnapshot.symbol == symbol)
                .order_by(SupplySnapshot.recorded_at.desc())
                .limit(1)
            ).scalars().first()

            reserve_row = session.execute(
                select(ReserveReport)
                .where(ReserveReport.symbol == symbol)
                .order_by(ReserveReport.ingested_at.desc())
                .limit(1)
            ).scalars().first()

            peg = _peg_score(price_row.peg_deviation_bps if price_row else None)
            liquidity = _liquidity_score(
                price_row.bid_depth_usd if price_row else None,
                price_row.ask_depth_usd if price_row else None,
            )
            reserve = _reserve_score(reserve_row)
            adoption = _adoption_score(supply_row.circulating_supply if supply_row else None)
            overall = round(
                peg * SCORE_WEIGHTS["peg"]
                + liquidity * SCORE_WEIGHTS["liquidity"]
                + reserve * SCORE_WEIGHTS["reserve"]
                + adoption * SCORE_WEIGHTS["adoption"],
                2,
            )

            scores.append(RiskScore(
                symbol=symbol,
                peg_score=round(peg, 2),
                liquidity_score=round(liquidity, 2),
                reserve_score=round(reserve, 2),
                adoption_score=round(adoption, 2),
                overall_score=overall,
                scored_at=now,
            ))

        session.add_all(scores)
        session.commit()

    rec.rows_written = len(scores)
    logger.info("scoring_complete count=%d", len(scores))

    # Detect and log notable risk changes from the freshly-scored data. Kept
    # best-effort so a detection bug can never fail the scoring run itself.
    try:
        from services.risk_events import log_new_events

        events = log_new_events(now=now)
        logger.info("risk_events_logged count=%d", len(events))
    except Exception as exc:  # noqa: BLE001 - detection is non-critical
        logger.warning("risk_event_detection_failed error=%s", exc)

    # Validate the stored data and open/resolve data-quality warnings. Also
    # best-effort: a validation bug must never fail the scoring run.
    try:
        from services.data_validation import run_validation

        result = run_validation(now=now)
        logger.info(
            "data_validation_complete opened=%d resolved=%d",
            len(result["opened"]), len(result["resolved"]),
        )
    except Exception as exc:  # noqa: BLE001 - validation is non-critical
        logger.warning("data_validation_failed error=%s", exc)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
