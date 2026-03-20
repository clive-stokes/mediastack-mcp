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
from app.confirmations import store as confirmation_store

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
_poller_loop: asyncio.AbstractEventLoop | None = None


def _run_async(coro):
    """Run an async coroutine from a sync MCP tool using the poller's event loop."""
    if _poller_loop and _poller_loop.is_running():
        future = asyncio.run_coroutine_threadsafe(coro, _poller_loop)
        return future.result(timeout=30)
    # Fallback: create a new event loop
    return asyncio.get_event_loop().run_until_complete(coro)


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


# -- Phase 3: Write Operations --

def _get_poller_client(name: str):
    """Get a client from the poller instance."""
    if _poller and hasattr(_poller, '_clients'):
        return _poller._clients.get(name)
    return None


@mcp_app.tool()
def mediastack_confirm(confirmation_id: str) -> str:
    """Execute a previously previewed write action.

    All write operations (add content, request, search missing, etc.)
    return a confirmation_id. Call this tool with that ID to execute
    the action. Unconfirmed actions expire after 5 minutes.

    Args:
        confirmation_id: The ID returned by a write operation preview
    """
    action = confirmation_store.get(confirmation_id)
    if not action:
        return json.dumps({"error": "Confirmation not found or expired. Please re-run the original command."})

    try:
        result = _run_async(action.execute_fn())
        # Record write event in media_events for audit trail
        db.insert_event({
            "source": "mediastack",
            "event_type": "write_confirmed",
            "title": action.description,
            "metadata": {"action_preview": action.preview, "result": str(result)[:500]},
            "source_event_id": f"mediastack_confirm_{action.confirmation_id}",
        })
        return json.dumps({"status": "success", "description": action.description, "result": result}, indent=2, default=str)
    except Exception as e:
        return json.dumps({"status": "error", "description": action.description, "error": str(e)})


