"""Service: system-wide data freshness across every source and provider.

Where ``services/profile.py`` reports freshness for a *single asset*, this
module reports it for the *whole dashboard*: for each data source (prices,
liquidity, supply, scores, reserves) it answers "when did this last update, and
is that within the cadence we expect?", and for each external provider it
reports whether recent requests are succeeding.

Read-only over existing tables — shared by the FastAPI ``/data-freshness``
endpoint and the Streamlit "Data Freshness" panel so the two never drift apart.

Source status (mirrors the cadence logic in ``services/profile.py``):

* ``fresh``   — updated within one expected cadence
* ``delayed`` — missed one expected refresh (one to two cadences old)
* ``stale``   — missed two or more refreshes
* ``missing`` — no data of this kind has ever been recorded

Provider status (from ``api_request_log``):

* ``healthy`` — the most recent logged request returned a 2xx status
* ``failing`` — the most recent logged request errored (4xx/5xx or no response)
* ``missing`` — no requests have been logged for this provider
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.sql.elements import ColumnElement

from db.models import (
    ApiRequestLog,
    PriceSnapshot,
    ReserveReport,
    RiskScore,
    SupplySnapshot,
    get_session,
)

# Pipeline cadences (seconds) — mirror services/profile.py and the dashboard.
PRICE_CADENCE_SECS = 600     # prices + liquidity + scores refresh every 10 minutes
SUPPLY_CADENCE_SECS = 3600   # supply + reserves refresh hourly

# Worse = larger. Lets us pick the single worst source for an overall headline.
STATUS_SEVERITY = {"fresh": 0, "delayed": 1, "stale": 2, "missing": 3}


@dataclass(frozen=True)
class _SourceSpec:
    """Static description of one user-facing data source."""

    source: str          # machine key
    label: str           # display name
    provider: str        # which provider feeds it
    metric: str          # plain-language description of what it tracks
    cadence_secs: int    # how often it is expected to refresh


# Ordered most-frequent first. Each maps to the table/column its rows land in.
_SOURCE_SPECS: list[_SourceSpec] = [
    _SourceSpec("prices", "Prices", "Binance / Coinbase", "Price & peg deviation", PRICE_CADENCE_SECS),
    _SourceSpec("liquidity", "Liquidity", "Binance", "Order book depth", PRICE_CADENCE_SECS),
    _SourceSpec("scores", "Risk Scores", "Computed", "Composite risk scores", PRICE_CADENCE_SECS),
    _SourceSpec("supply", "Supply", "DefiLlama", "Circulating supply", SUPPLY_CADENCE_SECS),
    _SourceSpec("reserves", "Reserves", "Curated", "Reserve attestations", SUPPLY_CADENCE_SECS),
]


def classify(age_seconds: float | None, cadence_secs: int) -> str:
    """Classify an age against a cadence. See module docstring for the bands."""
    if age_seconds is None:
        return "missing"
    if age_seconds <= cadence_secs:
        return "fresh"
    if age_seconds <= cadence_secs * 2:
        return "delayed"
    return "stale"


def _source_freshness(
    session,
    spec: _SourceSpec,
    ts_col: ColumnElement,
    symbol_col: ColumnElement,
    *,
    now: datetime,
    extra_filter: ColumnElement | None = None,
) -> dict[str, Any]:
    """Compute freshness for one source from its timestamp/symbol columns.

    ``assets_covered`` counts distinct symbols updated within the last two
    cadences — i.e. how many assets currently have reasonably current data.
    """
    last_q = select(func.max(ts_col))
    if extra_filter is not None:
        last_q = last_q.where(extra_filter)
    last_updated = session.execute(last_q).scalar_one_or_none()

    age = None
    if last_updated is not None:
        age = max(0.0, (now - last_updated).total_seconds())
    status = classify(age, spec.cadence_secs)

    cutoff = now - timedelta(seconds=spec.cadence_secs * 2)
    count_q = select(func.count(func.distinct(symbol_col))).where(ts_col >= cutoff)
    if extra_filter is not None:
        count_q = count_q.where(extra_filter)
    assets_covered = session.execute(count_q).scalar_one() or 0

    return {
        "source": spec.source,
        "label": spec.label,
        "provider": spec.provider,
        "metric": spec.metric,
        "last_updated": last_updated.isoformat() if last_updated is not None else None,
        "age_seconds": round(age) if age is not None else None,
        "expected_cadence_seconds": spec.cadence_secs,
        "status": status,
        "assets_covered": assets_covered,
    }


def _provider_health(session) -> list[dict[str, Any]]:
    """Per-provider request health from the API request log.

    A provider is ``failing`` when its most recent logged request errored, so a
    string of past successes does not mask a current outage.
    """
    providers = session.execute(select(ApiRequestLog.provider).distinct()).scalars().all()

    out: list[dict[str, Any]] = []
    for provider in sorted(providers):
        total = session.execute(
            select(func.count()).where(ApiRequestLog.provider == provider)
        ).scalar_one()
        errors = session.execute(
            select(func.count()).where(
                ApiRequestLog.provider == provider,
                (ApiRequestLog.status_code.is_(None)) | (ApiRequestLog.status_code >= 400),
            )
        ).scalar_one()
        latest = session.execute(
            select(ApiRequestLog)
            .where(ApiRequestLog.provider == provider)
            .order_by(ApiRequestLog.requested_at.desc())
            .limit(1)
        ).scalars().first()

        last_status = latest.status_code if latest is not None else None
        last_at = latest.requested_at if latest is not None else None
        if latest is None:
            status = "missing"
        elif last_status is None or last_status >= 400:
            status = "failing"
        else:
            status = "healthy"

        out.append({
            "provider": provider,
            "last_request_at": last_at.isoformat() if last_at is not None else None,
            "last_status_code": last_status,
            "total_requests": total,
            "error_count": errors,
            "status": status,
        })
    return out


def compute_data_freshness() -> dict[str, Any]:
    """Assemble system-wide freshness for every source and provider.

    ``overall_status`` is the worst per-source status, so a single stale or
    missing source surfaces in the headline even when everything else is fresh.
    """
    now = datetime.utcnow()

    with get_session() as session:
        depth_present = PriceSnapshot.bid_depth_usd.isnot(None) | PriceSnapshot.ask_depth_usd.isnot(None)
        sources = [
            _source_freshness(session, _SOURCE_SPECS[0], PriceSnapshot.recorded_at,
                              PriceSnapshot.symbol, now=now),
            _source_freshness(session, _SOURCE_SPECS[1], PriceSnapshot.recorded_at,
                              PriceSnapshot.symbol, now=now, extra_filter=depth_present),
            _source_freshness(session, _SOURCE_SPECS[2], RiskScore.scored_at,
                              RiskScore.symbol, now=now),
            _source_freshness(session, _SOURCE_SPECS[3], SupplySnapshot.recorded_at,
                              SupplySnapshot.symbol, now=now),
            _source_freshness(session, _SOURCE_SPECS[4], ReserveReport.ingested_at,
                              ReserveReport.symbol, now=now),
        ]
        providers = _provider_health(session)

    overall = max((s["status"] for s in sources), key=lambda st: STATUS_SEVERITY[st])

    return {
        "generated_at": now.isoformat(),
        "overall_status": overall,
        "sources": sources,
        "providers": providers,
    }
