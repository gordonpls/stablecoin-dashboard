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
