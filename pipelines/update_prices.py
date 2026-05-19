"""Pipeline: fetch peg prices from exchanges and write price snapshots."""

import asyncio
import logging
from datetime import datetime

from db.models import PriceSnapshot, get_session, init_db
from ingestion.exchanges import get_peg_prices

logger = logging.getLogger(__name__)


async def _fetch_and_store(symbols: list[str] | None = None) -> int:
    prices = await get_peg_prices(symbols)
    now = datetime.utcnow()
    rows: list[PriceSnapshot] = []

    for symbol, data in prices.items():
        if data.get("price") is None:
            logger.warning("no_price symbol=%s", symbol)
            continue
        rows.append(PriceSnapshot(
            symbol=symbol,
            price=data["price"],
            peg_deviation_bps=data.get("peg_deviation_bps"),
            bid_depth_usd=data.get("bid_depth_usd"),
            ask_depth_usd=data.get("ask_depth_usd"),
            source="binance",
            recorded_at=now,
        ))

    with get_session() as session:
        session.add_all(rows)
        session.commit()

    logger.info("prices_updated count=%d", len(rows))
    return len(rows)


def run(symbols: list[str] | None = None) -> None:
    init_db()
    asyncio.run(_fetch_and_store(symbols))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
