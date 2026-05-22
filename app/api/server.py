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


@app.get("/stablecoins/liquidity-drops")
def liquidity_drops(
    window: str = Query(default="24h"),
    limit: int = Query(default=10, le=100),
) -> list[dict]:
    """Cross-asset ranking of the sharpest order-book depth drops.

    ``window`` is "24h" or "7d". Ordered by severity then drop magnitude;
    returns an empty list when no asset has enough history to compare.
    """
    from services.liquidity import largest_liquidity_drops

    if window not in ("24h", "7d"):
        raise HTTPException(status_code=422, detail="window must be '24h' or '7d'")
    return largest_liquidity_drops(window=window, limit=limit)


@app.get("/stablecoins/chain-concentration")
def chain_concentration(limit: int = Query(default=50, le=200)) -> list[dict]:
    """Cross-asset chain concentration ranking, most concentrated first.

    Each entry reports an asset's top chain, top-chain share, chain count, HHI,
    and a plain-language concentration level/severity. Assets with no parseable
    chain breakdown are omitted. Returns an empty list on a brand-new database.

    Defined before ``/stablecoins/{symbol}`` so the literal path is not shadowed
    by the symbol lookup.
    """
    from services.chain_concentration import chain_concentration_ranking

    return chain_concentration_ranking(limit=limit)


@app.get("/stablecoins/rankings")
def stablecoin_rankings(
    window: str = Query(default="7d"),
    limit: int = Query(default=50, le=200),
    movers_limit: int = Query(default=10, le=100),
) -> dict:
    """Market dominance, share, and competitive momentum across all assets.

    Returns total tracked supply, asset count, the dominant asset and its share,
    a ``rankings`` list (market share descending, with 7d/30d share change), and
    a ``movers`` block of gainers/losers for ``window`` ("7d" or "30d"). Always
    returns a structured object, even on a brand-new database.

    Defined before ``/stablecoins/{symbol}`` so the literal path is not shadowed
    by the symbol lookup.
    """
    from services.dominance import compute_dominance, market_share_movers

    if window not in ("7d", "30d"):
        raise HTTPException(status_code=422, detail="window must be '7d' or '30d'")
    result = compute_dominance(limit=limit)
    result["movers"] = market_share_movers(window=window, limit=movers_limit)
    return result


@app.get("/regimes")
def list_regimes() -> list[dict]:
    """Current risk regime per asset, most severe first.

    Each entry is the asset's latest regime classification (Stable, Mild stress,
    Peg stress, Liquidity stress, Data quality concern, or High risk) with the
    score and peg that drove it. Empty list until the scoring pipeline has run.
    """
    from services.regimes import current_regimes

    return current_regimes()


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


@app.get("/stablecoins/{symbol}/supply")
def get_supply(
    symbol: str,
    history_days: int = Query(default=90, ge=1, le=365),
    history_limit: int | None = Query(default=None, ge=1, le=2000),
) -> dict:
    """Circulating-supply detail and history for one asset.

    Returns the latest supply + chain breakdown, 7d/30d supply change (null when
    there is not enough history to compare), and a deduplicated supply time
    series over ``history_days`` (optionally capped to the newest
    ``history_limit`` points). 404 only when the symbol is completely unknown; a
    known asset with no supply data returns null sections rather than erroring.
    """
    from services.supply import get_supply_detail

    detail = get_supply_detail(
        symbol, history_days=history_days, history_limit=history_limit
    )
    if detail is None:
        raise HTTPException(status_code=404, detail=f"{symbol} not found")
    return detail


@app.get("/stablecoins/{symbol}/liquidity")
def get_liquidity(symbol: str) -> dict:
    """Order-book liquidity trend for one asset: 24h/7d depth change + history.

    Always 200 for a known asset; sections are null when their history is
    insufficient. 404 only when the symbol is completely unknown.
    """
    from services.liquidity import get_liquidity_detail

    detail = get_liquidity_detail(symbol)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"{symbol} not found")
    return detail


