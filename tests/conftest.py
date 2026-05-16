"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from homeagent.db import connect


@pytest.fixture
def tmp_db(tmp_path: Path):
    """A fresh SQLite DB in a temp dir, schema initialised on first connect."""
    db_path = tmp_path / "test.db"
    with connect(db_path) as conn:
        yield conn


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"
