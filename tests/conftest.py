"""Shared pytest fixtures.

Offline tests need only data/icd10.db (build it with scripts/build_db.py).
Tests marked ``live`` call the Anthropic API and are skipped unless
ICD_CODER_LIVE_TESTS=1 is set -- they cost money and need a key in .env.
"""
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from search_engine.core import DB_PATH  # noqa: E402


def pytest_collection_modifyitems(config, items):
    if os.environ.get("ICD_CODER_LIVE_TESTS") == "1":
        return
    skip_live = pytest.mark.skip(reason="live model test; set ICD_CODER_LIVE_TESTS=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(scope="session")
def db_path() -> Path:
    if not DB_PATH.exists():
        pytest.fail(f"{DB_PATH} is missing -- run `python scripts/build_db.py` first")
    return DB_PATH


@pytest.fixture(scope="session")
def conn(db_path):
    c = sqlite3.connect(db_path)
    yield c
    c.close()
