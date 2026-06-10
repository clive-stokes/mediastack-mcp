"""Tier 4 — live smoke test against the production deployment.

Excluded from CI (pytest.ini deselects the live marker by default).
Run manually on the NAS with the production .env loaded:

    pytest -m live

Read-only: hits /health and the db-backed read tools. Never touches
write or confirm tools — tier 2 owns that path against fixtures.
"""

import json
import os

import httpx
import pytest

from app import db, server
from app.config import Config

pytestmark = pytest.mark.live

BASE_URL = os.environ.get("MEDIASTACK_LIVE_URL", "http://127.0.0.1:9202")


@pytest.fixture(scope="module")
def live_db():
    cfg = Config.from_env()
    if not os.environ.get("DB_MEDIASTACK_PASS") and not os.environ.get("MEDIASTACK_DB_URL"):
        pytest.skip("Production database environment not loaded")
    db.init(cfg.db_url)


def test_health_endpoint_returns_ok_with_build():
    resp = httpx.get(f"{BASE_URL}/health", timeout=10)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["build"] == server.MEDIASTACK_BUILD


def test_mediastack_health_returns_parseable_json(live_db):
    result = json.loads(server.mediastack_health())
    assert isinstance(result, (list, dict))


def test_mediastack_stats_returns_parseable_json(live_db):
    result = json.loads(server.mediastack_stats())
    assert result["total_events"] >= 0


def test_mediastack_timeline_returns_parseable_json(live_db):
    raw = server.mediastack_timeline(hours=1)
    # "No events" message is acceptable; JSON must parse when events exist
    if raw.startswith("No events"):
        return
    events = json.loads(raw)
    assert isinstance(events, list)
