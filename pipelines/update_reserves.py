"""Pipeline: ingest reserve report metadata.

Reserve attestation URLs are maintained as a static config here until
a live data source is identified. Report dates are updated manually or
by scraping issuer transparency pages.
"""

import json
import logging
from datetime import date

from db.models import ReserveReport, get_session, init_db

logger = logging.getLogger(__name__)

KNOWN_REPORTS: list[dict] = [
    {
        "symbol": "USDT",
        "report_url": "https://tether.to/en/transparency/",
        "report_date": "2025-04-01",
        "auditor": "BDO",
        "composition": json.dumps({"US_Treasuries": 0.84, "cash": 0.04, "other": 0.12}),
    },
    {
        "symbol": "USDC",
        "report_url": "https://www.circle.com/en/transparency",
        "report_date": "2025-04-01",
        "auditor": "Deloitte",
        "composition": json.dumps({"US_Treasuries": 0.95, "cash": 0.05}),
    },
    {
        "symbol": "DAI",
        "report_url": "https://daistats.com/",
        "report_date": None,
        "auditor": None,
        "composition": json.dumps({"USDC": 0.30, "ETH": 0.25, "RWA": 0.45}),
    },
]


def run() -> None:
    init_db()
    with get_session() as session:
        for r in KNOWN_REPORTS:
            report_date = date.fromisoformat(r["report_date"]) if r.get("report_date") else None
            session.add(ReserveReport(
                symbol=r["symbol"],
                report_url=r.get("report_url"),
                report_date=report_date,
                composition=r.get("composition"),
                auditor=r.get("auditor"),
            ))
        session.commit()
    logger.info("reserves_updated count=%d", len(KNOWN_REPORTS))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
