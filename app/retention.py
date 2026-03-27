"""Data retention and rollup for MediaStack.

Runs periodically to:
1. Aggregate old media_events into daily summaries, then purge raw rows (>90 days)
2. Roll up hourly storage_snapshots to daily averages (>14 days)
3. Library snapshots and service_health are kept indefinitely (small footprint)

Supports both PostgreSQL and SQLite engines via db.get_engine().
"""

import json
import logging

from app import db

logger = logging.getLogger(__name__)


def run_retention() -> dict:
    """Execute all retention tasks. Returns summary of actions taken."""
    results = {}
    engine = db.get_engine()
    if engine == "sqlite":
        results["events"] = _rollup_events_sqlite()
        results["storage"] = _rollup_storage_sqlite()
    else:
        results["events"] = _rollup_events_postgres()
        results["storage"] = _rollup_storage_postgres()
    return results


# -- PostgreSQL implementations (original) --

def _rollup_events_postgres() -> dict:
    """Aggregate events older than 90 days into daily summaries, then purge."""
    import psycopg2
    conn = db._conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT count(*) FROM media_events
                WHERE timestamp < NOW() - INTERVAL '90 days'
            """)
            old_count = cur.fetchone()[0]
            if old_count == 0:
                return {"purged": 0, "message": "No events older than 90 days"}

            cur.execute("""
                INSERT INTO media_events (timestamp, source, event_type, title, metadata, source_event_id)
                SELECT
                    date_trunc('day', timestamp) AS day,
                    source,
                    'daily_summary',
                    format('Daily summary: %s %s events', count(*), source),
                    jsonb_build_object(
                        'event_counts', jsonb_object_agg(event_type, cnt),
                        'total_size_bytes', sum_size,
                        'rollup', true
                    ),
                    format('rollup_%s_%s', source, date_trunc('day', timestamp)::date)
                FROM (
                    SELECT
                        timestamp, source, event_type,
                        count(*) OVER (PARTITION BY source, event_type, date_trunc('day', timestamp)) as cnt,
                        sum((metadata->>'size_bytes')::bigint) OVER (PARTITION BY source, date_trunc('day', timestamp)) as sum_size
                    FROM media_events
                    WHERE timestamp < NOW() - INTERVAL '90 days'
                      AND event_type != 'daily_summary'
                ) sub
                GROUP BY date_trunc('day', timestamp), source, cnt, sum_size
                ON CONFLICT (source_event_id) WHERE source_event_id IS NOT NULL DO NOTHING
            """)

            cur.execute("""
                DELETE FROM media_events
                WHERE timestamp < NOW() - INTERVAL '90 days'
                  AND event_type != 'daily_summary'
                  AND (metadata->>'rollup')::boolean IS NOT TRUE
            """)
            purged = cur.rowcount

        conn.commit()
        logger.info("Retention: purged %d old events (had %d)", purged, old_count)
        return {"purged": purged, "old_count": old_count}
    except Exception as e:
        conn.rollback()
        logger.exception("Retention: event rollup failed")
        return {"error": str(e)}
    finally:
        conn.close()


def _rollup_storage_postgres() -> dict:
    """Roll up storage snapshots older than 14 days to daily averages."""
    import psycopg2
    conn = db._conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT count(*) FROM storage_snapshots
                WHERE timestamp < NOW() - INTERVAL '14 days'
            """)
            old_count = cur.fetchone()[0]
            if old_count == 0:
                return {"rolled_up": 0, "message": "No snapshots older than 14 days"}

            cur.execute("""
                INSERT INTO storage_snapshots (timestamp, mount_point, total_bytes, used_bytes, source)
                SELECT
                    date_trunc('day', timestamp) + INTERVAL '12 hours',
                    mount_point,
                    avg(total_bytes)::bigint,
                    avg(used_bytes)::bigint,
                    'daily_rollup'
                FROM storage_snapshots
                WHERE timestamp < NOW() - INTERVAL '14 days'
                  AND source != 'daily_rollup'
                GROUP BY date_trunc('day', timestamp), mount_point
                ON CONFLICT DO NOTHING
            """)

            cur.execute("""
                DELETE FROM storage_snapshots
                WHERE timestamp < NOW() - INTERVAL '14 days'
                  AND source != 'daily_rollup'
            """)
            purged = cur.rowcount

        conn.commit()
        logger.info("Retention: rolled up %d old storage snapshots", purged)
        return {"rolled_up": purged, "old_count": old_count}
    except Exception as e:
        conn.rollback()
        logger.exception("Retention: storage rollup failed")
        return {"error": str(e)}
    finally:
        conn.close()


