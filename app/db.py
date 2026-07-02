"""PostgreSQL database layer for MediaStack.

All functions here are synchronous (psycopg2). Callers running inside an
asyncio event loop must wrap them in asyncio.to_thread(...) — see poller.py.
Connections come from a small ThreadedConnectionPool rather than a fresh
connect() per call.
"""

import json
import logging
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import psycopg2.pool

logger = logging.getLogger(__name__)

_db_url: str = ""
_pool: psycopg2.pool.ThreadedConnectionPool | None = None

POOL_MIN = 1
POOL_MAX = 5


def init(db_url: str) -> None:
    """Store the DB URL, build the connection pool, and create schema."""
    global _db_url, _pool
    _db_url = db_url
    if _pool is not None:
        try:
            _pool.closeall()
        except Exception:
            pass
    _pool = psycopg2.pool.ThreadedConnectionPool(POOL_MIN, POOL_MAX, db_url)
    _create_schema()


class _PooledConnection:
    """Borrowed pool connection — close() returns it to the pool.

    Keeps the existing `conn = _conn(); try: ... finally: conn.close()`
    call pattern working unchanged across db.py, retention.py, and server.py.
    """

    def __init__(self, pool: psycopg2.pool.ThreadedConnectionPool, conn):
        self._pool = pool
        self._raw = conn

    def cursor(self, *args, **kwargs):
        return self._raw.cursor(*args, **kwargs)

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        try:
            # Discard any uncommitted/failed transaction so the next borrower
            # gets a clean connection. No-op after a commit.
            if not self._raw.closed:
                self._raw.rollback()
        except Exception:
            pass
        self._pool.putconn(self._raw, close=self._raw.closed)


def _conn() -> _PooledConnection:
    if _pool is None:
        raise RuntimeError("db.init() has not been called")
    conn = _pool.getconn()
    if conn.closed:
        # Server-side drop (e.g. Postgres restart) — replace the dead one
        _pool.putconn(conn, close=True)
        conn = _pool.getconn()
    return _PooledConnection(_pool, conn)


def _create_schema() -> None:
    """Create tables if they don't exist."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS media_events (
                    id              BIGSERIAL PRIMARY KEY,
                    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    source          VARCHAR(50) NOT NULL,
                    event_type      VARCHAR(50) NOT NULL,
                    title           TEXT,
                    metadata        JSONB DEFAULT '{}'::jsonb,
                    source_event_id VARCHAR(200),
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_media_events_source_event_id
                    ON media_events (source_event_id) WHERE source_event_id IS NOT NULL;

                CREATE INDEX IF NOT EXISTS idx_media_events_source_ts
                    ON media_events (source, timestamp DESC);

                CREATE INDEX IF NOT EXISTS idx_media_events_type
                    ON media_events (event_type);

                CREATE TABLE IF NOT EXISTS storage_snapshots (
                    id          BIGSERIAL PRIMARY KEY,
                    timestamp   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    mount_point VARCHAR(500) NOT NULL,
                    total_bytes BIGINT NOT NULL,
                    used_bytes  BIGINT NOT NULL,
                    source      VARCHAR(50) NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_storage_mount_ts
                    ON storage_snapshots (mount_point, timestamp DESC);

                -- Dedupe daily rollups left behind while the retention job's
                -- ON CONFLICT DO NOTHING had no matching constraint, then
                -- enforce uniqueness so the dedup actually works.
                DELETE FROM storage_snapshots a
                    USING storage_snapshots b
                    WHERE a.id > b.id
                      AND a.source = 'daily_rollup' AND b.source = 'daily_rollup'
                      AND a.timestamp = b.timestamp
                      AND a.mount_point = b.mount_point;

                CREATE UNIQUE INDEX IF NOT EXISTS uq_storage_rollup
                    ON storage_snapshots (timestamp, mount_point)
                    WHERE source = 'daily_rollup';

                CREATE TABLE IF NOT EXISTS library_snapshots (
                    id           BIGSERIAL PRIMARY KEY,
                    timestamp    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    source       VARCHAR(50) NOT NULL,
                    library_name VARCHAR(200) NOT NULL,
                    item_count   INT NOT NULL DEFAULT 0,
                    size_bytes   BIGINT
                );

                CREATE INDEX IF NOT EXISTS idx_library_source_name_ts
                    ON library_snapshots (source, library_name, timestamp DESC);

                CREATE TABLE IF NOT EXISTS service_health (
                    id        BIGSERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    service   VARCHAR(50) NOT NULL,
                    status    VARCHAR(20) NOT NULL,
                    detail    TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_health_service_ts
                    ON service_health (service, timestamp DESC);
            """)
        conn.commit()
        logger.info("Database schema ready.")
    finally:
        conn.close()


