# Building MediaStack MCP with Claude — A Development Diary

## The Problem

I run a home media server with 13+ Docker services — Sonarr, Radarr, Lidarr, Jellyfin, SABnzbd, qBittorrent, and several others. Each has its own dashboard, its own API, its own way of reporting what happened. Asking "what downloaded last night?" meant checking four different UIs. Asking "how's storage looking across all my drives?" meant SSH and `df -h`.

I wanted a single interface where an AI agent could answer questions about my entire media stack — and, when asked, take controlled actions like adding content or triggering searches. Not a dashboard. A conversation.

## The Approach

The entire project was built using Claude (Anthropic's AI assistant) via Claude Code, working from a detailed requirements specification. Here's how that went.

### Phase 1: The Spec

Before writing any code, I wrote a comprehensive requirements document covering:

- Which services to integrate and what data to extract from each
- Database schema design (PostgreSQL with 4 tables)
- Polling strategy (intervals, deduplication, resilience)
- MCP tool design (what each tool should accept and return)
- A phased delivery plan (5 phases, from scaffolding to maturity)

This was the most important step. A detailed spec meant Claude could work autonomously through each phase with minimal back-and-forth. The spec was about 3,000 words — far more detailed than I'd normally write for my own reference, but the investment paid off in execution speed.

### Phase 2: Scaffolding and Core (1 session)

Claude set up the entire project structure in one go:

- Python project with FastMCP, httpx, psycopg2
- Docker multi-stage build
- PostgreSQL schema (auto-created on startup)
- Base client abstraction for *arr services (Sonarr, Radarr, Lidarr share an API pattern)
- Background polling engine running in a daemon thread
- Service auto-discovery from environment variables

The key architectural decision — polling over webhooks — was in the spec. Claude implemented it cleanly: a background thread with its own asyncio event loop, running six concurrent polling loops (events, storage, libraries, health, retention, and the poller coordinator).

### Phase 3: Service Clients (1 session)

This is where the pattern from [mcp-arr](https://github.com/aplaceforallmystuff/mcp-arr) proved useful. I pointed Claude at that project for reference on *arr API patterns and tool naming conventions.

Each service client is a standalone module:
- **Sonarr/Radarr/Lidarr** share a base class (`ArrClient`) since they use the same API v3 pattern
- **qBittorrent** has no event history API, so Claude implemented state-diffing — comparing the torrent list between polls to detect completions, stalls, and removals
- **Jellyfin** exposes 20+ libraries through a single endpoint
- **Bazarr, Seerr, SABnzbd** each have their own API patterns

Later, I added four more services that needed research first:
- **Audiobookshelf** — Bearer token auth, but its `/api/libraries` endpoint doesn't include stats inline (a quirk Claude discovered during testing and fixed)
- **Boxarr** — Full FastAPI REST API with no authentication. Adds movies directly to Radarr with a "boxarr" tag for attribution
- **Dispatcharr** — Django REST Framework API with JWT auth. IPTV channel/EPG management
- **Suggestarr** — Flask API with JWT auth. AI-driven content recommendations that push to Seerr (but strip source attribution before submitting)

For Dispatcharr, Boxarr, and Suggestarr, Claude researched their GitHub repos autonomously — finding API endpoints, authentication methods, and available data — then built the clients.

### Phase 4: Write Operations and Safety

This was the phase I was most careful about. Write operations that modify your media library need guardrails.

The solution is a two-step confirmation protocol:

1. You call a write tool (e.g. "add The Bear to Sonarr")
2. MediaStack returns a **preview** of what will happen and a **confirmation_id**
3. You call `mediastack_confirm` with that ID to execute
4. Unconfirmed actions expire after 5 minutes

Every confirmed action is logged to the `media_events` table as an audit trail. This means the agent can never silently add content — there's always a human-readable record.

### Phase 5: Maturity

The final phase added:
- **Retention rollup** — Events older than 90 days are aggregated to daily summaries. Storage snapshots older than 14 days are averaged to daily values. Keeps the database lean.
- **Storage forecasting** — Linear extrapolation from historical snapshots to estimate "days until 90% full"
- **media-brief skill** — A pre-built prompt for my AI agent (nanobot) that calls `mediastack_summary` and formats the output as a daily briefing

## What Worked Well

**The spec-first approach.** Writing a detailed spec before touching code meant each implementation phase was a clean handoff. Claude could work through an entire phase without stopping to ask "what should this tool return?" or "which table does this go in?"

**Parallel research.** When investigating Dispatcharr, Boxarr, and Suggestarr APIs, Claude launched three research agents simultaneously. Each autonomously explored the GitHub repo, read source code, and returned a structured summary of available endpoints, authentication methods, and data models. The total research took about 3 minutes wall-clock time.

**Pattern reuse.** The `ArrClient` base class meant adding Lidarr was trivial once Sonarr and Radarr worked. The JWT auth pattern for Dispatcharr was reused almost verbatim for Suggestarr.

**Iterative debugging.** When Audiobookshelf libraries showed zero items, Claude tested the API from inside the running container, identified that stats aren't included in the library list response, and fixed the poller to call the per-library stats endpoint instead. The whole cycle — identify, diagnose, fix, rebuild, verify — took about 5 minutes.

## What I'd Do Differently

**Start with fewer services.** 13 services is a lot. I'd recommend starting with the core *arr stack (Sonarr, Radarr, SABnzbd) and adding services incrementally. The auto-discovery pattern makes this painless.

**NPM proxy configuration for SSE.** Claude Desktop connects via mcp-remote, which uses Server-Sent Events. If you're running behind Nginx Proxy Manager, you need extended timeouts (`proxy_read_timeout 86400s`) and disabled buffering. This took some debugging to figure out.

**JWT token caching.** The Dispatcharr and Suggestarr clients cache JWT tokens and auto-refresh on 401. This works, but a more robust approach would be to check token expiry proactively rather than waiting for a failure.

## The Numbers

- **14 MCP tools** (7 read, 6 write + 1 confirm)
- **13 services** integrated
- **4 database tables** with automated retention
- **~2,000 lines of Python** across 18 source files
- **5 implementation phases** over 1 session
- **0 lines of code written by hand** — all generated by Claude from the spec and iterative feedback

## Try It

The project is open source under MIT licence: [github.com/clive-stokes/mediastack-mcp](https://github.com/clive-stokes/mediastack-mcp)

You'll need Docker, PostgreSQL, and at least one *arr service running. Configure your service URLs and API keys as environment variables, and MediaStack auto-discovers what's available.
