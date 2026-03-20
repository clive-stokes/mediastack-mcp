"""MediaStack MCP Server — unified media stack observer and actor."""

import asyncio
import json
import logging

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from app import db
from app.config import Config
from app.poller import Poller

# Patch: relax Accept header validation so Claude Code can connect.
import mcp.server.streamable_http as _shttp

_orig_validate = _shttp.StreamableHTTPServerTransport._validate_accept_header


async def _relaxed_validate(self, request, scope, send):
    return True


_shttp.StreamableHTTPServerTransport._validate_accept_header = _relaxed_validate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

mcp_app = FastMCP("MediaStack", json_response=True, host="0.0.0.0", port=8000)

# Global state
_config: Config | None = None
_poller: Poller | None = None


# -- Health endpoint --

@mcp_app.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    try:
        stats = db.get_stats()
        return JSONResponse({
            "status": "ok",
            "events": stats["total_events"],
            "sources": stats["active_sources"],
            "services": _config.describe() if _config else "not initialised",
        })
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=503)


# -- Phase 1 Tools: timeline, storage, health --

@mcp_app.tool()
def mediastack_timeline(
    hours: int = 24,
    source: str | None = None,
    event_type: str | None = None,
    limit: int = 100,
) -> str:
    """Get recent media stack events.

    Returns events from Sonarr, Radarr, SABnzbd, qBittorrent, and other
    configured services. Events include grabs, imports, failures, upgrades,
    downloads, and more.

    Args:
        hours: Look-back period in hours (default 24)
        source: Filter by service name (sonarr, radarr, sabnzbd, qbittorrent, etc.)
        event_type: Filter by event type (grabbed, imported, failed, downloaded, etc.)
        limit: Maximum events to return (default 100)
    """
    events = db.get_timeline(hours=hours, source=source, event_type=event_type, limit=limit)
    if not events:
        return "No events in the requested time window."
    return json.dumps(events, indent=2)


@mcp_app.tool()
def mediastack_storage(include_forecast: bool = True) -> str:
    """Get current storage usage across all media mount points.

    Shows disk usage per mount point discovered from *arr root folder APIs,
    with optional growth forecasts.

    Args:
        include_forecast: Include 7/30/90-day growth rates and capacity projections (default True)
    """
    current = db.get_storage_current()
    if not current:
        return "No storage data collected yet. The poller needs at least one cycle."

    if include_forecast:
        for entry in current:
            mp = entry["mount_point"]
            for days in (7, 30, 90):
                growth = db.get_storage_growth(mp, days)
                if growth and growth["bytes_per_day"] > 0:
                    entry[f"growth_{days}d"] = growth
                    # Project days to 85% and 90%
                    total = entry["total_bytes"]
                    used = entry["used_bytes"]
                    bpd = growth["bytes_per_day"]
                    for threshold in (85, 90):
                        target = total * threshold / 100
                        if used < target and bpd > 0:
                            days_to = int((target - used) / bpd)
                            entry[f"days_to_{threshold}pct_{days}d_rate"] = days_to

    return json.dumps(current, indent=2)


@mcp_app.tool()
def mediastack_health() -> str:
    """Get current health status of all monitored media services.

    Returns the latest health state per service (healthy, degraded, unreachable),
    with detail on any issues.
    """
    health_data = db.get_latest_health()
    if not health_data:
        return "No health data yet. The poller needs at least one cycle."
    return json.dumps(health_data, indent=2)


@mcp_app.tool()
def mediastack_search(query: str, days: int = 30, source: str | None = None) -> str:
    """Search the media event log by title.

    Find events related to a specific show, movie, artist, or download.
    Example: "what happened with Severance?"

    Args:
        query: Search term — matched against event titles
        days: Look-back period in days (default 30)
        source: Filter by service name (optional)
    """
    results = db.search_events(query=query, days=days, source=source)
    if not results:
        return f"No events matching '{query}' in the last {days} days."
    return json.dumps(results, indent=2)


@mcp_app.tool()
def mediastack_libraries(source: str | None = None, include_delta: bool = True) -> str:
    """Get current library sizes and recent changes.

    Shows item counts and disk usage per library from Sonarr, Radarr,
    Lidarr, Jellyfin, and other configured services.

    Args:
        source: Filter by service (sonarr, radarr, lidarr, jellyfin, etc.)
        include_delta: Include change since last snapshot (default True)
    """
    libraries = db.get_libraries_current()
    if source:
        libraries = [l for l in libraries if l["source"] == source]
    if not libraries:
        return "No library data yet. The poller needs at least one library snapshot cycle."

    if include_delta:
        for lib in libraries:
            delta = db.get_library_delta(lib["source"], lib["library_name"])
            if delta:
                lib["delta"] = delta

    return json.dumps(libraries, indent=2)


@mcp_app.tool()
def mediastack_summary(period: str = "day") -> str:
    """Get a condensed digest of media stack activity.

    Summarises events by source, storage status, health issues, and
    library changes for the given period. Designed for the media-brief
    skill or any consumer wanting a quick overview.

    Args:
        period: One of "day" (24h), "week" (7d), or "month" (30d)
    """
    hours_map = {"day": 24, "week": 168, "month": 720}
    hours = hours_map.get(period, 24)

    result = {"period": period, "hours": hours}

    # Event summary
    result["events"] = db.get_event_summary(hours)

    # Storage
    result["storage"] = db.get_storage_current()

    # Health
    result["health"] = db.get_latest_health()

    # Libraries
    result["libraries"] = db.get_libraries_current()

    return json.dumps(result, indent=2, default=str)


@mcp_app.tool()
def mediastack_stats() -> str:
    """Get MediaStack database statistics.

    Shows total events, active sources, last event time, and database size.
    """
    stats = db.get_stats()
    return json.dumps({
        "total_events": stats["total_events"],
        "active_sources": stats["active_sources"],
        "last_event": str(stats["last_event"]) if stats["last_event"] else None,
        "db_size": stats["db_size"],
    }, indent=2)


# -- Startup --

def _run_poller_thread(config: Config) -> None:
    """Run the poller in its own event loop in a daemon thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    poller = Poller(config)
    loop.run_until_complete(poller.start())


def main():
    import threading

    global _config, _poller

    _config = Config.from_env()
    logger.info("MediaStack starting. %s", _config.describe())

    db.init(_config.db_url)

    # Start poller in a daemon thread (gets its own event loop)
    poller_thread = threading.Thread(
        target=_run_poller_thread, args=(_config,), daemon=True, name="poller",
    )
    poller_thread.start()

    mcp_app.run(transport="streamable-http")


if __name__ == "__main__":
    main()
