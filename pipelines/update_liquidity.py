"""Pipeline: update order book depth / liquidity metrics from exchanges."""

import logging
from datetime import datetime

from db.models import PriceSnapshot, get_session, init_db
from ingestion.exchanges import get_peg_prices

logger = logging.getLogger(__name__)


def run(symbols: list[str] | None = None) -> None:
    """Re-uses the price pipeline but focuses on depth columns."""
    init_db()
    prices = get_peg_prices(symbols)
    now = datetime.utcnow()
    rows = []
    for symbol, data in prices.items():
        bid = data.get("bid_depth_usd")
        ask = data.get("ask_depth_usd")
        if bid is None and ask is None:
            logger.warning("no_depth_data symbol=%s", symbol)
            continue
        rows.append(PriceSnapshot(
            symbol=symbol,
            price=data.get("price", 1.0),
            peg_deviation_bps=data.get("peg_deviation_bps"),
            bid_depth_usd=bid,
            ask_depth_usd=ask,
            source="exchanges_depth",
            recorded_at=now,
        ))
    with get_session() as session:
        session.add_all(rows)
        session.commit()
    logger.info("liquidity_updated count=%d", len(rows))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
