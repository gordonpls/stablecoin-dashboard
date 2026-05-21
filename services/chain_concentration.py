"""Service: chain concentration risk per stablecoin.

DefiLlama reports each asset's supply split across blockchains; that breakdown is
already stored as JSON in ``supply_snapshots.supply_by_chain``. A stablecoin whose
supply lives almost entirely on one chain carries a *platform* risk separate from
its peg or reserves: a single-chain outage, congestion event, or bridge exploit
would freeze most of the float. This service turns the raw chain JSON into
explainable concentration signals:

- normalized chain rows (``chain``, ``supply``, ``supply_pct``) — "queryable as
  rows, not only JSON",
- the top chain and its share of total supply (the spec's
  ``top_chain_concentration``, expressed here as a 0–100 percentage),
- a Herfindahl-Hirschman Index (HHI, 0–10000) — the standard, explainable
  concentration measure (sum of squared percentage shares),
- a plain-language ``concentration_level`` + ``severity`` and a ``warning`` flag,
- a cross-asset ranking so users can compare concentration across stablecoins.

Read-only over ``supply_snapshots`` — shared by the FastAPI
``/stablecoins/{symbol}/chain-supply`` and ``/stablecoins/chain-concentration``
endpoints and the Streamlit dashboard so the two never drift apart. Chain JSON is
parsed by ``services.profile._parse_chains`` (the single canonical parser) so the
profile page and this service can never disagree on an asset's chain split.

Missing or unparseable chain data is surfaced explicitly (``concentration_level``
= "Unknown", metrics ``None``) rather than guessed, keeping "no breakdown" distinct
from "diversified".
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select

from db.models import Stablecoin, SupplySnapshot, get_session
from services.profile import _parse_chains

# Higher rank = more urgent. Mirrors services.market_changes / services.liquidity.
SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3}

# Share of supply on the single largest chain that we treat as a hard warning.
# Matches the 75% threshold the Asset Profile page already warns at, so the two
# surfaces never disagree on what counts as "concentration risk".
HIGH_CONCENTRATION_PCT = 75.0


def _hhi(chains: list[dict[str, Any]]) -> float | None:
    """Herfindahl-Hirschman Index over chain shares (0–10000).

    Sum of squared *percentage* shares: a single chain → 10000, an even split
    across N chains → 10000/N. ``None`` when there is no chain data.
    """
    if not chains:
        return None
    return round(sum((c.get("supply_pct") or 0.0) ** 2 for c in chains), 1)


def _classify(top_pct: float, chain_count: int) -> tuple[str, str]:
    """Map (top-chain share, #chains) to a (level, severity) pair.

    Bands are graded around the shared 75% warning threshold so the label and
    the ``warning`` flag stay consistent with the Asset Profile page.
    """
    if chain_count <= 1:
        return "Single-chain", "high"
    if top_pct >= HIGH_CONCENTRATION_PCT:
        return "Highly concentrated", "high"
    if top_pct >= 50.0:
        return "Concentrated", "medium"
    if top_pct >= 35.0:
        return "Moderately diversified", "low"
    return "Diversified", "info"


def _summary(symbol: str, top_chain: str, top_pct: float, chain_count: int, severity: str) -> str:
    if chain_count <= 1:
        return f"{symbol}'s entire supply is on {top_chain} — a single-chain outage would take it fully offline."
    base = f"{symbol}: {top_pct:.0f}% of supply is on {top_chain}, across {chain_count} chains."
    if severity == "high":
        return base + " A single-chain outage or exploit would affect most of the supply."
    return base


def _concentration_from_chains(symbol: str, chains: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the concentration metric block from already-parsed chain rows.

    ``chains`` is the output of ``_parse_chains`` (sorted by supply desc, each
    with a ``supply_pct``). An empty list yields an "Unknown" block with ``None``
    metrics so callers can show "no breakdown" rather than a fabricated zero.
    """
    if not chains:
        return {
            "total_supply": None,
            "chain_count": 0,
            "top_chain": None,
            "top_chain_pct": None,
            "hhi": None,
            "concentration_level": "Unknown",
            "severity": "info",
            "warning": False,
            "summary": None,
            "chains": [],
        }

    total_supply = round(sum(c["supply"] for c in chains), 2)
    chain_count = len(chains)
    top = chains[0]
    top_chain = top["chain"]
    top_pct = top["supply_pct"] if top["supply_pct"] is not None else 0.0
    level, severity = _classify(top_pct, chain_count)
    return {
        "total_supply": total_supply,
        "chain_count": chain_count,
        "top_chain": top_chain,
        "top_chain_pct": top_pct,
        "hhi": _hhi(chains),
        "concentration_level": level,
        "severity": severity,
        "warning": severity == "high",
        "summary": _summary(symbol, top_chain, top_pct, chain_count, severity),
        "chains": chains,
    }


def _latest_dominant_supply_row(session, symbol: str) -> SupplySnapshot | None:
    """Latest supply snapshot for ``symbol``, collapsing ticker collisions.

    Several DefiLlama assets can share a ticker and land at the same timestamp,
    so pick the dominant (largest) row at the newest timestamp — identical to
    services.profile / services.market_changes.
    """
    latest_ts = session.execute(
        select(func.max(SupplySnapshot.recorded_at)).where(SupplySnapshot.symbol == symbol)
    ).scalar_one_or_none()
    if latest_ts is None:
        return None
    return session.execute(
        select(SupplySnapshot)
        .where(SupplySnapshot.symbol == symbol, SupplySnapshot.recorded_at == latest_ts)
        .order_by(SupplySnapshot.circulating_supply.desc())
        .limit(1)
    ).scalars().first()


def get_chain_concentration(symbol: str) -> dict[str, Any] | None:
    """Chain concentration detail for one asset (case-insensitive).

    Returns ``None`` only when the symbol is completely unknown (no registry row
    and no supply snapshot), so the endpoint can 404 cleanly. When the asset is
    known but has no parseable chain breakdown, the concentration block is the
    "Unknown" shape (metrics ``None``) rather than guessed.
    """
    sym = symbol.upper()
    now = datetime.utcnow()

    with get_session() as session:
        meta = session.execute(
            select(Stablecoin).where(Stablecoin.symbol == sym)
        ).scalar_one_or_none()
        supply_row = _latest_dominant_supply_row(session, sym)

    if meta is None and supply_row is None:
        return None

    chains = _parse_chains(supply_row.supply_by_chain) if supply_row is not None else []
    block = _concentration_from_chains(sym, chains)

    return {
        "symbol": sym,
        "name": meta.name if meta else None,
        "registered": meta is not None,
        "recorded_at": supply_row.recorded_at.isoformat() if supply_row is not None else None,
        **block,
        "generated_at": now.isoformat(),
    }


def chain_concentration_ranking(limit: int | None = None) -> list[dict[str, Any]]:
    """Cross-asset ranking of chain concentration, most concentrated first.

    For every asset with a latest supply snapshot that has a parseable chain
    breakdown, computes the concentration block and ranks by severity, then
    top-chain share, then HHI. Assets with no chain breakdown are omitted (there
    is nothing to rank). Returns an empty list on a brand-new database.
    """
    with get_session() as session:
        # Latest recorded_at per symbol, then the rows at that timestamp (a
        # ticker collision can produce several) — collapsed to the dominant one.
        latest = (
            select(
                SupplySnapshot.symbol.label("symbol"),
                func.max(SupplySnapshot.recorded_at).label("mx"),
            )
            .group_by(SupplySnapshot.symbol)
            .subquery()
        )
        rows = session.execute(
            select(SupplySnapshot).join(
                latest,
                (SupplySnapshot.symbol == latest.c.symbol)
                & (SupplySnapshot.recorded_at == latest.c.mx),
            )
        ).scalars().all()

    # Collapse same-(symbol, timestamp) collisions to the dominant row.
    dominant: dict[str, SupplySnapshot] = {}
    for r in rows:
        existing = dominant.get(r.symbol)
        if existing is None or r.circulating_supply > existing.circulating_supply:
            dominant[r.symbol] = r

    ranking: list[dict[str, Any]] = []
    for sym, row in dominant.items():
        chains = _parse_chains(row.supply_by_chain)
        if not chains:
            continue  # nothing to rank without a chain breakdown
        block = _concentration_from_chains(sym, chains)
        ranking.append({
            "asset": sym,
            "total_supply": block["total_supply"],
            "chain_count": block["chain_count"],
            "top_chain": block["top_chain"],
            "top_chain_pct": block["top_chain_pct"],
            "hhi": block["hhi"],
            "concentration_level": block["concentration_level"],
            "severity": block["severity"],
            "warning": block["warning"],
            "summary": block["summary"],
            "recorded_at": row.recorded_at.isoformat(),
        })

    ranking.sort(
        key=lambda c: (
            SEVERITY_RANK[c["severity"]],
            c["top_chain_pct"] or 0.0,
            c["hhi"] or 0.0,
        ),
        reverse=True,
    )
    if limit is not None:
        ranking = ranking[:limit]
    return ranking
