"""SQLAlchemy ORM models + session factory."""

import os
from contextlib import contextmanager
from datetime import datetime
from typing import Generator

from sqlalchemy import create_engine, Column, String, Float, Integer, Text, DateTime, Date
from sqlalchemy.orm import DeclarativeBase, Session

def _default_db_url() -> str:
    # On Streamlit Cloud the repo root is a read-only mount; fall back to /tmp.
    candidate = os.path.join(os.path.dirname(__file__), "..", "stablecoin.db")
    candidate = os.path.abspath(candidate)
    try:
        # Test writeability by touching the file or its parent directory.
        parent = os.path.dirname(candidate)
        if os.access(parent, os.W_OK):
            return f"sqlite:///{candidate}"
    except Exception:
        pass
    import tempfile
    return f"sqlite:///{os.path.join(tempfile.gettempdir(), 'stablecoin.db')}"


DATABASE_URL = os.getenv("DATABASE_URL", _default_db_url())
engine = create_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False},
)


class Base(DeclarativeBase):
    pass


class Stablecoin(Base):
    __tablename__ = "stablecoins"

    id            = Column(String, primary_key=True)
    symbol        = Column(String, nullable=False, unique=True)
    name          = Column(String, nullable=False)
    issuer        = Column(String)
    peg_mechanism = Column(String)
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        return {c.key: getattr(self, c.key) for c in self.__table__.columns}


class SupplySnapshot(Base):
    __tablename__ = "supply_snapshots"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    symbol             = Column(String, nullable=False)
    circulating_supply = Column(Float, nullable=False)
    supply_by_chain    = Column(Text)  # JSON: {chain: usd_amount}
    recorded_at        = Column(DateTime, default=datetime.utcnow, nullable=False)

    def to_dict(self) -> dict:
        return {c.key: getattr(self, c.key) for c in self.__table__.columns}


class PriceSnapshot(Base):
    __tablename__ = "price_snapshots"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    symbol            = Column(String, nullable=False)
    price             = Column(Float, nullable=False)
    peg_deviation_bps = Column(Float)
    bid_depth_usd     = Column(Float)
    ask_depth_usd     = Column(Float)
    source            = Column(String, nullable=False)
    recorded_at       = Column(DateTime, default=datetime.utcnow, nullable=False)

    def to_dict(self) -> dict:
        return {c.key: getattr(self, c.key) for c in self.__table__.columns}


class ReserveReport(Base):
    __tablename__ = "reserve_reports"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    symbol      = Column(String, nullable=False)
    report_url  = Column(String)
    report_date = Column(Date)
    composition = Column(Text)  # JSON: {asset: pct}
    auditor     = Column(String)
    ingested_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {c.key: getattr(self, c.key) for c in self.__table__.columns}


class RiskScore(Base):
    __tablename__ = "risk_scores"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    symbol          = Column(String, nullable=False)
    peg_score       = Column(Float, nullable=False)
    liquidity_score = Column(Float, nullable=False)
    reserve_score   = Column(Float, nullable=False)
    adoption_score  = Column(Float, nullable=False)
    overall_score   = Column(Float, nullable=False)
    scored_at       = Column(DateTime, default=datetime.utcnow, nullable=False)

    def to_dict(self) -> dict:
        return {c.key: getattr(self, c.key) for c in self.__table__.columns}


class RiskEvent(Base):
    """A notable, time-stamped risk change detected by the scheduled jobs.

    Rows are append-only and de-duplicated on (symbol, event_type, triggered_at,
    metric_name) so re-running detection over unchanged data is a no-op.
    System-level events (e.g. API_FAILURE) use ``symbol = "SYSTEM"``.
    """

    __tablename__ = "risk_events"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    symbol         = Column(String, nullable=False)
    event_type     = Column(String, nullable=False)   # PEG_DEVIATION, LIQUIDITY_DROP, ...
    severity       = Column(String, nullable=False)    # low | medium | high
    title          = Column(String, nullable=False)
    description    = Column(Text)
    metric_name    = Column(String)
    previous_value = Column(Float)
    current_value  = Column(Float)
    triggered_at   = Column(DateTime, nullable=False)

    def to_dict(self) -> dict:
        return {c.key: getattr(self, c.key) for c in self.__table__.columns}


class DataQualityWarning(Base):
    """A detected data-integrity problem with the stored metrics.

    Distinct from ``RiskEvent`` (which marks *market* moves like a peg break):
    these flag data that looks *wrong, implausible, or incomplete* — an
    out-of-band stablecoin price, non-positive supply, a peg_deviation_bps that
    is inconsistent with its price, an implausible supply jump, ticker-collision
    duplicate snapshots, or missing chain distribution.

    Warnings have a lifecycle: a row is opened (``resolved_at`` NULL) when a
    problem is first detected and closed (``resolved_at`` set) once the
    underlying data no longer trips the rule. Identity is
    (symbol, metric_name, warning_type) among the currently-open rows, so
    re-running detection over an unchanged problem is a no-op.
    """

    __tablename__ = "data_quality_warnings"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    symbol       = Column(String)                       # null for non-asset warnings
    provider     = Column(String)
    metric_name  = Column(String, nullable=False)
    warning_type = Column(String, nullable=False)        # IMPOSSIBLE_PRICE, ...
    severity     = Column(String, nullable=False)        # low | medium | high
    message      = Column(Text, nullable=False)
    detected_at  = Column(DateTime, nullable=False)
    resolved_at  = Column(DateTime)                      # null while the warning is active

    def to_dict(self) -> dict:
        return {c.key: getattr(self, c.key) for c in self.__table__.columns}


class ApiRequestLog(Base):
    __tablename__ = "api_request_log"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    provider     = Column(String, nullable=False)
    endpoint     = Column(String, nullable=False)
    url          = Column(String, nullable=False)
    status_code  = Column(Integer)
    raw_response = Column(Text)
    requested_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def to_dict(self) -> dict:
        return {c.key: getattr(self, c.key) for c in self.__table__.columns}


class PipelineRun(Base):
    """One execution of an ingestion/scoring pipeline.

    Rows are append-only and written by ``services.pipeline_runs.record_run``
    on every pipeline run (success or failure), so the dashboard and admins can
    see whether the data jobs are actually working and when each last succeeded.
    """

    __tablename__ = "pipeline_runs"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    pipeline_name    = Column(String, nullable=False)
    started_at       = Column(DateTime, nullable=False)
    finished_at      = Column(DateTime)
    status           = Column(String, nullable=False)   # success | error
    rows_written     = Column(Integer, default=0)
    error_message    = Column(Text)
    duration_seconds = Column(Float)

    def to_dict(self) -> dict:
        return {c.key: getattr(self, c.key) for c in self.__table__.columns}


@contextmanager
def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session


def init_db() -> None:
    Base.metadata.create_all(engine)


if __name__ == "__main__":
    init_db()
    print("Database initialized.")