@app.get("/stablecoins/{symbol}/chain-supply")
def get_chain_supply(symbol: str) -> dict:
    """Chain breakdown + concentration risk for one asset.

    Returns the normalized per-chain rows plus the top-chain share, HHI, and a
    plain-language concentration level. 404 only when the symbol is completely
    unknown; an asset with no chain breakdown returns concentration_level
    "Unknown" with null metrics rather than guessed values.
    """
    from services.chain_concentration import get_chain_concentration

    detail = get_chain_concentration(symbol)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"{symbol} not found")
    return detail


@app.get("/stablecoins/{symbol}/events")
def get_stablecoin_events(symbol: str, limit: int = Query(default=100, le=500)) -> list[dict]:
    """Risk-event timeline for a single asset, newest first."""
    from services.risk_events import query_events

    return query_events(symbol=symbol, limit=limit)


@app.get("/stablecoins/{symbol}/score-explanation")
def get_score_explanation(symbol: str) -> dict:
    """Explain why an asset has its latest risk score.

    Returns a per-dimension drilldown (inputs, weights, point contributions),
    the dimension dragging the score down most, and a plain-language delta
    versus the prior snapshot. 404 only when the asset has no risk score yet.
    """
    from services.score_explanation import explain_scores

    explanation = explain_scores(symbol)
    if explanation is None:
        raise HTTPException(status_code=404, detail=f"No scores for {symbol}")
    return explanation


@app.get("/stablecoins/{symbol}/regime")
def get_regime(symbol: str, history_limit: int = Query(default=100, le=500)) -> dict:
    """Current risk regime and transition history for one asset.

    Always 200: ``current`` is null and ``history`` empty when the asset has not
    been classified yet, so the dashboard can show "not classified" explicitly
    rather than erroring.
    """
    from services.regimes import get_regime_detail

    return get_regime_detail(symbol, history_limit=history_limit)


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


@app.get("/data-quality")
def data_quality(
    symbol: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    warning_type: str | None = Query(default=None),
    active_only: bool = Query(default=True),
    limit: int = Query(default=200, le=500),
) -> dict:
    """Data-quality warnings plus an active-warning summary.

    ``summary`` reports the active-warning total with breakdowns by severity and
    type; ``warnings`` is the newest-first list, by default only active
    (unresolved) warnings. Filter by ``symbol``, ``severity`` (low/medium/high),
    and ``warning_type`` (IMPOSSIBLE_PRICE, NON_POSITIVE_SUPPLY,
    PEG_DEVIATION_MISMATCH, SUPPLY_JUMP, DUPLICATE_SNAPSHOT,
    MISSING_CHAIN_DISTRIBUTION); pass ``active_only=false`` to include resolved
    history. Always returns a structured object, even on a brand-new database.
    """
    from services.data_validation import query_warnings, warning_summary

    return {
        "summary": warning_summary(),
        "warnings": query_warnings(
            symbol=symbol, severity=severity, warning_type=warning_type,
            active_only=active_only, limit=limit,
        ),
    }


@app.get("/provider-fallback")
def provider_fallback(
    window_hours: int = Query(default=24, ge=1, le=720),
    recent_limit: int = Query(default=50, le=500),
) -> dict:
    """Provider fallback status for price ingestion.

    Reports which provider currently serves each asset's price, the
    primary-vs-fallback rate over ``window_hours``, whether any asset is
    currently on the Coinbase fallback, primary-provider health
    (healthy/degraded/failing), and a list of recent fallback events with the
    reason the primary was skipped. Always returns a structured object, even on
    a brand-new database.
    """
    from services.provider_fallback import get_fallback_status

    return get_fallback_status(window_hours=window_hours, recent_limit=recent_limit)


@app.get("/data-freshness")
def data_freshness() -> dict:
    """System-wide freshness per data source and per provider.

    Each source reports its last update, age, expected cadence, and a status
    (fresh / delayed / stale / missing); ``overall_status`` is the worst of
    them. Always returns a structured object, even on a brand-new database.
    """
    from services.freshness import compute_data_freshness

    return compute_data_freshness()