# -- SQLite implementations --

def _rollup_events_sqlite() -> dict:
    """Aggregate events older than 90 days into daily summaries, then purge (SQLite)."""
    conn = db._conn()
    try:
        cur = conn.execute("""
            SELECT count(*) FROM media_events
            WHERE timestamp < datetime('now', '-90 days')
        """)
        old_count = cur.fetchone()[0]
        if old_count == 0:
            return {"purged": 0, "message": "No events older than 90 days"}

        # Fetch grouped data for rollup
        cur = conn.execute("""
            SELECT date(timestamp) as day, source, event_type, count(*) as cnt
            FROM media_events
            WHERE timestamp < datetime('now', '-90 days')
              AND event_type != 'daily_summary'
            GROUP BY day, source, event_type
            ORDER BY day, source
        """)
        rows = [dict(r) for r in cur.fetchall()]

        # Group by day+source and build summaries
        summaries: dict[str, dict] = {}
        for row in rows:
            key = f"{row['day']}_{row['source']}"
            if key not in summaries:
                summaries[key] = {
                    "day": row["day"],
                    "source": row["source"],
                    "event_counts": {},
                    "total_count": 0,
                }
            summaries[key]["event_counts"][row["event_type"]] = row["cnt"]
            summaries[key]["total_count"] += row["cnt"]

        # Insert summaries
        for key, summary in summaries.items():
            source_event_id = f"rollup_{summary['source']}_{summary['day']}"
            metadata = json.dumps({
                "event_counts": summary["event_counts"],
                "rollup": True,
            })
            conn.execute(
                """INSERT OR IGNORE INTO media_events
                   (timestamp, source, event_type, title, metadata, source_event_id)
                   VALUES (?, ?, 'daily_summary', ?, ?, ?)""",
                (
                    summary["day"] + "T12:00:00+00:00",
                    summary["source"],
                    f"Daily summary: {summary['total_count']} {summary['source']} events",
                    metadata,
                    source_event_id,
                ),
            )

        # Purge old raw events (keep summaries)
        cur = conn.execute("""
            DELETE FROM media_events
            WHERE timestamp < datetime('now', '-90 days')
              AND event_type != 'daily_summary'
              AND json_extract(metadata, '$.rollup') IS NOT 1
        """)
        purged = cur.rowcount

        conn.commit()
        logger.info("Retention: purged %d old events (had %d)", purged, old_count)
        return {"purged": purged, "old_count": old_count}
    except Exception as e:
        conn.rollback()
        logger.exception("Retention: event rollup failed")
        return {"error": str(e)}


def _rollup_storage_sqlite() -> dict:
    """Roll up storage snapshots older than 14 days to daily averages (SQLite)."""
    conn = db._conn()
    try:
        cur = conn.execute("""
            SELECT count(*) FROM storage_snapshots
            WHERE timestamp < datetime('now', '-14 days')
        """)
        old_count = cur.fetchone()[0]
        if old_count == 0:
            return {"rolled_up": 0, "message": "No snapshots older than 14 days"}

        # Insert daily averages
        conn.execute("""
            INSERT OR IGNORE INTO storage_snapshots
                (timestamp, mount_point, total_bytes, used_bytes, source)
            SELECT
                date(timestamp) || 'T12:00:00+00:00',
                mount_point,
                CAST(avg(total_bytes) AS INTEGER),
                CAST(avg(used_bytes) AS INTEGER),
                'daily_rollup'
            FROM storage_snapshots
            WHERE timestamp < datetime('now', '-14 days')
              AND source != 'daily_rollup'
            GROUP BY date(timestamp), mount_point
        """)

        # Purge old hourly snapshots (keep daily rollups)
        cur = conn.execute("""
            DELETE FROM storage_snapshots
            WHERE timestamp < datetime('now', '-14 days')
              AND source != 'daily_rollup'
        """)
        purged = cur.rowcount

        conn.commit()
        logger.info("Retention: rolled up %d old storage snapshots", purged)
        return {"rolled_up": purged, "old_count": old_count}
    except Exception as e:
        conn.rollback()
        logger.exception("Retention: storage rollup failed")
        return {"error": str(e)}
