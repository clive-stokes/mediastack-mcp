# MediaStack MCP

Unified MCP server for the NASgnolia media stack. Observes events, storage, and health across 9 services; provides write operations with confirmation protocol.

## Tech Stack

- **Language:** Python 3.11
- **Protocol:** MCP via FastMCP (streamable-http transport)
- **Database:** PostgreSQL (existing NASgnolia instance)
- **HTTP client:** httpx (async)
- **Deployment:** Docker container on NASgnolia, port 9202

## Architecture

```
app/
├── __init__.py
├── __main__.py         # Entry point
├── server.py           # FastMCP server, 14 MCP tools, health endpoint
├── config.py           # Service auto-discovery from env vars
├── db.py               # PostgreSQL schema, queries, event storage
├── poller.py           # Background polling engine (daemon thread)
├── confirmations.py    # Two-step write confirmation protocol
├── retention.py        # Data retention rollup (90d events, 14d storage)
└── clients/
    ├── base.py         # ArrClient (Sonarr/Radarr/Lidarr), JellyfinClient
    ├── sonarr.py       # Sonarr API v3 (read + write)
    ├── radarr.py       # Radarr API v3 (read + write)
    ├── lidarr.py       # Lidarr API v1 (read + write)
    ├── prowlarr.py     # Prowlarr API v1 (health + indexer stats)
    ├── bazarr.py       # Bazarr API (subtitle history + search)
    ├── jellyfin.py     # Jellyfin API (library stats)
    ├── seerr.py        # Seerr API v1 (request pipeline + search)
    ├── sabnzbd.py      # SABnzbd API (download history)
    └── qbittorrent.py  # qBittorrent Web API v2 (state diffing)
```

## MCP Tools

### Read (7 tools)
- `mediastack_timeline` — Recent events from all sources
- `mediastack_storage` — Disk usage with growth forecasts
- `mediastack_health` — Service health status
- `mediastack_search` — Search events by title
- `mediastack_stats` — Database statistics
- `mediastack_libraries` — Library sizes and changes
- `mediastack_summary` — Condensed digest for media-brief skill

### Write (6 tools, all require confirmation)
- `mediastack_search_content` — Search *arr lookup APIs
- `mediastack_add_content` — Add to Sonarr/Radarr/Lidarr
- `mediastack_list_profiles` — Show quality profiles and root folders
- `mediastack_request_content` — Request via Seerr
- `mediastack_search_missing` — Trigger *arr missing content search
- `mediastack_search_subtitles` — Trigger Bazarr subtitle search

### Confirmation
- `mediastack_confirm` — Execute a previewed write action (5-min expiry)

## Services (9 active)

| Service | Events | Storage | Libraries | Health | Write |
|---------|--------|---------|-----------|--------|-------|
| Sonarr | history | root folders | series count | yes | add, search |
| Radarr | history | root folders | movie count | yes | add, search |
| Lidarr | history | root folders | artist count | yes | add, search |
| Prowlarr | — | — | — | yes | — |
| Bazarr | subtitle history | — | — | yes | subtitle search |
| Jellyfin | — | — | 20 libraries | yes | — |
| Seerr | request pipeline | — | — | yes | request |
| SABnzbd | download history | — | — | yes | — |
| qBittorrent | state diff | — | — | yes | — |

## Key Design Decisions

- Polling-based (not webhooks) — simpler, resilient to service restarts
- Events deduplicated by source_event_id (unique constraint)
- Health table is sparse — only records state changes
- qBittorrent events detected by diffing torrent state between polls
- Storage stats from os.statvfs on mounted /media volume
- Poller runs in daemon thread with its own event loop
- Write ops use async bridge via poller event loop
- Retention: events rolled up at 90 days, storage at 14 days
- media-brief skill registered in nanobot as MCP consumer
