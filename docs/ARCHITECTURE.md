# MediaStack MCP — Architecture & Usage Guide

Unified MCP server for home media stacks. Observes events, storage, and health across 13 services; provides controlled write operations for content acquisition.

---

## Overview

MediaStack MCP has two roles:

1. **Observer** — Polls services on a schedule, records events/storage/health to PostgreSQL, and exposes read tools for agents and humans to query.
2. **Actor** — Provides controlled write operations (add content, request via Seerr, trigger searches) with a two-step confirmation protocol. No action executes without explicit confirmation.

MediaStack does **not** restart containers, modify service configurations, or perform any infrastructure-level operations. It operates at the content layer only.

---

## Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                        Your Server                             │
│                                                                │
│  ┌────────────────┐         ┌──────────────────────────┐      │
│  │  MediaStack MCP │────────▶│ PostgreSQL (mediastack)  │      │
│  │  (FastMCP)      │         │ 4 tables, daily rollup   │      │
│  │  Port 8000      │         └──────────────────────────┘      │
│  └──────┬─────────┘                                            │
│         │                                                       │
│   Polls (every 5 min):                 Writes (confirmed):         │
│   ├── Sonarr       (:8989)            ├── Sonarr (add/del, search) │
│   ├── Radarr       (:7878)            ├── Radarr (add/del, search) │
│   ├── Lidarr       (:8686)            ├── Lidarr (add/del, search) │
│   ├── Prowlarr     (:9696)            ├── Seerr  (request, cancel) │
│   ├── Bazarr       (:6767)            └── Bazarr (subtitles)       │
│   ├── Jellyfin     (:8096)                                     │
│   ├── Seerr        (:5055)                                     │
│   ├── Audiobookshelf (:80)                                     │
│   ├── Boxarr       (:8888)                                     │
│   ├── Suggestarr   (:5000)                                     │
│   ├── Dispatcharr  (:9191)                                     │
│   ├── SABnzbd      (:8080)                                     │
│   └── qBittorrent  (:8481)                                     │
│                                                                 │
│  MCP Consumers:                                                 │
│  ├── Claude Code     (localhost/mcp)                           │
│  ├── Claude Desktop  (via reverse proxy)                       │
│  └── Other MCP clients (nanobot, custom agents)                │
└────────────────────────────────────────────────────────────────┘
```

---

## Repository Structure

```
mediastack-mcp/
├── CLAUDE.md               # Dev reference
├── Dockerfile              # Python 3.11-slim-bookworm
├── docker-compose.yaml     # Deployment config
├── requirements.txt        # mcp[cli], psycopg2-binary, httpx, uvicorn, starlette
└── app/
    ├── server.py           # FastMCP server, 14 MCP tools, /health endpoint
    ├── config.py           # Service auto-discovery from env vars
    ├── db.py               # PostgreSQL schema + queries (4 tables)
    ├── poller.py           # Background polling engine (daemon thread, 6 loops)
    ├── confirmations.py    # Two-step write confirmation protocol (5-min expiry)
    ├── retention.py        # Data rollup (90d events, 14d storage)
    └── clients/            # Per-service API clients (13 services)
        ├── base.py         # ArrClient, JellyfinClient base classes
        ├── sonarr.py       # Sonarr API v3 (read + write)
        ├── radarr.py       # Radarr API v3 (read + write)
        ├── lidarr.py       # Lidarr API v1 (read + write)
        ├── prowlarr.py     # Prowlarr API v1
        ├── bazarr.py       # Bazarr API (read + subtitle search)
        ├── jellyfin.py     # Jellyfin API (library stats)
        ├── seerr.py        # Seerr/Overseerr API v1 (read + request)
        ├── sabnzbd.py      # SABnzbd API
        ├── qbittorrent.py  # qBittorrent Web API v2
        ├── audiobookshelf.py # Audiobookshelf API (library stats)
        ├── boxarr.py       # Boxarr FastAPI (scheduler history, no auth)
        ├── dispatcharr.py  # Dispatcharr DRF API (JWT auth)
        └── suggestarr.py   # SuggestArr Flask API (JWT auth)