# -- Event operations --

def insert_event(event: dict) -> bool:
    """Insert a media event. Returns False if duplicate (source_event_id already exists)."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO media_events (timestamp, source, event_type, title, metadata, source_event_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_event_id) WHERE source_event_id IS NOT NULL DO NOTHING
                RETURNING id
                """,
                (
                    event.get("timestamp") or datetime.now(timezone.utc),
                    event["source"],
                    event["event_type"],
                    event.get("title"),
                    json.dumps(event.get("metadata", {})),
                    event.get("source_event_id"),
                ),
            )
            inserted = cur.fetchone() is not None
        conn.commit()
        return inserted
    finally:
        conn.close()


def insert_events(events: list[dict]) -> int:
    """Bulk insert events. Returns count of new (non-duplicate) events.

    Uses a single connection for the batch — more efficient for backfills
    of ~100 items than opening a connection per event.
    """
    conn = _conn()
    count = 0
    try:
        with conn.cursor() as cur:
            for event in events:
                cur.execute(
                    """
                    INSERT INTO media_events (timestamp, source, event_type, title, metadata, source_event_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source_event_id) WHERE source_event_id IS NOT NULL DO NOTHING
                    RETURNING id
                    """,
                    (
                        event.get("timestamp") or datetime.now(timezone.utc),
                        event["source"],
                        event["event_type"],
                        event.get("title"),
                        json.dumps(event.get("metadata", {})),
                        event.get("source_event_id"),
                    ),
                )
                if cur.fetchone() is not None:
                    count += 1
        conn.commit()
        return count
    finally:
        conn.close()


def get_timeline(hours: int = 24, source: str | None = None,
                 event_type: str | None = None, limit: int = 100) -> list[dict]:
    """Fetch recent events for the timeline tool."""
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = ["timestamp >= NOW() - make_interval(hours => %s)"]
            params: list = [hours]
            if source:
                conditions.append(
                    "(source = %s OR (source = 'mediastack'"
                    " AND metadata->'action_preview'->>'service' = %s))"
                )
                params.extend([source, source])
            if event_type:
                conditions.append("event_type = %s")
                params.append(event_type)
            params.append(limit)
            where = " AND ".join(conditions)
            cur.execute(
                f"SELECT id, timestamp, source, event_type, title, metadata "
                f"FROM media_events WHERE {where} "
                f"ORDER BY timestamp DESC LIMIT %s",
                params,
            )
            rows = cur.fetchall()
            for row in rows:
                row["timestamp"] = row["timestamp"].isoformat()
            return [dict(r) for r in rows]
    finally:
        conn.close()


def search_events(query: str, days: int = 30, source: str | None = None) -> list[dict]:
    """Search events by title text match."""
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = [
                "timestamp >= NOW() - make_interval(days => %s)",
                "title ILIKE %s",
            ]
            params: list = [days, f"%{query}%"]
            if source:
                conditions.append(
                    "(source = %s OR (source = 'mediastack'"
                    " AND metadata->'action_preview'->>'service' = %s))"
                )
                params.extend([source, source])
            where = " AND ".join(conditions)
            cur.execute(
                f"SELECT id, timestamp, source, event_type, title, metadata "
                f"FROM media_events WHERE {where} "
                f"ORDER BY timestamp DESC LIMIT 50",
                params,
            )
            rows = cur.fetchall()
            for row in rows:
                row["timestamp"] = row["timestamp"].isoformat()
            return [dict(r) for r in rows]
    finally:
        conn.close()


