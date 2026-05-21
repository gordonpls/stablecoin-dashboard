"""Pipeline: pull stablecoin supply from DefiLlama and upsert to DB."""

import asyncio
import json
import logging
from datetime import datetime

from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from db.models import Stablecoin, SupplySnapshot, get_session, init_db
from ingestion.defillama import get_stablecoins, parse_supply

logger = logging.getLogger(__name__)


async def _fetch_and_store() -> int:
    assets = await get_stablecoins()
    now = datetime.utcnow()
    snapshots: list[SupplySnapshot] = []

    with get_session() as session:
        for asset in assets:
            parsed = parse_supply(asset)
            if not parsed.get("symbol") or not parsed.get("id"):
                continue

            stmt = sqlite_insert(Stablecoin).values(
                id=parsed["id"],
                symbol=parsed["symbol"],
                name=parsed["name"],
                issuer=parsed["issuer"],
                updated_at=now,
            ).on_conflict_do_update(
                index_elements=["symbol"],
                set_={"name": parsed["name"], "issuer": parsed["issuer"], "updated_at": now},
            )
            session.execute(stmt)

            if parsed.get("circulating_supply"):
                snapshots.append(SupplySnapshot(
                    symbol=parsed["symbol"],
                    circulating_supply=parsed["circulating_supply"],
                    supply_by_chain=json.dumps(asset.get("chainCirculating") or {}),
                    recorded_at=now,
                ))

        session.add_all(snapshots)
        session.commit()

    logger.info("supply_updated assets=%d snapshots=%d", len(assets), len(snapshots))
    return len(snapshots)


def run() -> None:
    init_db()
    from services.pipeline_runs import record_run

    with record_run("update_supply") as rec:
        rec.rows_written = asyncio.run(_fetch_and_store())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