```

---

## Services (13)

| Service | Auth | Events | Libraries | Health | Write |
|---|---|---|---|---|---|
| Sonarr | API key | history | series count + size | *arr warnings | add, search |
| Radarr | API key | history | movie count + size | *arr warnings | add, search |
| Lidarr | API key | history | artist count + size | *arr warnings | add, search |
| Prowlarr | API key | — | — | *arr warnings | — |
| Bazarr | API key | subtitle history | — | *arr warnings | subtitle search |
| Jellyfin | API key | — | per-library counts | ping | — |
| Seerr | API key | request pipeline | — | ping | request |
| Audiobookshelf | Bearer token | — | audiobooks + podcasts | ping | — |
| Boxarr | None | scheduler runs | — | ping | — |
| Suggestarr | JWT (user/pass) | recommendation stats | — | ping | — |
| Dispatcharr | JWT (user/pass) | — | IPTV channel count | ping | — |
| SABnzbd | API key | download history | — | ping | — |
| qBittorrent | Session cookie | state diff | — | ping | — |

---

## Database

### Tables

| Table | Purpose | Retention |
|---|---|---|
| `media_events` | Every event from every service (grabs, imports, failures, requests) | 90 days raw, then daily summaries |
| `storage_snapshots` | Hourly disk usage per mount point | 14 days hourly, then daily averages |
| `library_snapshots` | Periodic library item counts and sizes | Indefinite (small footprint) |
| `service_health` | Service health state changes (sparse) | Indefinite (sparse) |

### Useful Queries

```sql
-- Recent events by source
SELECT source, event_type, title, timestamp
FROM media_events ORDER BY timestamp DESC LIMIT 20;

-- Storage usage
SELECT mount_point, pg_size_pretty(total_bytes) as total,
       pg_size_pretty(used_bytes) as used,
       round(used_bytes::numeric / total_bytes * 100, 1) as pct
FROM storage_snapshots ORDER BY timestamp DESC LIMIT 5;

-- Service health
SELECT DISTINCT ON (service) service, status, detail, timestamp
FROM service_health ORDER BY service, timestamp DESC;

-- Library sizes
SELECT source, library_name, item_count, pg_size_pretty(size_bytes)
FROM library_snapshots ORDER BY source, library_name;
```

---

## MCP Tools

### Read Tools (9)

| Tool | Description | Key Parameters |
|---|---|---|
| `mediastack_timeline` | Recent events (grabs, imports, failures) | `hours`, `source`, `event_type`, `limit` |
| `mediastack_storage` | Disk usage with growth forecasts | `include_forecast` |
| `mediastack_health` | Current health of all 13 services | — |
| `mediastack_search` | Search events by title | `query`, `days`, `source` |
| `mediastack_stats` | Database statistics | — |
| `mediastack_libraries` | Library sizes and deltas | `source`, `include_delta` |
| `mediastack_summary` | Condensed digest for briefings | `period` (day/week/month) |
| `mediastack_jellyfin_search` | Search Jellyfin library by name | `query`, `limit` |
| `mediastack_jellyfin_genres` | List genres or browse items by genre | `media_type`, `genre`, `limit` |

### Write Tools (14, all require confirmation)

| Tool | Description | Key Parameters |
|---|---|---|
| `mediastack_search_content` | Search *arr lookup APIs | `query`, `media_type` (tv/movie/music) |
| `mediastack_add_content` | Add to Sonarr/Radarr/Lidarr | `media_type`, `external_id`, `title` |
| `mediastack_list_profiles` | Show quality profiles & root folders | `service` |
| `mediastack_request_content` | Request via Seerr | `query`, `media_type` |
| `mediastack_search_missing` | Trigger *arr missing search | `service`, `item_id`, `season` |
| `mediastack_search_subtitles` | Trigger Bazarr subtitle search | `media_type`, `item_id`, `language` |
| `mediastack_delete_content` | Remove from Sonarr/Radarr/Lidarr | `media_type`, `item_id`, `delete_files` |
| `mediastack_cancel_request` | Cancel a Seerr request | `request_id` |
| `mediastack_jellyfin_favorite` | Add/remove Jellyfin favourite | `item_id`, `item_name`, `favorite` |
| `mediastack_jellyfin_watched` | Mark played/unplayed | `item_id`, `item_name`, `played` |
| `mediastack_jellyfin_collection_create` | Create a Jellyfin collection | `name`, `item_ids` |
| `mediastack_jellyfin_collection_modify` | Modify collection items | `collection_id`, `add_ids`, `remove_ids` |
| `mediastack_jellyfin_playlist_create` | Create a Jellyfin playlist | `name`, `item_ids`, `media_type` |
| `mediastack_jellyfin_playlist_modify` | Modify playlist items/order | `playlist_id`, `add_ids`, `remove_ids`, `move_item_id` |

### Confirmation Protocol

All write tools return a **preview** and a **confirmation_id**. To execute:

```
1. Call write tool -> get preview + confirmation_id
2. Call mediastack_confirm(confirmation_id) -> action executes
```

Unconfirmed actions expire after **5 minutes**. All confirmed writes are logged to `media_events` as audit trail.

### Deletion Safety

Content deletion uses a tiered safety model:

- **Library-only removal** (`delete_files=false`, default) — removes from *arr database, files stay on disk. Standard 5-minute confirmation expiry.
- **File deletion** (`delete_files=true`) — removes entry AND deletes media files. Shortened **2-minute** expiry with prominent warning showing path and size.
- **No bulk operations** — single `item_id` only. No arrays or wildcards.
- **Audit trail** — deletions logged as `delete_confirmed` event type with full metadata (external ID, quality profile, root folder) enabling manual re-add if needed.

---

## Usage Examples

### "What happened with Severance?"
```
> mediastack_search(query="Severance", days=30)
< Events: grabbed S02E08, imported S02E08, subtitle downloaded (English)
```

### "How's storage looking?"
```
> mediastack_storage(include_forecast=true)
< /media/Movies: 35.8% (5.9TB / 16TB), ~180 days to 90%
  /media/TV: 35.8%
