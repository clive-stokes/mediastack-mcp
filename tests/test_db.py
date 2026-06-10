"""Tier 3 — database layer against a real (throwaway) PostgreSQL.

Connection comes from the session-scoped db_url fixture: TEST_DATABASE_URL
(CI services: block) or a testcontainers instance. Skipped when neither is
available.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app import db
from app.retention import run_retention


@pytest.fixture(autouse=True)
def database(db_url):
    db.init(db_url)
    yield
    conn = db._conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE media_events, storage_snapshots, "
                "library_snapshots, service_health RESTART IDENTITY",
            )
        conn.commit()
    finally:
        conn.close()


def count_rows(table: str, where: str = "TRUE") -> int:
    conn = db._conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table} WHERE {where}")  # noqa: S608 — test helper, fixed inputs
            return cur.fetchone()[0]
    finally:
        conn.close()


def days_ago(n: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=n)


def make_event(i: int, *, event_type="grabbed", source="sonarr",
               timestamp=None, source_event_id=...):
    return {
        "source": source,
        "event_type": event_type,
        "title": f"Event {i}",
        "metadata": {"size_bytes": 1000},
        "timestamp": timestamp,
        "source_event_id": f"{source}_{i}" if source_event_id is ... else source_event_id,
    }


# -- Schema --

def test_schema_creation_idempotent(db_url):
    db.init(db_url)  # second init on top of the autouse one
    db.init(db_url)
    assert count_rows("media_events") == 0


# -- Dedup --

def test_same_source_event_id_inserted_once():
    assert db.insert_event(make_event(1)) is True
    assert db.insert_event(make_event(1)) is False
    assert count_rows("media_events") == 1


def test_insert_events_returns_new_row_count_not_batch_size():
    batch = [make_event(i) for i in range(10)]
    assert db.insert_events(batch) == 10
    # Re-poll: same batch plus two new events
    batch.extend(make_event(i) for i in (10, 11))
    assert db.insert_events(batch) == 2
    assert count_rows("media_events") == 12


def test_null_source_event_id_rows_never_collide():
    a = make_event(1, source_event_id=None)
    b = make_event(2, source_event_id=None)
    assert db.insert_event(a) is True
    assert db.insert_event(b) is True
    assert count_rows("media_events") == 2


# -- Retention rollup --

def seed_retention_data():
    """100 events >90 days old on one day: 60 grabbed + 40 imported.
    Plus 5 recent events that must survive untouched."""
    old_day = days_ago(100)
    events = [
        make_event(i, event_type="grabbed", timestamp=old_day)
        for i in range(60)
    ] + [
        make_event(100 + i, event_type="imported", timestamp=old_day)
        for i in range(40)
    ]
    assert db.insert_events(events) == 100

    recent = [make_event(500 + i, timestamp=days_ago(1)) for i in range(5)]
    assert db.insert_events(recent) == 5


def get_summaries() -> list[dict]:
    conn = db._conn()
    try:
        import psycopg2.extras
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT source, title, metadata FROM media_events "
                "WHERE event_type = 'daily_summary'",
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def test_retention_rollup_summarises_purges_and_spares_recent():
    seed_retention_data()

    results = run_retention()
    assert results["events"]["purged"] == 100

    summaries = get_summaries()
    assert len(summaries) == 1
    counts = summaries[0]["metadata"]["event_counts"]
    assert counts == {"grabbed": 60, "imported": 40}
    assert summaries[0]["metadata"]["total_size_bytes"] == 100 * 1000
    assert "100 sonarr events" in summaries[0]["title"]

    # Raw old rows purged, recent rows untouched
    assert count_rows("media_events", "event_type != 'daily_summary'") == 5


def test_retention_second_run_is_noop():
    seed_retention_data()
    run_retention()
    second = run_retention()

    # The summary row itself is >90 days old but protected; nothing new purged
    assert second["events"].get("purged", 0) == 0
    assert len(get_summaries()) == 1
    assert count_rows("media_events", "event_type != 'daily_summary'") == 5


# -- Storage growth --

def seed_storage(mount: str, start_used: int, bytes_per_day: int, days: int):
    conn = db._conn()
    try:
        with conn.cursor() as cur:
            for d in range(days, -1, -1):
                cur.execute(
                    "INSERT INTO storage_snapshots "
                    "(timestamp, mount_point, total_bytes, used_bytes, source) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (
                        days_ago(d),
                        mount,
                        100 * 1024**4,
                        start_used + (days - d) * bytes_per_day,
                        "test",
                    ),
                )
        conn.commit()
    finally:
        conn.close()


def test_storage_growth_slope_within_tolerance():
    one_gb = 1024**3
    seed_storage("/media", 50 * 1024**4, one_gb, days=7)

    growth = db.get_storage_growth("/media", days=7)
    assert growth is not None
    assert growth["delta_bytes"] == 7 * one_gb
    # Within 1% of 1 GB/day
    assert abs(growth["bytes_per_day"] - one_gb) < one_gb * 0.01


def test_storage_growth_returns_none_without_data():
    assert db.get_storage_growth("/nowhere", days=7) is None


# -- Pooled connection behaviour --

def test_failed_query_does_not_poison_pooled_connection():
    conn = db._conn()
    try:
        with pytest.raises(Exception):
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM does_not_exist")
    finally:
        conn.close()  # returns to pool; must roll back the failed txn

    # Next borrower gets a usable connection
    assert db.insert_event(make_event(1)) is True


def test_event_summary_roundtrip():
    db.insert_event(make_event(1, event_type="grabbed"))
    db.insert_event(make_event(2, event_type="grabbed"))
    db.insert_event(make_event(3, event_type="imported"))

    summary = db.get_event_summary(hours=24)
    by_type = {(r["source"], r["event_type"]): r["count"] for r in summary}
    assert by_type[("sonarr", "grabbed")] == 2
    assert by_type[("sonarr", "imported")] == 1


def test_timeline_metadata_roundtrip():
    db.insert_event(make_event(1))
    events = db.get_timeline(hours=1)
    assert len(events) == 1
    assert events[0]["title"] == "Event 1"
    metadata = events[0]["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    assert metadata["size_bytes"] == 1000
