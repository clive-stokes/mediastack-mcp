# MediaStack MCP v1.2.0

## Changes

### Feat: opt-in bearer token auth on `/mcp` and `/ingest`

New optional `MEDIASTACK_AUTH_TOKEN` env var. When set, requests to `/mcp`
and `/ingest` without `Authorization: Bearer <token>` are rejected with 401
(`secrets.compare_digest` comparison). `/health` stays open for the Docker
healthcheck. Unset = previous behaviour; existing deployments unaffected
until they opt in. Rationale: 127.0.0.1 port binding protects against the
LAN, not against the ~15 containers sharing `npm-network` — any compromised
container previously had unauthenticated access to the file deletion tools.

### Perf: persistent HTTP clients and database connection pool

- `ArrClient`/`JellyfinClient` now reuse one `httpx.AsyncClient` per service
  instead of a new TCP connection per request; closed cleanly on shutdown.
- `db.py` borrows from a `ThreadedConnectionPool` (1–5) instead of
  `psycopg2.connect()` per call.
- All `db.*` calls made from coroutines (poller loops, `/health`, `/ingest`)
  are wrapped in `asyncio.to_thread(...)` — the event loop no longer stalls
  on database I/O.

### Fix: retention rollup dropped per-type counts from daily summaries

The summary INSERT grouped by a per-event-type window count, so event types
with different daily counts collided on the `rollup_{source}_{day}` dedup
key and only the first survived. Summaries now aggregate per type in a CTE
first, then fold into one complete row per (day, source).

### Fix: `_run_async` fallback broke on Python 3.14

`asyncio.get_event_loop()` raises `RuntimeError` on 3.14 when no loop
exists; the startup-race fallback now uses `asyncio.run()`.

### Test: four-tier test suite + CI

72 tests. Tier 1 pure logic (confirmation protocol, path validation, env
discovery, auth middleware); tier 2 mocked HTTP via respx (delete previews,
client headers, history parsing); tier 3 real throwaway PostgreSQL (dedup,
retention, growth); tier 4 opt-in live smoke (`pytest -m live`). GitHub
Actions workflow runs ruff + tiers 1–3 on push/PR.

### Build/docs

- MCP SDK pinned `>=1.8,<2.0` with a guard test for the
  `_validate_accept_header` monkey-patch target.
- `.env.example` added (quick start step 2 previously referenced a file
  that did not exist); README Python version corrected to 3.14, threading
  model wording fixed, tests claim replaced by one that is true.

---

# MediaStack MCP v1.1.0

## Changes

### Fix: Jellyfin library sizes for unmanaged libraries (#3)

`mediastack_libraries` previously returned `size_bytes: 0` for any Jellyfin library
not managed by an arr tool — Trash TV, Music Videos, Radio Shows, My Videos, and similar
unmanaged content had no usable size data.

**Root cause:** The poller explicitly set `size = 0` for all non-STRM Jellyfin libraries
with the assumption that arr tools would cover them. They don't for content outside
Sonarr/Radarr/Lidarr root folders.

**Fix:** The Jellyfin library polling block now uses path-based detection to distinguish:

- **STRM/iFiesta libraries** — `size_bytes = NULL` (negligible `.strm` text files, unchanged)
- **Arr-managed libraries** — `size_bytes = NULL` (Sonarr/Radarr/Lidarr record accurate
  sizes via their own poll; storing 0 from Jellyfin would be misleading)
- **Unmanaged libraries** — `size_bytes` measured via `du -sb` on the library's filesystem
  path, run in a thread pool to avoid blocking the event loop

A library is considered arr-managed when any of its `Locations` paths (from Jellyfin's
`/Library/VirtualFolders` API) overlap with a root folder configured in Sonarr, Radarr,
or Lidarr.

**Impact:** The `mediastack_libraries` tool now returns real sizes for unmanaged Jellyfin
libraries, enabling accurate pie chart breakdowns of disk usage across all content types.

---

## Changes (continued)

### Feat: daily cron schedule for library polling

Adds an optional `MEDIASTACK_LIBRARY_CRON=HH:MM` env var that replaces the
fixed-interval library poll with a daily scheduled run.

**Behaviour:**
- On container startup, the library poll fires immediately (unchanged — useful for testing)
- After the first run, the poller sleeps until the next daily occurrence of `HH:MM`
- If `MEDIASTACK_LIBRARY_CRON` is not set, the existing `MEDIASTACK_LIBRARY_INTERVAL`
  behaviour is preserved (fully backwards compatible)

