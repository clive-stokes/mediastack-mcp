# MediaStack MCP

Unified MCP server for the NASgnolia media stack. Observes events, storage, and health across Sonarr, Radarr, Lidarr, SABnzbd, qBittorrent, Jellyfin, Seerr, and Bazarr.

## Tech Stack

- **Language:** Python 3.11
- **Protocol:** MCP via FastMCP (streamable-http transport)
- **Database:** PostgreSQL (existing NASgnolia instance)
- **HTTP client:** httpx (async)
- **Deployment:** Docker container on NASgnolia

## Architecture

```
app/
├── __init__.py
├── __main__.py         # Entry point
├── server.py           # FastMCP server, tool definitions, health endpoint
├── config.py           # Service auto-discovery from env vars
├── db.py               # PostgreSQL schema, queries, event storage
├── poller.py           # Background polling engine (daemon thread)
└── clients/
    ├── __init__.py
    ├── base.py         # ArrClient (Sonarr/Radarr/Lidarr), JellyfinClient
    ├── sonarr.py       # Sonarr API v3
    ├── radarr.py       # Radarr API v3
    ├── sabnzbd.py      # SABnzbd API
    └── qbittorrent.py  # qBittorrent Web API v2
```

## Development

```bash
# Build
docker compose build

# Run
docker compose up -d

# Logs
docker compose logs -f

# Health check
curl http://127.0.0.1:9202/health
```

## Database

- **Name:** mediastack
- **User:** mediastack
- **Tables:** media_events, storage_snapshots, library_snapshots, service_health

## MCP Tools (Phase 1)

- `mediastack_timeline` — Recent events from all sources
- `mediastack_storage` — Disk usage with growth forecasts
- `mediastack_health` — Service health status
- `mediastack_search` — Search events by title
- `mediastack_stats` — Database statistics

## Service Auto-Discovery

Services are activated when both URL and API key are provided via environment. Missing services are silently skipped.

## Key Design Decisions

- Polling-based (not webhooks) — simpler, resilient to service restarts
- Events deduplicated by source_event_id (unique constraint)
- Health table is sparse — only records state changes
- qBittorrent events detected by diffing torrent state between polls
- Storage stats from os.statvfs on mounted /media volume (not *arr API which lacks totalSpace)
- Poller runs in daemon thread with its own event loop
