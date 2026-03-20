# MediaStack MCP

A unified [Model Context Protocol](https://modelcontextprotocol.io/) server for home media stacks. Polls 13 services, records events/storage/health to PostgreSQL, and exposes tools for AI agents to observe and act on your media library.

## What it does

**Observer** — Continuously polls your media services and builds institutional memory:
- What was grabbed, imported, failed, upgraded, downloaded
- Storage usage with growth forecasting
- Library sizes across all services
- Service health with degradation detection

**Actor** — Controlled content acquisition with a two-step confirmation protocol:
- Search and add content to Sonarr/Radarr/Lidarr
- Request content via Seerr/Overseerr
- Trigger missing episode/movie searches
- Trigger subtitle searches via Bazarr

Every write operation requires explicit confirmation before execution.

## Supported Services

| Service | Events | Libraries | Health | Write |
|---|---|---|---|---|
| Sonarr | history | series + size | warnings | add, search |
| Radarr | history | movies + size | warnings | add, search |
| Lidarr | history | artists + size | warnings | add, search |
| Prowlarr | — | — | warnings | — |
| Bazarr | subtitle history | — | warnings | subtitle search |
| Jellyfin | — | per-library counts | ping | — |
| Seerr/Overseerr | request pipeline | — | ping | request |
| Audiobookshelf | — | books + podcasts | ping | — |
| Boxarr | scheduler runs | — | ping | — |
| Suggestarr | recommendation stats | — | ping | — |
| Dispatcharr | — | IPTV channels | ping | — |
| SABnzbd | download history | — | ping | — |
| qBittorrent | state diff | — | ping | — |

Services are auto-discovered from environment variables. Missing services are silently skipped.

## MCP Tools (14)

### Read
- `mediastack_timeline` — Recent events from all sources
- `mediastack_storage` — Disk usage with growth forecasts
- `mediastack_health` — Service health status
- `mediastack_search` — Search events by title
- `mediastack_stats` — Database statistics
- `mediastack_libraries` — Library sizes and deltas
- `mediastack_summary` — Condensed digest (daily/weekly/monthly)

### Write (confirmation required)
- `mediastack_search_content` — Search *arr lookup APIs
- `mediastack_add_content` — Add to Sonarr/Radarr/Lidarr
- `mediastack_list_profiles` — Show quality profiles and root folders
- `mediastack_request_content` — Request via Seerr
- `mediastack_search_missing` — Trigger missing content search
- `mediastack_search_subtitles` — Trigger Bazarr subtitle search
- `mediastack_confirm` — Execute a previewed write action

## Quick Start

### Prerequisites
- Docker and Docker Compose
- PostgreSQL (for event/storage/health data)
- At least one *arr service running

### 1. Create the database

```bash
docker exec postgres psql -U postgres \
  -c "CREATE USER mediastack WITH PASSWORD 'your_password';" \
  -c "CREATE DATABASE mediastack OWNER mediastack;"
```

### 2. Configure environment

```bash
cd mediastack-mcp
cp .env.example .env  # or symlink to your shared env file
```

Required variables:
```env
# Database
DB_MEDIASTACK_NAME=mediastack
DB_MEDIASTACK_USER=mediastack
DB_MEDIASTACK_PASS=your_password
POSTGRES_HOST=postgres
POSTGRES_PORT=5432

# Add services (only configured services are polled)
SONARR_URL=http://sonarr:8989
SONARR_API_KEY=your_key
RADARR_URL=http://radarr:7878
RADARR_API_KEY=your_key
# ... etc
```

### 3. Build and run

```bash
docker compose build
docker compose up -d
```

### 4. Verify

```bash
curl http://127.0.0.1:9202/health
```

```json
{
  "status": "ok",
  "events": 202,
  "sources": 5,
  "services": "Active services: radarr, sabnzbd, sonarr, ..."
}
```

## Connecting to MCP Clients

### Claude Code
Add to `.mcp.json`:
```json
{
  "mcpServers": {
    "mediastack": {
      "type": "stdio",
      "command": "npx",
      "args": ["mcp-remote", "http://127.0.0.1:9202/mcp"]
    }
  }
}
```

### Claude Desktop
Add to `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "mediastack": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://your-host:9202/mcp"]
    }
  }
}
```

For non-HTTPS connections, add `"--allow-http"` to the args.

## Architecture

```
app/
├── server.py           # FastMCP server, 14 tools, /health endpoint
├── config.py           # Service auto-discovery from env vars
├── db.py               # PostgreSQL schema + queries
├── poller.py           # Background polling engine
├── confirmations.py    # Write confirmation protocol (5-min expiry)
├── retention.py        # Data rollup (90d events, 14d storage)
└── clients/            # Per-service API clients (13 services)
```

- **Transport:** Streamable HTTP (FastMCP)
- **Database:** PostgreSQL with JSONB metadata
- **Polling:** Background daemon thread with configurable intervals
- **Deduplication:** Events keyed by `source_event_id` (unique constraint)
- **Health:** Sparse table — only records state changes
- **Retention:** Events rolled up to daily summaries after 90 days; storage snapshots rolled up to daily averages after 14 days

## Configuration Reference

### Polling Intervals

| Variable | Default | Description |
|---|---|---|
| `MEDIASTACK_POLL_INTERVAL` | 300 | Event polling interval (seconds) |
| `MEDIASTACK_STORAGE_INTERVAL` | 3600 | Storage snapshot interval (seconds) |
| `MEDIASTACK_LIBRARY_INTERVAL` | 21600 | Library snapshot interval (seconds) |

### Service Environment Variables

<details>
<summary>API Key services (URL + key)</summary>

| Service | URL | Key |
|---|---|---|
| Sonarr | `SONARR_URL` | `SONARR_API_KEY` |
| Radarr | `RADARR_URL` | `RADARR_API_KEY` |
| Lidarr | `LIDARR_URL` | `LIDARR_API_KEY` |
| Prowlarr | `PROWLARR_URL` | `PROWLARR_API_KEY` |
| Bazarr | `BAZARR_URL` | `BAZARR_API_KEY` |
| Jellyfin | `JELLYFIN_URL` | `JELLYFIN_API_KEY` |
| Seerr | `SEERR_URL` | `SEERR_API_KEY` |
| Audiobookshelf | `AUDIOBOOKSHELF_URL` | `AUDIOBOOKSHELF_API_KEY` |

</details>

<details>
<summary>Credential services (URL + username + password)</summary>

| Service | URL | Username | Password |
|---|---|---|---|
| qBittorrent | `QBITTORRENT_URL` | `QBITTORRENT_USERNAME` | `QBITTORRENT_PASSWORD` |
| Dispatcharr | `DISPATCHARR_URL` | `DISPATCHARR_USERNAME` | `DISPATCHARR_PASSWORD` |
| Suggestarr | `SUGGESTARR_URL` | `SUGGESTARR_USERNAME` | `SUGGESTARR_PASSWORD` |

</details>

<details>
<summary>Other</summary>

| Service | URL | Auth |
|---|---|---|
| SABnzbd | `SABNZBD_URL` | `SABNZBD_API_KEY` |
| Boxarr | `BOXARR_URL` | None |

</details>

## Tech Stack

- Python 3.11
- [FastMCP](https://github.com/jlowin/fastmcp) (MCP SDK)
- PostgreSQL (psycopg2)
- httpx (async HTTP client)
- Uvicorn + Starlette

## License

Private — not for redistribution.