**Why:** The `du -sb` calls added in this release traverse media directories to measure
disk usage for unmanaged Jellyfin libraries. On a spinning HDD array, this wakes
hibernating disks. Scheduling the poll at 02:30 (when Backrest is already running and
drives are spinning) eliminates unnecessary wake-ups at arbitrary times.

**Configuration:**
```yaml
# docker-compose.yaml
environment:
  TZ: Europe/London  # Required — cron fires in container local time
  MEDIASTACK_LIBRARY_CRON: "02:30"
```

Invalid values (wrong format, out-of-range hours/minutes) log a warning and fall
back to `MEDIASTACK_LIBRARY_INTERVAL`.

---

# MediaStack MCP v1.0.0

A unified MCP server for home media stacks. Polls 13 services, records events and storage to PostgreSQL, and exposes 27 tools for AI agents to observe and manage your media library.

## Highlights

- **13 services** monitored: Sonarr, Radarr, Lidarr, Prowlarr, Bazarr, Jellyfin, Seerr, Audiobookshelf, SABnzbd, qBittorrent, Boxarr, Suggestarr, Dispatcharr
- **27 MCP tools** — 12 read, 14 write (with confirmation protocol), plus confirm
- **Two-step confirmation** for all write operations — preview before execute, auto-renew on expiry
- **Content deletion** with tiered safety: library-only removal (default) or file deletion (2-minute expiry with warnings)
- **Duplicate prevention** on playlist and collection creation
- **Multi-user Jellyfin** — all user-scoped tools accept an optional `user_name` parameter
- **Storage forecasting** with linear regression on snapshot history
- **Data retention** — automatic daily rollup (90-day events, 14-day storage snapshots)

## Tools

### Observer (read)
| Tool | Description |
|---|---|
| `mediastack_timeline` | Recent events from all sources |
| `mediastack_storage` | Disk usage with growth forecasts |
| `mediastack_health` | Service health status |
| `mediastack_search` | Search events by title |
| `mediastack_stats` | Database statistics |
| `mediastack_libraries` | Library sizes and deltas |
| `mediastack_summary` | Condensed digest (daily/weekly/monthly) |
| `mediastack_jellyfin_search` | Search Jellyfin library by name |
| `mediastack_jellyfin_genres` | List genres or browse items by genre |
| `mediastack_jellyfin_favorites` | List a user's favourites |
| `mediastack_jellyfin_collections` | List collections or items in a collection |
| `mediastack_jellyfin_playlists` | List playlists or items in a playlist |

### Actor (write — all require confirmation)
| Tool | Description |
|---|---|
| `mediastack_search_content` | Search *arr lookup APIs |
| `mediastack_add_content` | Add to Sonarr/Radarr/Lidarr |
| `mediastack_delete_content` | Remove from Sonarr/Radarr/Lidarr |
| `mediastack_list_profiles` | Show quality profiles and root folders |
| `mediastack_request_content` | Request via Seerr |
| `mediastack_cancel_request` | Cancel a Seerr request |
| `mediastack_search_missing` | Trigger *arr missing content search |
| `mediastack_search_subtitles` | Trigger Bazarr subtitle search |
| `mediastack_jellyfin_favorite` | Add/remove Jellyfin favourite |
| `mediastack_jellyfin_watched` | Mark played/unplayed |
| `mediastack_jellyfin_collection_create` | Create a collection |
| `mediastack_jellyfin_collection_modify` | Add/remove items from a collection |
| `mediastack_jellyfin_playlist_create` | Create a playlist |
| `mediastack_jellyfin_playlist_modify` | Add/remove/reorder playlist items |
| `mediastack_confirm` | Execute a previewed write action |

## Getting Started

```bash
# Create database
docker exec postgres psql -U postgres \
  -c "CREATE USER mediastack WITH PASSWORD 'your_password';" \
  -c "CREATE DATABASE mediastack OWNER mediastack;"

# Configure services via environment variables
cp .env.example .env

# Build and run
docker compose build && docker compose up -d

# Verify
curl http://127.0.0.1:9202/health
```

See [README.md](README.md) for full configuration reference and MCP client setup.

## Tech Stack

- Python 3.11 + FastMCP
- PostgreSQL (psycopg2, JSONB metadata)
- httpx (async HTTP)
- Docker

## How this was built

Designed and implemented entirely with [Claude](https://claude.ai), working from a detailed requirements specification. The spec covered architecture, 13 service integrations, database schema, polling strategy, MCP tool design, confirmation protocol, and phased delivery. Five implementation phases from foundation to Jellyfin user operations.

## License

[MIT](LICENSE)