```

### "Add The Bear to Sonarr"
```
> mediastack_search_content(query="The Bear", media_type="tv")
< Results: The Bear (2022), tvdb_id: 394256
> mediastack_add_content(media_type="tv", external_id="394256", title="The Bear")
< Preview: Add "The Bear" to Sonarr (HD-1080p, /media/TV). Confirm? [abc123]
> mediastack_confirm(confirmation_id="abc123")
< Done. "The Bear" added to Sonarr. Monitoring for downloads.
```

### "Give me a daily media brief"
```
> mediastack_summary(period="day")
< Events by source, storage deltas, health status, library changes
```

---

## Polling Schedule

| Data Type | Interval | Source |
|---|---|---|
| Events (history) | 5 minutes | Sonarr, Radarr, Lidarr, SABnzbd, qBittorrent, Bazarr, Seerr, Boxarr, Suggestarr |
| Storage snapshots | 1 hour | *arr root folder APIs + os.statvfs |
| Library snapshots | 6 hours | Sonarr, Radarr, Lidarr, Jellyfin, Audiobookshelf, Dispatcharr |
| Health checks | 5 minutes | All 13 services (ping + *arr health warnings) |
| Retention rollup | 24 hours | media_events (90d), storage_snapshots (14d) |

---

## Troubleshooting

### Container won't start
```bash
docker compose logs mediastack-mcp
# Common: database not created, env vars missing
```

### Service shows "unreachable"
```bash
# Test from inside the container
docker exec mediastack-mcp curl -sf http://sonarr:8989/api/v3/system/status \
  -H "X-Api-Key: YOUR_KEY"
# Check: is the service running? Is it on the same Docker network?
```

### Events not recording
```bash
# Check event counts
docker exec postgres psql -U mediastack -d mediastack \
  -c "SELECT source, count(*) FROM media_events GROUP BY source;"
# If 0: check API keys, service connectivity
```

### VPN services (qBittorrent, SABnzbd, Dispatcharr)
If these services run behind a VPN container (e.g. gluetun), access them via `gluetun:<port>`, not `<service>:<port>`. If the VPN container is down, all services behind it are unreachable — this is expected.

### Storage snapshots empty
MediaStack uses `os.statvfs` on `/media/*`. If the volume mount is missing from docker-compose.yaml, no storage data is collected.

### MCP tools not appearing in Claude Desktop
- Restart Claude Desktop after adding the MCP config
- `mcp-remote` requires `--allow-http` for non-HTTPS URLs
- If using a reverse proxy, ensure SSE-compatible timeout settings:
  ```nginx
  proxy_read_timeout 86400s;
  proxy_send_timeout 86400s;
  proxy_buffering off;
  proxy_cache off;
  ```

### JWT auth services (Dispatcharr, Suggestarr) failing
- JWT tokens are cached and auto-refreshed on 401
- If credentials change, restart MediaStack

### Audiobookshelf libraries showing zero items
Audiobookshelf's `/api/libraries` response does not include stats inline. The poller calls `/api/libraries/{id}/stats` separately for each library to get `totalItems` and `totalSize`. If the stats endpoint fails, the library is recorded with zero items rather than skipped.

### Retention not running
Retention runs 1 hour after startup, then every 24 hours. To run manually:
```bash
docker exec mediastack-mcp python -c "from app.retention import run_retention; print(run_retention())"
```

---

## Key Design Decisions

| Decision | Rationale |
|---|---|
| Polling, not webhooks | Simpler, resilient to service restarts, no *arr webhook config needed |
| PostgreSQL, not SQLite | Concurrent access, JSONB for flexible metadata, existing infrastructure |
| source_event_id dedup | Prevents duplicate events when polling overlapping time windows |
| Sparse health table | Only records state changes, not every 5-min ping |
| qBit state diffing | qBittorrent has no history API — detect transitions by comparing torrent lists |
| os.statvfs for storage | *arr rootfolder API only reports freeSpace, not totalSpace |
| Daemon thread for poller | Keeps poller event loop separate from FastMCP's uvicorn loop |
| 5-min confirmation expiry | Prevents stale write actions from executing unexpectedly |
| Boxarr tracks via Radarr tags | Boxarr adds movies directly to Radarr with "boxarr" tag, not via Seerr |
| Suggestarr internal tracking | SuggestArr strips attribution before Seerr; query its own API for request source |
| media-brief as skill, not cron | Callable on-demand; can be scheduled via agent cron separately |
