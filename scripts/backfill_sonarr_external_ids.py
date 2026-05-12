#!/usr/bin/env python3
"""One-off backfill: populate tvdb_id / tmdb_id / imdb_id on Sonarr media_events.

Reads SONARR_URL + SONARR_API_KEY from env, fetches the full series list once,
builds a {series_id: {tvdb_id, tmdb_id, imdb_id}} map (omitting None values),
then merges those keys into matching media_events rows via JSONB ||.

Idempotent — only updates rows missing at least one of the three IDs.
"""

import json
import os
import sys

import httpx
import psycopg2


def main() -> int:
    sonarr_url = os.environ.get("SONARR_URL", "").rstrip("/")
    sonarr_key = os.environ.get("SONARR_API_KEY", "")
    if not sonarr_url or not sonarr_key:
        print("SONARR_URL or SONARR_API_KEY not set", file=sys.stderr)
        return 1

    db_url = os.environ.get(
        "MEDIASTACK_DB_URL",
        f"postgresql://{os.environ.get('DB_MEDIASTACK_USER', 'mediastack')}"
        f":{os.environ.get('DB_MEDIASTACK_PASS', '')}"
        f"@{os.environ.get('POSTGRES_HOST', 'postgres')}"
        f":{os.environ.get('POSTGRES_PORT', '5432')}"
        f"/{os.environ.get('DB_MEDIASTACK_NAME', 'mediastack')}",
    )

    print(f"Fetching Sonarr series from {sonarr_url} …")
    resp = httpx.get(
        f"{sonarr_url}/api/v3/series",
        headers={"X-Api-Key": sonarr_key},
        timeout=30,
    )
    resp.raise_for_status()
    series_list = resp.json()
    print(f"  {len(series_list)} series in library")

    # Sonarr uses 0 / "" as "unknown" sentinels — exclude them from the map
    # so consumers don't try /3/tv/0 lookups.
    id_map: dict[int, dict] = {}
    for s in series_list:
        sid = s.get("id")
        if sid is None:
            continue
        ids: dict = {}
        if s.get("tvdbId"):
            ids["tvdb_id"] = s["tvdbId"]
        if s.get("tmdbId"):
            ids["tmdb_id"] = s["tmdbId"]
        if s.get("imdbId"):
            ids["imdb_id"] = s["imdbId"]
        if ids:
            id_map[sid] = ids
    print(f"  built ID map for {len(id_map)} series")

    print("Connecting to database …")
    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    try:
        # Cleanup pass — strip sentinel values written by an earlier run before
        # we treated 0 / "" as "unknown". Removes the keys entirely so the next
        # WHERE clause sees these rows as missing the id.
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE media_events SET metadata = metadata - 'tmdb_id'
                 WHERE source='sonarr' AND metadata->>'tmdb_id' IN ('0', '');
                """
            )
            tmdb_cleared = cur.rowcount
            cur.execute(
                """
                UPDATE media_events SET metadata = metadata - 'tvdb_id'
                 WHERE source='sonarr' AND metadata->>'tvdb_id' IN ('0', '');
                """
            )
            tvdb_cleared = cur.rowcount
            cur.execute(
                """
                UPDATE media_events SET metadata = metadata - 'imdb_id'
                 WHERE source='sonarr' AND metadata->>'imdb_id' = '';
                """
            )
            imdb_cleared = cur.rowcount
        conn.commit()
        if tmdb_cleared or tvdb_cleared or imdb_cleared:
            print(
                f"  cleanup: cleared sentinel values — "
                f"tmdb_id={tmdb_cleared}, tvdb_id={tvdb_cleared}, imdb_id={imdb_cleared}"
            )

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, (metadata->>'series_id')::int AS series_id
                  FROM media_events
                 WHERE source = 'sonarr'
                   AND metadata->>'series_id' IS NOT NULL
                   AND (NOT (metadata ? 'tvdb_id')
                        OR NOT (metadata ? 'tmdb_id')
                        OR NOT (metadata ? 'imdb_id'))
                """
            )
            rows = cur.fetchall()
        print(f"  {len(rows)} sonarr rows missing at least one external id")

        updated = 0
        skipped = 0
        with conn.cursor() as cur:
            for event_id, sid in rows:
                ids = id_map.get(sid)
                if not ids:
                    skipped += 1
                    continue
                cur.execute(
                    "UPDATE media_events SET metadata = metadata || %s::jsonb WHERE id = %s",
                    (json.dumps(ids), event_id),
                )
                updated += 1
        conn.commit()
        print(
            f"  updated {updated} rows, "
            f"skipped {skipped} (series_id no longer in Sonarr)"
        )
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
