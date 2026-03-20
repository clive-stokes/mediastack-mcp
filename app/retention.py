"""Data retention and rollup for MediaStack.

Runs periodically to:
1. Aggregate old media_events into daily summaries, then purge raw rows (>90 days)
2. Roll up hourly storage_snapshots to daily averages (>14 days)
3. Library snapshots and service_health are kept indefinitely (small footprint)
"""

import logging

import psycopg2

from app import db

logger = logging.getLogger(__name__)


def run_retention() -> dict:
    """Execute all retention tasks. Returns summary of actions taken."""
    results = {}
    results["events"] = _rollup_events()
    results["storage"] = _rollup_storage()
    return results


def _rollup_events() -> dict:
    """Aggregate events older than 90 days into daily summaries, then purge."""
    conn = db._conn()
    try:
        with conn.cursor() as cur:
            # Count what we'll aggregate
            cur.execute("""
                SELECT count(*) FROM media_events
                WHERE timestamp < NOW() - INTERVAL '90 days'
            """)
            old_count = cur.fetchone()[0]
            if old_count == 0:
                return {"purged": 0, "message": "No events older than 90 days"}

            # Insert daily summaries for old events (if not already done)
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

            # Purge old raw events (keep summaries)
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


def _rollup_storage() -> dict:
    """Roll up storage snapshots older than 14 days to daily averages."""
    conn = db._conn()
    try:
        with conn.cursor() as cur:
            # Count old hourly snapshots
            cur.execute("""
                SELECT count(*) FROM storage_snapshots
                WHERE timestamp < NOW() - INTERVAL '14 days'
            """)
            old_count = cur.fetchone()[0]
            if old_count == 0:
                return {"rolled_up": 0, "message": "No snapshots older than 14 days"}

            # Insert daily averages
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

            # Purge old hourly snapshots (keep daily rollups)
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
