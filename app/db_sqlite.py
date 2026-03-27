"""SQLite database layer for MediaStack.

This module is loaded by db.py when DB_ENGINE=sqlite.
Provides the same public API as db_postgres.py using SQLite-compatible SQL.
"""

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_db_path: str = ""
_local = threading.local()


def init(db_url: str) -> None:
    """Store the DB path and create schema."""
    global _db_path
    _db_path = db_url
    # Ensure parent directory exists
    parent = os.path.dirname(_db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    _create_schema()


def _conn() -> sqlite3.Connection:
    """Get a thread-local SQLite connection with WAL mode and dict rows."""
    if not hasattr(_local, "conn") or _local.conn is None:
        conn = sqlite3.connect(_db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return _local.conn


def _dict_rows(cursor: sqlite3.Cursor) -> list[dict]:
    """Convert sqlite3.Row results to list of dicts."""
    return [dict(row) for row in cursor.fetchall()]


def _create_schema() -> None:
    """Create tables if they don't exist."""
    conn = _conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS media_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       DATETIME NOT NULL DEFAULT (datetime('now')),
            source          TEXT NOT NULL,
            event_type      TEXT NOT NULL,
            title           TEXT,
            metadata        TEXT DEFAULT '{}',
            source_event_id TEXT,
            created_at      DATETIME NOT NULL DEFAULT (datetime('now'))
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_media_events_source_event_id
            ON media_events (source_event_id) WHERE source_event_id IS NOT NULL;

        CREATE INDEX IF NOT EXISTS idx_media_events_source_ts
            ON media_events (source, timestamp DESC);

        CREATE INDEX IF NOT EXISTS idx_media_events_type
            ON media_events (event_type);

        CREATE TABLE IF NOT EXISTS storage_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   DATETIME NOT NULL DEFAULT (datetime('now')),
            mount_point TEXT NOT NULL,
            total_bytes INTEGER NOT NULL,
            used_bytes  INTEGER NOT NULL,
            source      TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_storage_mount_ts
            ON storage_snapshots (mount_point, timestamp DESC);

        CREATE TABLE IF NOT EXISTS library_snapshots (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp    DATETIME NOT NULL DEFAULT (datetime('now')),
            source       TEXT NOT NULL,
            library_name TEXT NOT NULL,
            item_count   INTEGER NOT NULL DEFAULT 0,
            size_bytes   INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_library_source_name_ts
            ON library_snapshots (source, library_name, timestamp DESC);

        CREATE TABLE IF NOT EXISTS service_health (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME NOT NULL DEFAULT (datetime('now')),
            service   TEXT NOT NULL,
            status    TEXT NOT NULL,
            detail    TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_health_service_ts
            ON service_health (service, timestamp DESC);
    """)
    conn.commit()
    logger.info("Database schema ready (SQLite).")


# -- Event operations --

def insert_event(event: dict) -> bool:
    """Insert a media event. Returns False if duplicate (source_event_id already exists)."""
    conn = _conn()
    ts = event.get("timestamp")
    if isinstance(ts, datetime):
        ts = ts.isoformat()
    elif ts is None:
        ts = datetime.now(timezone.utc).isoformat()

    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO media_events
               (timestamp, source, event_type, title, metadata, source_event_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                ts,
                event["source"],
                event["event_type"],
                event.get("title"),
                json.dumps(event.get("metadata", {})),
                event.get("source_event_id"),
            ),
        )
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        conn.rollback()
        raise


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
    conditions = [f"timestamp >= datetime('now', '-{int(hours)} hours')"]
    params: list = []
    if source:
        conditions.append(
            "(source = ? OR (source = 'mediastack'"
            " AND json_extract(metadata, '$.action_preview.service') = ?))"
        )
        params.extend([source, source])
    if event_type:
        conditions.append("event_type = ?")
        params.append(event_type)
    params.append(limit)
    where = " AND ".join(conditions)
    cur = conn.execute(
        f"SELECT id, timestamp, source, event_type, title, metadata "
        f"FROM media_events WHERE {where} "
        f"ORDER BY timestamp DESC LIMIT ?",
        params,
    )
    rows = _dict_rows(cur)
    for row in rows:
        # Parse metadata from JSON string
        if isinstance(row.get("metadata"), str):
            try:
                row["metadata"] = json.loads(row["metadata"])
            except (json.JSONDecodeError, TypeError):
                pass
    return rows


def search_events(query: str, days: int = 30, source: str | None = None) -> list[dict]:
    """Search events by title text match."""
    conn = _conn()
    conditions = [
        f"timestamp >= datetime('now', '-{int(days)} days')",
        "title LIKE ?",
    ]
    params: list = [f"%{query}%"]
    if source:
        conditions.append(
            "(source = ? OR (source = 'mediastack'"
            " AND json_extract(metadata, '$.action_preview.service') = ?))"
        )
        params.extend([source, source])
    where = " AND ".join(conditions)
    cur = conn.execute(
        f"SELECT id, timestamp, source, event_type, title, metadata "
        f"FROM media_events WHERE {where} "
        f"ORDER BY timestamp DESC LIMIT 50",
        params,
    )
    rows = _dict_rows(cur)
    for row in rows:
        if isinstance(row.get("metadata"), str):
            try:
                row["metadata"] = json.loads(row["metadata"])
            except (json.JSONDecodeError, TypeError):
                pass
    return rows


# -- Library operations --

def insert_library_snapshot(source: str, library_name: str, item_count: int,
                            size_bytes: int | None = None) -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO library_snapshots (source, library_name, item_count, size_bytes) "
        "VALUES (?, ?, ?, ?)",
        (source, library_name, item_count, size_bytes),
    )
    conn.commit()


def get_libraries_current() -> list[dict]:
    """Get latest snapshot per source+library_name."""
    conn = _conn()
    cur = conn.execute("""
        SELECT source, library_name, item_count, size_bytes, timestamp
        FROM library_snapshots
        WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY source, library_name ORDER BY timestamp DESC
                ) as rn
                FROM library_snapshots
            ) WHERE rn = 1
        )
        ORDER BY source, library_name
    """)
    rows = _dict_rows(cur)
    result = []
    for r in rows:
        entry = {
            "source": r["source"],
            "library_name": r["library_name"],
            "item_count": r["item_count"],
            "timestamp": r["timestamp"],
        }
        if r["size_bytes"] is not None:
            entry["size_bytes"] = r["size_bytes"]
        result.append(entry)
    return result


def get_library_delta(source: str, library_name: str) -> dict | None:
    """Get change since previous snapshot for a specific library."""
    conn = _conn()
    cur = conn.execute("""
        SELECT item_count, size_bytes, timestamp
        FROM library_snapshots
        WHERE source = ? AND library_name = ?
        ORDER BY timestamp DESC
        LIMIT 2
    """, (source, library_name))
    rows = _dict_rows(cur)
    if len(rows) < 2:
        return None
    current, previous = rows[0], rows[1]
    return {
        "item_delta": current["item_count"] - previous["item_count"],
        "size_delta": (current["size_bytes"] - previous["size_bytes"])
            if current["size_bytes"] is not None and previous["size_bytes"] is not None
            else None,
        "since": previous["timestamp"],
    }


def get_event_summary(hours: int = 24) -> list[dict]:
    """Get event counts grouped by source and event_type for the summary tool."""
    conn = _conn()
    cur = conn.execute(f"""
        SELECT source, event_type, count(*) as count,
               group_concat(DISTINCT title) as titles
        FROM media_events
        WHERE timestamp >= datetime('now', '-{int(hours)} hours')
          AND title IS NOT NULL
        GROUP BY source, event_type
        ORDER BY source, count DESC
    """)
    rows = _dict_rows(cur)
    # Convert titles from comma-separated string to list
    for row in rows:
        if row.get("titles"):
            row["titles"] = sorted(set(row["titles"].split(",")))
        else:
            row["titles"] = []
    return rows


# -- Storage operations --

def insert_storage_snapshot(mount_point: str, total_bytes: int, used_bytes: int, source: str) -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO storage_snapshots (mount_point, total_bytes, used_bytes, source) "
        "VALUES (?, ?, ?, ?)",
        (mount_point, total_bytes, used_bytes, source),
    )
    conn.commit()


def get_storage_current() -> list[dict]:
    """Get latest snapshot per mount point."""
    conn = _conn()
    cur = conn.execute("""
        SELECT mount_point, total_bytes, used_bytes, source, timestamp
        FROM storage_snapshots
        WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY mount_point ORDER BY timestamp DESC
                ) as rn
                FROM storage_snapshots
            ) WHERE rn = 1
        )
        ORDER BY mount_point
    """)
    rows = _dict_rows(cur)
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
            "timestamp": r["timestamp"],
        })
    return result


def get_storage_growth(mount_point: str, days: int = 7) -> dict | None:
    """Calculate storage growth rate over the given period."""
    conn = _conn()
    cur = conn.execute(f"""
        SELECT used_bytes, timestamp
        FROM storage_snapshots
        WHERE mount_point = ? AND timestamp >= datetime('now', '-{int(days)} days')
        ORDER BY timestamp ASC
        LIMIT 1
    """, (mount_point,))
    earliest = cur.fetchone()

    cur = conn.execute("""
        SELECT used_bytes, timestamp
        FROM storage_snapshots
        WHERE mount_point = ?
        ORDER BY timestamp DESC
        LIMIT 1
    """, (mount_point,))
    latest = cur.fetchone()

    if not earliest or not latest:
        return None

    earliest, latest = dict(earliest), dict(latest)
    delta_bytes = latest["used_bytes"] - earliest["used_bytes"]

    # Parse timestamps and compute delta
    from datetime import datetime as dt
    try:
        t1 = dt.fromisoformat(earliest["timestamp"])
        t2 = dt.fromisoformat(latest["timestamp"])
        delta_secs = (t2 - t1).total_seconds()
    except (ValueError, TypeError):
        return None

    if delta_secs <= 0:
        return None

    bytes_per_day = delta_bytes / (delta_secs / 86400)
    return {
        "period_days": days,
        "delta_bytes": delta_bytes,
        "bytes_per_day": round(bytes_per_day),
    }


# -- Health operations --

def get_latest_health() -> list[dict]:
    """Get the most recent health status per service."""
    conn = _conn()
    cur = conn.execute("""
        SELECT service, status, detail, timestamp
        FROM service_health
        WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY service ORDER BY timestamp DESC
                ) as rn
                FROM service_health
            ) WHERE rn = 1
        )
        ORDER BY service
    """)
    return _dict_rows(cur)


def upsert_health(service: str, status: str, detail: str | None = None) -> None:
    """Record a health state change (only inserts if state actually changed)."""
    conn = _conn()
    cur = conn.execute("""
        SELECT status, detail FROM service_health
        WHERE service = ? ORDER BY timestamp DESC LIMIT 1
    """, (service,))
    last = cur.fetchone()
    if last and dict(last)["status"] == status and dict(last)["detail"] == detail:
        return
    conn.execute(
        "INSERT INTO service_health (service, status, detail) VALUES (?, ?, ?)",
        (service, status, detail),
    )
    conn.commit()


# -- Stats --

def _human_size(size_bytes: int) -> str:
    """Format bytes as human-readable string."""
    for unit in ("bytes", "kB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}" if unit != "bytes" else f"{size_bytes} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def get_stats() -> dict:
    conn = _conn()
    cur = conn.execute("""
        SELECT
            (SELECT count(*) FROM media_events) AS total_events,
            (SELECT count(DISTINCT source) FROM media_events) AS active_sources,
            (SELECT max(created_at) FROM media_events) AS last_event
    """)
    row = dict(cur.fetchone())
    # Database size from file
    try:
        db_size = os.path.getsize(_db_path)
        row["db_size"] = _human_size(db_size)
    except OSError:
        row["db_size"] = "unknown"
    return row
