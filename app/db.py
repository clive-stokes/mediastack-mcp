"""PostgreSQL database layer for MediaStack."""

import json
import logging
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

_db_url: str = ""


def init(db_url: str) -> None:
    """Store the DB URL and create schema."""
    global _db_url
    _db_url = db_url
    _create_schema()


def _conn():
    return psycopg2.connect(_db_url)


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
    """Bulk insert events. Returns count of new (non-duplicate) events."""
    count = 0
    for event in events:
        if insert_event(event):
            count += 1
    return count


def get_timeline(hours: int = 24, source: str | None = None,
                 event_type: str | None = None, limit: int = 100) -> list[dict]:
    """Fetch recent events for the timeline tool."""
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            conditions = ["timestamp >= NOW() - INTERVAL '%s hours'"]
            params: list = [hours]
            if source:
                conditions.append("source = %s")
                params.append(source)
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
                "timestamp >= NOW() - INTERVAL '%s days'",
                "title ILIKE %s",
            ]
            params: list = [days, f"%{query}%"]
            if source:
                conditions.append("source = %s")
                params.append(source)
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
