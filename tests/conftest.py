"""Shared fixtures for the MediaStack test suite."""

import json
import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    """Load a recorded JSON fixture by filename."""
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def fixture():
    return load_fixture


# -- Tier 3: real PostgreSQL --

@pytest.fixture(scope="session")
def db_url():
    """Connection URL for a throwaway PostgreSQL.

    Priority: TEST_DATABASE_URL env var (CI services: block), then a
    testcontainers-managed instance. Skips tier-3 tests if neither works.
    """
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield url
        return

    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        pytest.skip("TEST_DATABASE_URL not set and testcontainers not installed")
        return

    try:
        with PostgresContainer("postgres:16-alpine") as pg:
            yield pg.get_connection_url().replace("+psycopg2", "")
    except Exception as e:
        pytest.skip(f"Could not start throwaway PostgreSQL: {e}")


@pytest.fixture
def clean_confirmation_store():
    """Empty the singleton confirmation store before and after a test."""
    from app.confirmations import store
    with store._lock:
        store._pending.clear()
    yield store
    with store._lock:
        store._pending.clear()
