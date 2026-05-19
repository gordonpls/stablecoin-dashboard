"""Shared test fixtures."""

import pytest
from unittest.mock import AsyncMock, patch

from db.models import init_db, Base
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    """Replace the real DB engine with a fresh in-memory SQLite for each test."""
    import db.models as models_mod

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        future=True,
    )
    monkeypatch.setattr(models_mod, "engine", test_engine)
    Base.metadata.create_all(test_engine)
    yield test_engine
    Base.metadata.drop_all(test_engine)


@pytest.fixture
def mock_tracked_get():
    """Patch tracked_get so ingestion modules never make real HTTP calls."""
    with patch("core.http.tracked_get", new_callable=AsyncMock) as m:
        yield m
