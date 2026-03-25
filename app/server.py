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
from app.confirmations import store as confirmation_store, EXPIRY_SECONDS

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
    the action. Unconfirmed actions expire after 5 minutes (2 for file
    deletion).

    If the confirmation has expired, this tool will automatically
    re-issue the same preview with a fresh confirmation_id — no need
    to re-run the original command.

    Args:
        confirmation_id: The ID returned by a write operation preview
    """
    action = confirmation_store.get(confirmation_id)
    if not action:
        # Check if it expired — if so, re-issue with a fresh ID
        expired_action = confirmation_store.get_expired(confirmation_id)
        if expired_action:
            renewed = confirmation_store.renew(expired_action)
            return json.dumps({
                "status": "renewed",
                "message": (
                    f"Previous confirmation expired. Re-issued with fresh ID. "
                    f"Call mediastack_confirm('{renewed.confirmation_id}') to execute."
                ),
                "confirmation_id": renewed.confirmation_id,
                "description": renewed.description,
                "preview": renewed.preview,
            }, indent=2, default=str)
        return json.dumps({"error": "Confirmation not found. Please re-run the original command."})

    try:
        result = _run_async(action.execute_fn())
        # Use specific event_type for deletions vs other writes
        event_type = "write_confirmed"
        if action.preview.get("action") in ("delete", "cancel_request"):
            event_type = "delete_confirmed"
        # Record write event in media_events for audit trail
        db.insert_event({
            "source": "mediastack",
            "event_type": event_type,
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


DELETE_FILES_EXPIRY = 120  # 2-minute expiry when files will be deleted


def _format_bytes(size_bytes: int) -> str:
    """Format bytes into human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"