def get_events_filtered(
    source: str,
    days: int = 365,
    event_type: str | None = None,
    metadata_filters: dict | None = None,
    limit: int = 500,
) -> list[dict]:
    """Fetch events with source, type, and JSONB metadata filters.

    Args:
        source: Required source name (e.g. "get_iplayer").
        days: Look-back period in days.
        event_type: Optional event type filter.
        metadata_filters: Dict of metadata key->value pairs to match
                          (e.g. {"type": "tv"}).
        limit: Maximum rows to return.
    """
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = [
                "source = %s",
                "timestamp >= NOW() - make_interval(days => %s)",
            ]
            params: list = [source, days]

            if event_type:
                conditions.append("event_type = %s")
                params.append(event_type)

            if metadata_filters:
                for key, value in metadata_filters.items():
                    conditions.append("metadata->>%s = %s")
                    params.extend([key, value])

            params.append(limit)
            where = " AND ".join(conditions)
            cur.execute(
                f"SELECT id, timestamp, source, event_type, title, metadata "
                f"FROM media_events WHERE {where} "
                f"ORDER BY timestamp DESC LIMIT %s",
                params,
            )
            rows = cur.fetchall()
            for row in rows:
                row["timestamp"] = row["timestamp"].isoformat()
            return [dict(r) for r in rows]
    finally:
        conn.close()


def get_seerr_unavailable_events(days: int, media_type: str | None = None) -> list[dict]:
    """Fetch seerr_media_unavailable events for the deletion audit.

    Returns rows with raw datetime timestamps (the caller formats them).
    """
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = [
                "event_type = 'seerr_media_unavailable'",
                "timestamp >= NOW() - make_interval(days => %s)",
            ]
            params: list = [days]
            if media_type:
                conditions.append("metadata->>'media_type' = %s")
                params.append(media_type)
            where = " AND ".join(conditions)
            cur.execute(
                f"SELECT id, timestamp, title, metadata FROM media_events "
                f"WHERE {where} ORDER BY timestamp DESC",
                params,
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# -- Library operations --

def insert_library_snapshot(source: str, library_name: str, item_count: int,
                            size_bytes: int | None = None) -> None:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO library_snapshots (source, library_name, item_count, size_bytes) "
                "VALUES (%s, %s, %s, %s)",
                (source, library_name, item_count, size_bytes),
            )
        conn.commit()
    finally:
        conn.close()


def get_libraries_current() -> list[dict]:
    """Get latest snapshot per source+library_name."""
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT DISTINCT ON (source, library_name)
                    source, library_name, item_count, size_bytes, timestamp
                FROM library_snapshots
                ORDER BY source, library_name, timestamp DESC
            """)
            rows = cur.fetchall()
            result = []
            for r in rows:
                entry = {
                    "source": r["source"],
                    "library_name": r["library_name"],
                    "item_count": r["item_count"],
                    "timestamp": r["timestamp"].isoformat(),
                }
                if r["size_bytes"] is not None:
                    entry["size_bytes"] = r["size_bytes"]
                result.append(entry)
            return result
    finally:
        conn.close()


def get_library_delta(source: str, library_name: str) -> dict | None:
    """Get change since previous snapshot for a specific library."""
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT item_count, size_bytes, timestamp
                FROM library_snapshots
                WHERE source = %s AND library_name = %s
                ORDER BY timestamp DESC
                LIMIT 2
            """, (source, library_name))
            rows = cur.fetchall()
            if len(rows) < 2:
                return None
            current, previous = rows[0], rows[1]
            return {
                "item_delta": current["item_count"] - previous["item_count"],
                "size_delta": (current["size_bytes"] - previous["size_bytes"])
                    if current["size_bytes"] is not None and previous["size_bytes"] is not None
                    else None,
                "since": previous["timestamp"].isoformat(),
            }
    finally:
        conn.close()


