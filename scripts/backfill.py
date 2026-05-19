"""Backfill historical supply snapshots from DefiLlama chart data."""

import argparse
import json
import logging
from datetime import datetime

from db.models import Stablecoin, SupplySnapshot, get_session, init_db
from ingestion.defillama import get_stablecoins, get_stablecoin_charts

logger = logging.getLogger(__name__)


def backfill_supply(symbol: str | None = None) -> None:
    init_db()
    assets = get_stablecoins()

    if symbol:
        assets = [a for a in assets if a.get("symbol", "").upper() == symbol.upper()]

    with get_session() as session:
        for asset in assets:
            sym = asset.get("symbol")
            asset_id = asset.get("id")
            if not sym or not asset_id:
                continue

            logger.info("backfilling", symbol=sym)
            try:
                charts = get_stablecoin_charts(asset_id)
            except Exception as e:
                logger.warning("chart_fetch_failed", symbol=sym, error=str(e))
                continue

            entries = charts if isinstance(charts, list) else charts.get("totalCirculating", [])
            rows = []
            for entry in entries:
                ts = entry.get("date")
                value = entry.get("totalCirculating") or entry.get("peggedUSD")
                if not ts or value is None:
                    continue
                rows.append(SupplySnapshot(
                    symbol=sym,
                    circulating_supply=float(value),
                    supply_by_chain=None,
                    recorded_at=datetime.utcfromtimestamp(int(ts)),
                ))

            session.add_all(rows)
            session.commit()
            logger.info("backfilled", symbol=sym, rows=len(rows))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Backfill historical supply data from DefiLlama")
    parser.add_argument("--symbol", help="Limit to a specific symbol (e.g. USDT)")
    args = parser.parse_args()
    backfill_supply(symbol=args.symbol)
