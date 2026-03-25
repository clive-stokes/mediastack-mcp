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
    ├── jellyfin.py     # Jellyfin API (library stats, search, user data, collections, playlists)
    ├── seerr.py        # Seerr API v1 (request pipeline + search)
    ├── sabnzbd.py      # SABnzbd API (download history)
    ├── qbittorrent.py  # qBittorrent Web API v2 (state diffing)
    ├── audiobookshelf.py # Audiobookshelf API (library stats via /stats endpoint)
    ├── boxarr.py       # Boxarr API (box office tracking, no auth)
    ├── dispatcharr.py  # Dispatcharr API (IPTV channels/EPG, JWT auth)
    └── suggestarr.py   # Suggestarr API (AI recommendations, JWT auth)
```

## MCP Tools

### Read (12 tools)
- `mediastack_timeline` — Recent events from all sources
- `mediastack_storage` — Disk usage with growth forecasts
- `mediastack_health` — Service health status
- `mediastack_search` — Search events by title
- `mediastack_stats` — Database statistics
- `mediastack_libraries` — Library sizes and changes
- `mediastack_summary` — Condensed digest for media-brief skill
- `mediastack_jellyfin_search` — Search Jellyfin library by name
- `mediastack_jellyfin_genres` — List genres or browse items by genre
- `mediastack_jellyfin_favorites` — List current user's favourites
- `mediastack_jellyfin_collections` — List collections or items in a collection
- `mediastack_jellyfin_playlists` — List playlists or items in a playlist

### Write (14 tools, all require confirmation)
- `mediastack_search_content` — Search *arr lookup APIs
- `mediastack_add_content` — Add to Sonarr/Radarr/Lidarr
- `mediastack_list_profiles` — Show quality profiles and root folders
- `mediastack_request_content` — Request via Seerr
- `mediastack_search_missing` — Trigger *arr missing content search
- `mediastack_search_subtitles` — Trigger Bazarr subtitle search
- `mediastack_delete_content` — Remove from Sonarr/Radarr/Lidarr (optional file deletion)
- `mediastack_cancel_request` — Cancel a Seerr request
- `mediastack_jellyfin_favorite` — Add/remove Jellyfin item from favourites
- `mediastack_jellyfin_watched` — Mark Jellyfin item as played/unplayed
- `mediastack_jellyfin_collection_create` — Create a Jellyfin collection
- `mediastack_jellyfin_collection_modify` — Add/remove items from a Jellyfin collection
- `mediastack_jellyfin_playlist_create` — Create a Jellyfin playlist
- `mediastack_jellyfin_playlist_modify` — Add/remove/reorder Jellyfin playlist items

### Confirmation
- `mediastack_confirm` — Execute a previewed write action (5-min expiry, 2-min for file deletion, auto-renew on expiry)

## Services (13 active)

| Service | Events | Storage | Libraries | Health | Write |
|---------|--------|---------|-----------|--------|-------|
| Sonarr | history | root folders | series count | yes | add, search |
| Radarr | history | root folders | movie count | yes | add, search |
| Lidarr | history | root folders | artist count | yes | add, search |
| Prowlarr | — | — | — | yes | — |
| Bazarr | subtitle history | — | — | yes | subtitle search |
| Jellyfin | — | — | 20 libraries | yes | favourite, watched, collections, playlists |
| Seerr | request pipeline | — | — | yes | request |
| SABnzbd | download history | — | — | yes | — |
| qBittorrent | state diff | — | — | yes | — |
| Audiobookshelf | — | — | per-library stats | yes | — |
| Boxarr | scheduler history | — | — | yes | — |
| Dispatcharr | system events | — | channel count | yes | — |
| Suggestarr | recommendation stats | — | — | yes | — |

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
- Audiobookshelf /api/libraries doesn't include stats inline — must call /api/libraries/{id}/stats separately
- Dispatcharr and Suggestarr use JWT auth (username/password login → token)
- Boxarr has no auth — designed for local/Tailscale-only access
- Content deletion uses tiered safety: library-only removal (default, 5-min expiry) vs file deletion (2-min expiry + prominent warning)
- No bulk deletion — single item_id only, no arrays or wildcards
- Deletion audit trail stored as `delete_confirmed` event type with full preview metadata for manual re-add
