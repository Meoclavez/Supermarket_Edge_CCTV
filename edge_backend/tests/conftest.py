"""Test configuration.

The suite previously wrote its fixtures to ``settings.DATABASE_PATH`` -- the
same SQLite file the running application uses. That is how a ``cam_living_room``
camera and five ``FALL_DETECTED`` events ended up in the production database and
were later indistinguishable from real records.

Every test run now gets its own throwaway database.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Must be set before app.config is imported anywhere, because Settings resolves
# these paths at class-definition time.
_TMP = Path(tempfile.mkdtemp(prefix="cctv_test_"))
os.environ.setdefault("STORAGE_DIR", str(_TMP))
os.environ.setdefault("DATABASE_PATH", str(_TMP / "test.db"))
os.environ.setdefault("SQLITE_DB_PATH", str(_TMP / "test.db"))
os.environ.setdefault("DEBUG", "true")


@pytest.fixture(scope="session")
def test_storage_dir() -> Path:
    return _TMP


@pytest.fixture(autouse=True)
def _guard_production_db():
    """Fail loudly if a test is about to touch the real database file."""
    from app.config import settings

    real = Path(__file__).resolve().parent.parent.parent / "storage" / "cctv_core.db"
    assert Path(settings.DATABASE_PATH).resolve() != real.resolve(), (
        "Tests are pointed at the production database. Check the environment "
        "overrides in conftest.py."
    )
    yield