@mcp_app.tool()
def mediastack_search_content(query: str, media_type: str) -> str:
    """Search for content to add to your library.

    Searches the appropriate *arr service lookup API for content not yet
    in your library. Use this before mediastack_add_content.

    Args:
        query: Search term (title)
        media_type: One of "tv", "movie", or "music"
    """
    service_map = {"tv": "sonarr", "movie": "radarr", "music": "lidarr"}
    service_name = service_map.get(media_type)
    if not service_name:
        return json.dumps({"error": f"Invalid media_type '{media_type}'. Use: tv, movie, or music"})

    client = _get_poller_client(service_name)
    if not client:
        return json.dumps({"error": f"{service_name} is not configured"})

    try:
        results = _run_async(client.lookup(query))
        # Slim down results for readability
        slim = []
        for r in results[:15]:
            entry = {"title": r.get("title") or r.get("artistName", "Unknown")}
            if "year" in r:
                entry["year"] = r["year"]
            if "tvdbId" in r:
                entry["tvdb_id"] = r["tvdbId"]
            if "tmdbId" in r:
                entry["tmdb_id"] = r["tmdbId"]
            if "foreignArtistId" in r:
                entry["foreign_artist_id"] = r["foreignArtistId"]
            if "overview" in r:
                entry["overview"] = r["overview"][:150]
            entry["in_library"] = bool(r.get("id") or r.get("path"))
            slim.append(entry)
        return json.dumps(slim, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp_app.tool()
def mediastack_add_content(
    media_type: str,
    external_id: str,
    title: str,
    quality_profile: str | None = None,
    root_folder: str | None = None,
) -> str:
    """Add content to an *arr library. Returns a preview; requires confirmation.

    Args:
        media_type: "tv", "movie", or "music"
        external_id: tvdbId (tv), tmdbId (movie), or foreignArtistId (music)
        title: Title for confirmation display
        quality_profile: Quality profile name (uses default if omitted)
        root_folder: Root folder path (uses default if omitted)
    """
    service_map = {"tv": "sonarr", "movie": "radarr", "music": "lidarr"}
    service_name = service_map.get(media_type)
    if not service_name:
        return json.dumps({"error": f"Invalid media_type '{media_type}'"})

    client = _get_poller_client(service_name)
    if not client:
        return json.dumps({"error": f"{service_name} is not configured"})

    try:
        # Get profiles and root folders for defaults/validation
        profiles = _run_async(client.get_quality_profiles())
        folders = _run_async(client.get_root_folders())

        # Resolve quality profile
        profile_id = None
        profile_name = quality_profile
        if quality_profile:
            for p in profiles:
                if p["name"].lower() == quality_profile.lower():
                    profile_id = p["id"]
                    profile_name = p["name"]
                    break
            if not profile_id:
                return json.dumps({"error": f"Quality profile '{quality_profile}' not found",
                                   "available": [p["name"] for p in profiles]})
        else:
            profile_id = profiles[0]["id"] if profiles else None
            profile_name = profiles[0]["name"] if profiles else "default"

        # Resolve root folder
        folder_path = root_folder or (folders[0]["path"] if folders else None)
        if not folder_path:
            return json.dumps({"error": "No root folder available"})

        preview = {
            "action": "add",
            "service": service_name,
            "title": title,
            "media_type": media_type,
            "external_id": external_id,
            "quality_profile": profile_name,
            "root_folder": folder_path,
        }
        description = f"Add '{title}' to {service_name.title()} ({profile_name}, {folder_path})"

        async def execute():
            if media_type == "tv":
                return await client.add_series(int(external_id), title, profile_id, folder_path)
            elif media_type == "movie":
                return await client.add_movie(int(external_id), title, profile_id, folder_path)
            elif media_type == "music":
                return await client.add_artist(external_id, title, profile_id, folder_path)

        action = confirmation_store.create(description, preview, execute)
        return json.dumps({
            "preview": preview,
            "confirmation_id": action.confirmation_id,
            "message": f"{description}. Call mediastack_confirm('{action.confirmation_id}') to execute.",
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp_app.tool()
def mediastack_list_profiles(service: str) -> str:
    """List available quality profiles and root folders for a service.

    Helper for mediastack_add_content — shows what quality profiles
    and root folders are available.

    Args:
        service: "sonarr", "radarr", or "lidarr"
    """
    client = _get_poller_client(service)
    if not client:
        return json.dumps({"error": f"{service} is not configured"})

    try:
        profiles = _run_async(client.get_quality_profiles())
        folders = _run_async(client.get_root_folders())
        return json.dumps({
            "profiles": [{"id": p["id"], "name": p["name"]} for p in profiles],
            "root_folders": [{"path": f["path"], "free_space": f.get("freeSpace")} for f in folders],
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp_app.tool()
def mediastack_request_content(query: str, media_type: str) -> str:
    """Search and request content via Seerr. Returns a preview; requires confirmation.

    Searches Seerr and returns matching results. Select one to create a
    pending request for approval.

    Args:
        query: Search term
        media_type: "tv" or "movie"
    """
    client = _get_poller_client("seerr")
    if not client:
        return json.dumps({"error": "Seerr is not configured"})

    try:
        results = _run_async(client.search(query))
        if not results:
            return json.dumps({"results": [], "message": "No results found"})

        # Return search results for the agent to select from
        slim = []
        for r in results[:10]:
            entry = {
                "id": r.get("id"),
                "media_type": r.get("mediaType", media_type),
                "title": r.get("title") or r.get("name") or r.get("originalTitle", "Unknown"),
                "year": r.get("releaseDate", "")[:4] if r.get("releaseDate") else r.get("firstAirDate", "")[:4] if r.get("firstAirDate") else None,
                "overview": (r.get("overview") or "")[:150],
                "status": r.get("mediaInfo", {}).get("status") if r.get("mediaInfo") else None,
            }
            slim.append(entry)

        # If there's a clear top result, create a confirmation for it
        if slim:
            top = slim[0]
            top_id = top["id"]
            top_title = top["title"]
            mt = top.get("media_type", media_type)

            preview = {
                "action": "request",
                "service": "seerr",
                "title": top_title,
                "media_type": mt,
                "seerr_id": top_id,
            }
            description = f"Request '{top_title}' via Seerr ({mt})"

            async def execute():
                return await client.request_media(mt, top_id)

            action = confirmation_store.create(description, preview, execute)
            return json.dumps({
                "results": slim,
                "top_result_confirmation": {
                    "confirmation_id": action.confirmation_id,
                    "message": f"{description}. Call mediastack_confirm('{action.confirmation_id}') to request the top result.",
                },
            }, indent=2)

        return json.dumps({"results": slim}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp_app.tool()
def mediastack_search_missing(service: str, item_id: int, season: int | None = None) -> str:
    """Tell an *arr service to search for missing content. Returns a preview; requires confirmation.

    Args:
        service: "sonarr", "radarr", or "lidarr"
        item_id: Series ID (sonarr), Movie ID (radarr), or Artist ID (lidarr)
        season: Sonarr only — limit search to a specific season
    """
    client = _get_poller_client(service)
    if not client:
        return json.dumps({"error": f"{service} is not configured"})

    preview = {
        "action": "search_missing",
        "service": service,
        "item_id": item_id,
        "season": season,
    }

    if service == "sonarr":
        desc = f"Search missing episodes for series {item_id} on Sonarr"
        if season is not None:
            desc += f" (season {season})"

        async def execute():
            return await client.search_missing_episodes(item_id, season)
    elif service == "radarr":
        desc = f"Search for movie {item_id} on Radarr"

        async def execute():
            return await client.search_movie(item_id)
    elif service == "lidarr":
        desc = f"Search missing albums for artist {item_id} on Lidarr"

        async def execute():
            return await client.search_missing(item_id)
    else:
        return json.dumps({"error": f"Unsupported service '{service}'"})

    action = confirmation_store.create(desc, preview, execute)
    return json.dumps({
        "preview": preview,
        "confirmation_id": action.confirmation_id,
        "message": f"{desc}. Call mediastack_confirm('{action.confirmation_id}') to execute.",
    }, indent=2)


@mcp_app.tool()
def mediastack_search_subtitles(
    media_type: str,
    item_id: int,
    language: str | None = None,
) -> str:
    """Tell Bazarr to search for subtitles. Returns a preview; requires confirmation.

    Args:
        media_type: "movie" or "episode"
        item_id: Radarr movie ID or Sonarr episode ID
        language: Language code (e.g. "en", "nl"). Uses Bazarr defaults if omitted.
    """
    client = _get_poller_client("bazarr")
    if not client:
        return json.dumps({"error": "Bazarr is not configured"})

    lang_desc = f" ({language})" if language else ""
    desc = f"Search subtitles for {media_type} {item_id}{lang_desc} via Bazarr"

    preview = {
        "action": "search_subtitles",
        "media_type": media_type,
        "item_id": item_id,
        "language": language,
    }

    async def execute():
        if media_type == "movie":
            return await client.search_subtitles_movie(item_id, language)
        else:
            return await client.search_subtitles_episode(item_id, language)

    action = confirmation_store.create(desc, preview, execute)
    return json.dumps({
        "preview": preview,
        "confirmation_id": action.confirmation_id,
        "message": f"{desc}. Call mediastack_confirm('{action.confirmation_id}') to execute.",
    }, indent=2)


# -- Startup --

def _run_poller_thread(config: Config) -> None:
    """Run the poller in its own event loop in a daemon thread."""
    global _poller_loop, _poller
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _poller_loop = loop
    _poller = Poller(config)
    loop.run_until_complete(_poller.start())


def main():
    import threading

    global _config

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