def get_event_summary(hours: int = 24) -> list[dict]:
    """Get event counts grouped by source and event_type for the summary tool."""
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT source, event_type, count(*) as count,
                       array_agg(DISTINCT title ORDER BY title) FILTER (WHERE title IS NOT NULL) as titles
                FROM media_events
                WHERE timestamp >= NOW() - INTERVAL '%s hours'
                GROUP BY source, event_type
                ORDER BY source, count DESC
            """, (hours,))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# -- Storage operations --

def insert_storage_snapshot(mount_point: str, total_bytes: int, used_bytes: int, source: str) -> None:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO storage_snapshots (mount_point, total_bytes, used_bytes, source) "
                "VALUES (%s, %s, %s, %s)",
                (mount_point, total_bytes, used_bytes, source),
            )
        conn.commit()
    finally:
        conn.close()


def get_storage_current() -> list[dict]:
    """Get latest snapshot per mount point."""
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT DISTINCT ON (mount_point)
                    mount_point, total_bytes, used_bytes, source, timestamp
                FROM storage_snapshots
                ORDER BY mount_point, timestamp DESC
            """)
            rows = cur.fetchall()
            result = []
            for r in rows:
                total = r["total_bytes"]
                used = r["used_bytes"]
                free = total - used
                pct = (used / total * 100) if total > 0 else 0
                result.append({
                    "mount_point": r["mount_point"],
                    "total_bytes": total,
                    "used_bytes": used,
                    "free_bytes": free,
                    "percent_used": round(pct, 1),
                    "source": r["source"],
                    "timestamp": r["timestamp"].isoformat(),
                })
            return result
    finally:
        conn.close()


def get_storage_growth(mount_point: str, days: int = 7) -> dict | None:
    """Calculate storage growth rate over the given period."""
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Get earliest and latest snapshots in the window
            cur.execute("""
                SELECT used_bytes, timestamp
                FROM storage_snapshots
                WHERE mount_point = %s AND timestamp >= NOW() - INTERVAL '%s days'
                ORDER BY timestamp ASC
                LIMIT 1
            """, (mount_point, days))
            earliest = cur.fetchone()

            cur.execute("""
                SELECT used_bytes, timestamp
                FROM storage_snapshots
                WHERE mount_point = %s
                ORDER BY timestamp DESC
                LIMIT 1
            """, (mount_point,))
            latest = cur.fetchone()

            if not earliest or not latest:
                return None

            delta_bytes = latest["used_bytes"] - earliest["used_bytes"]
            delta_secs = (latest["timestamp"] - earliest["timestamp"]).total_seconds()
            if delta_secs <= 0:
                return None

            bytes_per_day = delta_bytes / (delta_secs / 86400)
            return {
                "period_days": days,
                "delta_bytes": delta_bytes,
                "bytes_per_day": round(bytes_per_day),
            }
    finally:
        conn.close()


# -- Health operations --

def get_latest_health() -> list[dict]:
    """Get the most recent health status per service."""
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT DISTINCT ON (service)
                    service, status, detail, timestamp
                FROM service_health
                ORDER BY service, timestamp DESC
            """)
            rows = cur.fetchall()
            for row in rows:
                row["timestamp"] = row["timestamp"].isoformat()
            return [dict(r) for r in rows]
    finally:
        conn.close()


def upsert_health(service: str, status: str, detail: str | None = None) -> None:
    """Record a health state change (only inserts if state actually changed)."""
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT status, detail FROM service_health
                WHERE service = %s ORDER BY timestamp DESC LIMIT 1
            """, (service,))
            last = cur.fetchone()
            # Only insert if state changed
            if last and last["status"] == status and last["detail"] == detail:
                return
            cur.execute(
                "INSERT INTO service_health (service, status, detail) VALUES (%s, %s, %s)",
                (service, status, detail),
            )
        conn.commit()
    finally:
        conn.close()


# -- Stats --

def get_stats() -> dict:
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    (SELECT count(*) FROM media_events) AS total_events,
                    (SELECT count(DISTINCT source) FROM media_events) AS active_sources,
                    (SELECT max(created_at) FROM media_events) AS last_event,
                    pg_size_pretty(pg_database_size(current_database())) AS db_size
            """)
            return dict(cur.fetchone())
    finally:
        conn.close()