@mcp_app.tool()
def mediastack_delete_content(
    media_type: str,
    item_id: int,
    delete_files: bool = False,
) -> str:
    """Remove content from an *arr library. Returns a preview; requires confirmation.

    Removes a single series, movie, or artist from the corresponding *arr service.
    By default, only the library entry is removed — files on disk are kept.
    Set delete_files=True to also permanently delete media files from disk.

    IMPORTANT: File deletion is irreversible. The preview will show exactly
    what will be affected. Confirm only when you are certain.

    Args:
        media_type: "tv" (Sonarr series), "movie" (Radarr movie), or "music" (Lidarr artist)
        item_id: The *arr internal ID (series ID, movie ID, or artist ID)
        delete_files: If True, permanently delete media files from disk (default False)
    """
    service_map = {"tv": "sonarr", "movie": "radarr", "music": "lidarr"}
    service_name = service_map.get(media_type)
    if not service_name:
        return json.dumps({"error": f"Invalid media_type '{media_type}'. Use: tv, movie, or music"})

    client = _get_poller_client(service_name)
    if not client:
        return json.dumps({"error": f"{service_name} is not configured"})

    try:
        # Fetch full item details for the preview
        if media_type == "tv":
            item = _run_async(client.get_series_by_id(item_id))
            title = item.get("title", "Unknown")
            size_on_disk = item.get("statistics", {}).get("sizeOnDisk", 0)
            path = item.get("path", "Unknown")
            external_id_key = "tvdbId"
            external_id = item.get("tvdbId")
            quality_profile_id = item.get("qualityProfileId")
        elif media_type == "movie":
            item = _run_async(client.get_movie_by_id(item_id))
            title = f"{item.get('title', 'Unknown')} ({item.get('year', '?')})"
            size_on_disk = item.get("sizeOnDisk", 0)
            path = item.get("path", "Unknown")
            external_id_key = "tmdbId"
            external_id = item.get("tmdbId")
            quality_profile_id = item.get("qualityProfileId")
        else:  # music
            item = _run_async(client.get_artist_by_id(item_id))
            title = item.get("artistName", "Unknown")
            size_on_disk = item.get("statistics", {}).get("sizeOnDisk", 0)
            path = item.get("path", "Unknown")
            external_id_key = "foreignArtistId"
            external_id = item.get("foreignArtistId")
            quality_profile_id = item.get("qualityProfileId")

        size_str = _format_bytes(size_on_disk) if size_on_disk else "0 B"

        if delete_files:
            warning = (
                f"THIS WILL PERMANENTLY DELETE {size_str} OF FILES FROM DISK. "
                f"Path: {path}. This cannot be undone."
            )
        else:
            warning = (
                f"Content will be removed from the {service_name.title()} library. "
                f"Files on disk ({size_str} at {path}) will NOT be deleted."
            )

        preview = {
            "action": "delete",
            "service": service_name,
            "title": title,
            "media_type": media_type,
            "item_id": item_id,
            "delete_files": delete_files,
            "size_on_disk": size_str,
            "path": path,
            external_id_key: external_id,
            "quality_profile_id": quality_profile_id,
            "warning": warning,
        }

        files_label = " AND DELETE FILES" if delete_files else ""
        description = f"Remove '{title}' from {service_name.title()}{files_label}"

        async def execute():
            if media_type == "tv":
                return await client.delete_series(item_id, delete_files)
            elif media_type == "movie":
                return await client.delete_movie(item_id, delete_files)
            else:
                return await client.delete_artist(item_id, delete_files)

        # Shortened expiry for file deletion
        expiry = DELETE_FILES_EXPIRY if delete_files else EXPIRY_SECONDS
        action = confirmation_store.create(description, preview, execute, expiry_seconds=expiry)

        msg = f"{description}. Call mediastack_confirm('{action.confirmation_id}') to execute."
        if delete_files:
            msg += f" WARNING: Files will be permanently deleted. Confirmation expires in {expiry} seconds."

        return json.dumps({
            "preview": preview,
            "confirmation_id": action.confirmation_id,
            "expires_in_seconds": expiry,
            "message": msg,
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp_app.tool()
def mediastack_cancel_request(request_id: int) -> str:
    """Cancel a Seerr media request. Returns a preview; requires confirmation.

    Removes a pending or approved request from Seerr. This does not affect
    any content already downloaded to *arr libraries.

    Args:
        request_id: The Seerr request ID
    """
    client = _get_poller_client("seerr")
    if not client:
        return json.dumps({"error": "Seerr is not configured"})

    try:
        request = _run_async(client.get_request_by_id(request_id))
        media = request.get("media", {})
        req_type = request.get("type", "unknown")
        status = request.get("status", 0)
        status_map = {1: "pending", 2: "approved", 3: "declined"}
        status_label = status_map.get(status, f"status_{status}")

        title = "Unknown"
        if media:
            if req_type == "movie":
                title = media.get("originalTitle") or media.get("title", "Unknown")
            elif req_type == "tv":
                title = media.get("name") or media.get("originalName", "Unknown")

        requested_by = request.get("requestedBy", {})
        user_name = requested_by.get("displayName") or requested_by.get("email", "unknown")

        preview = {
            "action": "cancel_request",
            "service": "seerr",
            "title": title,
            "media_type": req_type,
            "request_id": request_id,
            "status": status_label,
            "requested_by": user_name,
            "warning": f"This will cancel the {status_label} request for '{title}'.",
        }
        description = f"Cancel Seerr request for '{title}' (request #{request_id}, {status_label})"

        async def execute():
            return await client.delete_request(request_id)

        action = confirmation_store.create(description, preview, execute)
        return json.dumps({
            "preview": preview,
            "confirmation_id": action.confirmation_id,
            "message": f"{description}. Call mediastack_confirm('{action.confirmation_id}') to execute.",
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


# -- Jellyfin User Operations --


@mcp_app.tool()
def mediastack_jellyfin_search(query: str, limit: int = 20,
                                user_name: str | None = None) -> str:
    """Search the Jellyfin library by name.

    Returns matching items with name, year, type, overview, and current
    favourite/played status. Use the returned item_id for favourite,
    watched, collection, or playlist operations.

    Args:
        query: Search term — matched against item names
        limit: Maximum results to return (default 20)
        user_name: Jellyfin user name to query as (default: primary admin)
    """
    client = _get_poller_client("jellyfin")
    if not client:
        return json.dumps({"error": "Jellyfin is not configured"})

    try:
        results = _run_async(client.search_items(query, limit, user_name=user_name))
        if not results:
            return json.dumps({"results": [], "message": f"No items matching '{query}'"})
        return json.dumps({"results": results, "count": len(results)}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp_app.tool()
def mediastack_jellyfin_genres(
    media_type: str | None = None,
    genre: str | None = None,
    limit: int = 50,
    user_name: str | None = None,
) -> str:
    """List Jellyfin genres or browse items by genre.

    Two modes:
    - No genre: returns list of available genres
    - With genre: returns items in that genre

    Args:
        media_type: Filter by type — Movie, Series, Audio (optional)
        genre: Genre name to browse items for (optional)
        limit: Maximum items when browsing by genre (default 50)
        user_name: Jellyfin user name to query as (default: primary admin)
    """
    client = _get_poller_client("jellyfin")
    if not client:
        return json.dumps({"error": "Jellyfin is not configured"})

    try:
        if genre:
            items = _run_async(client.get_items_by_genre(genre, media_type, limit, user_name=user_name))
            return json.dumps({
                "genre": genre,
                "items": items,
                "count": len(items),
            }, indent=2)
        else:
            genres = _run_async(client.get_genres(media_type))
            return json.dumps({
                "genres": genres,
                "count": len(genres),
            }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp_app.tool()
def mediastack_jellyfin_favorites(
    media_type: str | None = None,
    limit: int = 50,
    user_name: str | None = None,
) -> str:
    """List a user's Jellyfin favourites.

    Args:
        media_type: Filter by type — Movie, Series, Audio (optional)
        limit: Maximum items to return (default 50)
        user_name: Jellyfin user name (default: primary admin). e.g. "Clive", "Pete"
    """
    client = _get_poller_client("jellyfin")
    if not client:
        return json.dumps({"error": "Jellyfin is not configured"})

    try:
        results = _run_async(client.get_favorites(media_type, limit, user_name=user_name))
        if not results:
            return json.dumps({"results": [], "message": "No favourites found"})
        return json.dumps({"results": results, "count": len(results)}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp_app.tool()
def mediastack_jellyfin_favorite(
    item_id: str, item_name: str, favorite: bool = True,
    user_name: str | None = None,
) -> str:
    """Add or remove a Jellyfin item from favourites. Requires confirmation.

    Use mediastack_jellyfin_search to find the item_id first.

    Args:
        item_id: Jellyfin item ID (from search results)
        item_name: Item name (for confirmation display)
        favorite: True to add to favourites, False to remove (default True)
        user_name: Jellyfin user name (default: primary admin)
    """
    client = _get_poller_client("jellyfin")
    if not client:
        return json.dumps({"error": "Jellyfin is not configured"})

    action_label = "Add to" if favorite else "Remove from"
    description = f"{action_label} favourites: '{item_name}'"
    preview = {
        "action": "favorite" if favorite else "unfavorite",
        "service": "jellyfin",
        "item_id": item_id,
        "item_name": item_name,
        "favorite": favorite,
    }

    async def execute():
        return await client.set_favorite(item_id, favorite, user_name=user_name)

    action = confirmation_store.create(description, preview, execute)
    return json.dumps({
        "preview": preview,
        "confirmation_id": action.confirmation_id,
        "message": f"{description}. Call mediastack_confirm('{action.confirmation_id}') to execute.",
    }, indent=2)


@mcp_app.tool()
def mediastack_jellyfin_watched(
    item_id: str, item_name: str, played: bool = True,
    user_name: str | None = None,
) -> str:
    """Mark a Jellyfin item as played or unplayed. Requires confirmation.

    Use mediastack_jellyfin_search to find the item_id first.

    Args:
        item_id: Jellyfin item ID (from search results)
        item_name: Item name (for confirmation display)
        played: True to mark as played, False for unplayed (default True)
        user_name: Jellyfin user name (default: primary admin)
    """
    client = _get_poller_client("jellyfin")
    if not client:
        return json.dumps({"error": "Jellyfin is not configured"})

    action_label = "Mark as played" if played else "Mark as unplayed"
    description = f"{action_label}: '{item_name}'"
    preview = {
        "action": "mark_played" if played else "mark_unplayed",
        "service": "jellyfin",
        "item_id": item_id,
        "item_name": item_name,
        "played": played,
    }

    async def execute():
        return await client.set_played(item_id, played, user_name=user_name)

    action = confirmation_store.create(description, preview, execute)
    return json.dumps({
        "preview": preview,
        "confirmation_id": action.confirmation_id,
        "message": f"{description}. Call mediastack_confirm('{action.confirmation_id}') to execute.",
    }, indent=2)


@mcp_app.tool()
def mediastack_jellyfin_collections(
    collection_id: str | None = None,
    limit: int = 50,
) -> str:
    """List Jellyfin collections, or show items in a specific collection.

    Args:
        collection_id: If provided, list items in this collection. Otherwise list all collections.
        limit: Maximum collections to return (default 50)
    """
    client = _get_poller_client("jellyfin")
    if not client:
        return json.dumps({"error": "Jellyfin is not configured"})

    try:
        if collection_id:
            items = _run_async(client.get_collection_items(collection_id))
            return json.dumps({"items": items, "count": len(items)}, indent=2)
        else:
            collections = _run_async(client.get_collections(limit))
            if not collections:
                return json.dumps({"collections": [], "message": "No collections found"})
            return json.dumps({"collections": collections, "count": len(collections)}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp_app.tool()
def mediastack_jellyfin_playlists(
    playlist_id: str | None = None,
    limit: int = 50,
) -> str:
    """List Jellyfin playlists, or show items in a specific playlist.

    Args:
        playlist_id: If provided, list items in this playlist (ordered). Otherwise list all playlists.
        limit: Maximum playlists to return (default 50)
    """
    client = _get_poller_client("jellyfin")
    if not client:
        return json.dumps({"error": "Jellyfin is not configured"})

    try:
        if playlist_id:
            items = _run_async(client.get_playlist_items(playlist_id))
            return json.dumps({"items": items, "count": len(items)}, indent=2)
        else:
            playlists = _run_async(client.get_playlists(limit))
            if not playlists:
                return json.dumps({"playlists": [], "message": "No playlists found"})
            return json.dumps({"playlists": playlists, "count": len(playlists)}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp_app.tool()
def mediastack_jellyfin_collection_create(
    name: str, item_ids: str | None = None,
) -> str:
    """Create a new Jellyfin collection. Requires confirmation.

    IMPORTANT: To add items to an EXISTING collection, use
    mediastack_jellyfin_collection_modify instead.

    Args:
        name: Collection name
        item_ids: Comma-separated Jellyfin item IDs to include (optional)
    """
    client = _get_poller_client("jellyfin")
    if not client:
        return json.dumps({"error": "Jellyfin is not configured"})

    # Check for existing collection with the same name
    try:
        existing = _run_async(client.get_collections(limit=100))
        matches = [c for c in existing if c["name"].lower() == name.lower()]
        if matches:
            match = matches[0]
            return json.dumps({
                "warning": f"A collection named '{match['name']}' already exists.",
                "existing_collection_id": match["item_id"],
                "existing_item_count": match.get("child_count", 0),
                "suggestion": (
                    f"Use mediastack_jellyfin_collection_modify("
                    f"collection_id=\"{match['item_id']}\", "
                    f"collection_name=\"{match['name']}\", "
                    f"add_ids=\"...\") to add items to the existing collection."
                ),
            }, indent=2)
    except Exception:
        pass

    ids = [i.strip() for i in item_ids.split(",")] if item_ids else []
    description = f"Create collection '{name}'"
    if ids:
        description += f" with {len(ids)} item(s)"
    preview = {
        "action": "create_collection",
        "service": "jellyfin",
        "name": name,
        "item_count": len(ids),
    }

    async def execute():
        return await client.create_collection(name, ids or None)

    action = confirmation_store.create(description, preview, execute)
    return json.dumps({
        "preview": preview,
        "confirmation_id": action.confirmation_id,
        "message": f"{description}. Call mediastack_confirm('{action.confirmation_id}') to execute.",
    }, indent=2)


@mcp_app.tool()
def mediastack_jellyfin_collection_modify(
    collection_id: str,
    collection_name: str,
    add_ids: str | None = None,
    remove_ids: str | None = None,
) -> str:
    """Add or remove items from a Jellyfin collection. Requires confirmation.

    Args:
        collection_id: Jellyfin collection ID
        collection_name: Collection name (for confirmation display)
        add_ids: Comma-separated item IDs to add (optional)
        remove_ids: Comma-separated item IDs to remove (optional)
    """
    client = _get_poller_client("jellyfin")
    if not client:
        return json.dumps({"error": "Jellyfin is not configured"})

    adds = [i.strip() for i in add_ids.split(",")] if add_ids else []
    removes = [i.strip() for i in remove_ids.split(",")] if remove_ids else []

    if not adds and not removes:
        return json.dumps({"error": "Provide add_ids or remove_ids (or both)"})

    parts = []
    if adds:
        parts.append(f"add {len(adds)}")
    if removes:
        parts.append(f"remove {len(removes)}")
    description = f"Modify collection '{collection_name}': {' and '.join(parts)} item(s)"

    preview = {
        "action": "modify_collection",
        "service": "jellyfin",
        "collection_id": collection_id,
        "collection_name": collection_name,
        "adding": len(adds),
        "removing": len(removes),
    }

    async def execute():
        results = {}
        if adds:
            results["added"] = await client.add_to_collection(collection_id, adds)
        if removes:
            results["removed"] = await client.remove_from_collection(collection_id, removes)
        return results

    action = confirmation_store.create(description, preview, execute)
    return json.dumps({
        "preview": preview,
        "confirmation_id": action.confirmation_id,
        "message": f"{description}. Call mediastack_confirm('{action.confirmation_id}') to execute.",
    }, indent=2)


@mcp_app.tool()
def mediastack_jellyfin_playlist_create(
    name: str,
    item_ids: str | None = None,
    media_type: str = "Video",
) -> str:
    """Create a new Jellyfin playlist. Requires confirmation.

    IMPORTANT: To add items to an EXISTING playlist, use
    mediastack_jellyfin_playlist_modify instead. This tool creates
    a new playlist — duplicates will result if a playlist with the
    same name already exists.

    Args:
        name: Playlist name
        item_ids: Comma-separated Jellyfin item IDs to include (optional)
        media_type: Video or Audio (default Video)
    """
    client = _get_poller_client("jellyfin")
    if not client:
        return json.dumps({"error": "Jellyfin is not configured"})

    # Check for existing playlist with the same name
    try:
        existing = _run_async(client.get_playlists(limit=100))
        matches = [p for p in existing if p["name"].lower() == name.lower()]
        if matches:
            match = matches[0]
            return json.dumps({
                "warning": f"A playlist named '{match['name']}' already exists.",
                "existing_playlist_id": match["item_id"],
                "existing_item_count": match.get("child_count", 0),
                "suggestion": (
                    f"Use mediastack_jellyfin_playlist_modify("
                    f"playlist_id=\"{match['item_id']}\", "
                    f"playlist_name=\"{match['name']}\", "
                    f"add_ids=\"...\") to add items to the existing playlist."
                ),
            }, indent=2)
    except Exception:
        pass  # If the check fails, proceed with creation anyway

    ids = [i.strip() for i in item_ids.split(",")] if item_ids else []
    description = f"Create {media_type.lower()} playlist '{name}'"
    if ids:
        description += f" with {len(ids)} item(s)"
    preview = {
        "action": "create_playlist",
        "service": "jellyfin",
        "name": name,
        "media_type": media_type,
        "item_count": len(ids),
    }

    async def execute():
        return await client.create_playlist(name, ids or None, media_type)

    action = confirmation_store.create(description, preview, execute)
    return json.dumps({
        "preview": preview,
        "confirmation_id": action.confirmation_id,
        "message": f"{description}. Call mediastack_confirm('{action.confirmation_id}') to execute.",
    }, indent=2)


@mcp_app.tool()
def mediastack_jellyfin_playlist_modify(
    playlist_id: str,
    playlist_name: str,
    add_ids: str | None = None,
    remove_ids: str | None = None,
    move_item_id: str | None = None,
    move_to_index: int | None = None,
) -> str:
    """Add, remove, or reorder items in a Jellyfin playlist. Requires confirmation.

    Args:
        playlist_id: Jellyfin playlist ID
        playlist_name: Playlist name (for confirmation display)
        add_ids: Comma-separated item IDs to add (optional)
        remove_ids: Comma-separated item IDs to remove (optional)
        move_item_id: Item ID to move (optional, requires move_to_index)
        move_to_index: New position for the item, zero-based (optional)
    """
    client = _get_poller_client("jellyfin")
    if not client:
        return json.dumps({"error": "Jellyfin is not configured"})

    adds = [i.strip() for i in add_ids.split(",")] if add_ids else []
    removes = [i.strip() for i in remove_ids.split(",")] if remove_ids else []
    has_move = move_item_id is not None and move_to_index is not None

    if not adds and not removes and not has_move:
        return json.dumps({"error": "Provide add_ids, remove_ids, or move_item_id + move_to_index"})

    parts = []
    if adds:
        parts.append(f"add {len(adds)}")
    if removes:
        parts.append(f"remove {len(removes)}")
    if has_move:
        parts.append(f"move 1 item to position {move_to_index}")
    description = f"Modify playlist '{playlist_name}': {', '.join(parts)}"

    preview = {
        "action": "modify_playlist",
        "service": "jellyfin",
        "playlist_id": playlist_id,
        "playlist_name": playlist_name,
        "adding": len(adds),
        "removing": len(removes),
        "moving": has_move,
    }

    async def execute():
        results = {}
        if adds:
            results["added"] = await client.add_to_playlist(playlist_id, adds)
        if removes:
            results["removed"] = await client.remove_from_playlist(playlist_id, removes)
        if has_move:
            results["moved"] = await client.move_playlist_item(
                playlist_id, move_item_id, move_to_index,
            )
        return results

    action = confirmation_store.create(description, preview, execute)
    return json.dumps({
        "preview": preview,
        "confirmation_id": action.confirmation_id,
        "message": f"{description}. Call mediastack_confirm('{action.confirmation_id}') to execute.",
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
