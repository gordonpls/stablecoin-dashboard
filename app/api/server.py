"""FastAPI server — internal API consumed by the Streamlit dashboard."""

from fastapi import FastAPI, HTTPException, Query
from sqlalchemy import select
from db.models import get_session, Stablecoin, RiskScore, PriceSnapshot

app = FastAPI(title="Stablecoin Dashboard API", version="0.1.0")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/stablecoins")
def list_stablecoins(limit: int = Query(default=100, le=500)) -> list[dict]:
    with get_session() as session:
        rows = session.execute(select(Stablecoin).limit(limit)).scalars().all()
        return [r.to_dict() for r in rows]


@app.get("/stablecoins/changes")
def get_market_changes(limit: int = Query(default=20, le=100)) -> list[dict]:
    """Ranked, plain-language changes since a prior snapshot across all assets.

    Returns an empty list when there is not enough history to compare.
    """
    from services.market_changes import compute_market_changes

    return compute_market_changes(limit=limit)


@app.get("/risk-events")
def list_risk_events(
    symbol: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
) -> list[dict]:
    """Risk-event timeline, newest first, optionally filtered.

    Filter by ``symbol``, ``severity`` (low/medium/high), and ``event_type``
    (PEG_DEVIATION, LIQUIDITY_DROP, SUPPLY_SHOCK, SCORE_CHANGE, RESERVE_STALE,
    API_FAILURE). Returns an empty list when nothing matches.
    """
    from services.risk_events import query_events

    return query_events(
        symbol=symbol, severity=severity, event_type=event_type, limit=limit
    )


@app.get("/stablecoins/{symbol}")
def get_stablecoin(symbol: str) -> dict:
    with get_session() as session:
        row = session.execute(
            select(Stablecoin).where(Stablecoin.symbol == symbol.upper())
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail=f"{symbol} not found")
        return row.to_dict()


@app.get("/stablecoins/{symbol}/scores")
def get_scores(symbol: str) -> dict:
    with get_session() as session:
        row = session.execute(
            select(RiskScore).where(RiskScore.symbol == symbol.upper())
            .order_by(RiskScore.scored_at.desc())
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail=f"No scores for {symbol}")
        return row.to_dict()


@app.get("/stablecoins/{symbol}/prices")
def get_prices(symbol: str, limit: int = Query(default=288, le=1440)) -> list[dict]:
    with get_session() as session:
        rows = session.execute(
            select(PriceSnapshot)
            .where(PriceSnapshot.symbol == symbol.upper())
            .order_by(PriceSnapshot.recorded_at.desc())
            .limit(limit)
        ).scalars().all()
        return [r.to_dict() for r in rows]


@app.get("/stablecoins/{symbol}/events")
def get_stablecoin_events(symbol: str, limit: int = Query(default=100, le=500)) -> list[dict]:
    """Risk-event timeline for a single asset, newest first."""
    from services.risk_events import query_events

    return query_events(symbol=symbol, limit=limit)


@app.get("/stablecoins/{symbol}/profile")
def get_profile(symbol: str) -> dict:
    """Complete per-asset profile: price, supply, chains, scores, reserve, freshness.

    Returns 404 only when the symbol is completely unknown; sections with no
    data are returned as null rather than omitted or guessed.
    """
    from services.profile import get_stablecoin_profile

    profile = get_stablecoin_profile(symbol)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"{symbol} not found")
    return profile


@app.get("/providers/usage")
def provider_usage() -> dict:
    """Return logged API call counts per provider (from the request log table)."""
    from db.models import ApiRequestLog
    from sqlalchemy import func

    with get_session() as session:
        rows = session.execute(
            select(ApiRequestLog.provider, func.count().label("calls"))
            .group_by(ApiRequestLog.provider)
        ).all()
        return {r.provider: r.calls for r in rows}


@app.get("/pipeline-runs")
def pipeline_runs(
    pipeline: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
) -> dict:
    """Pipeline execution history plus a per-pipeline health summary.

    ``summary`` has one entry per pipeline (last status, last run, last
    success, recent failures); ``runs`` is the newest-first run log, optionally
    filtered by ``pipeline`` name and ``status`` (success/error). Always returns
    a structured object, even on a brand-new database.
    """
    from services.pipeline_runs import pipeline_status_summary, query_runs

    return {
        "summary": pipeline_status_summary(),
        "runs": query_runs(pipeline_name=pipeline, status=status, limit=limit),
    }


@app.get("/data-freshness")
def data_freshness() -> dict:
    """System-wide freshness per data source and per provider.

    Each source reports its last update, age, expected cadence, and a status
    (fresh / delayed / stale / missing); ``overall_status`` is the worst of
    them. Always returns a structured object, even on a brand-new database.
    """
    from services.freshness import compute_data_freshness

    return compute_data_freshness()
