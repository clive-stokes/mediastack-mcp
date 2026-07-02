"""MediaStack MCP Server — unified media stack observer and actor."""

import asyncio
import json
import logging
import re

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from app import db
from app.config import Config
from app.poller import Poller
from app.confirmations import store as confirmation_store, EXPIRY_SECONDS

# Monkey-patch: relax the SDK's Accept header validation.
#
# Why: the streamable-http transport 406s any POST whose Accept header does
# not include BOTH application/json and text/event-stream. mcp-remote (the
# bridge Claude Code/Desktop use) does not always send both, so without this
# patch those clients cannot connect.
#
# Verified against: mcp 1.27.0 (the version pinned in requirements.txt at the
# time of writing). _validate_accept_header is a private SDK internal — the
# pin in requirements.txt (<2.0) plus tests/test_sdk_patch.py guard against
# an upgrade renaming it silently. Upstream discussion of relaxing the
# validation: https://github.com/modelcontextprotocol/python-sdk/issues/609
# — remove this patch if the SDK stops requiring both Accept values.
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

# Build version — increment on each deploy to verify code is loaded
MEDIASTACK_BUILD = "2026-05-12.1"

# Global state
_config: Config | None = None
_poller: Poller | None = None
_poller_loop: asyncio.AbstractEventLoop | None = None


def _run_async(coro, timeout: int = 30):
    """Run an async coroutine from a sync MCP tool using the poller's event loop."""
    if _poller_loop and _poller_loop.is_running():
        future = asyncio.run_coroutine_threadsafe(coro, _poller_loop)
        return future.result(timeout=timeout)
    # Fallback: no poller loop yet (startup race) — run on a fresh loop.
    # asyncio.get_event_loop() raises RuntimeError here on Python >= 3.14.
    return asyncio.run(coro)


# -- Health endpoint --

@mcp_app.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    try:
        stats = await asyncio.to_thread(db.get_stats)
        return JSONResponse({
            "status": "ok",
            "build": MEDIASTACK_BUILD,
            "events": stats["total_events"],
            "sources": stats["active_sources"],
            "services": _config.describe() if _config else "not initialised",
        })
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=503)


# -- Ingest endpoint --

@mcp_app.custom_route("/ingest", methods=["POST"])
async def ingest(request: Request) -> JSONResponse:
    """Receive a download event from an external source (e.g. get_iplayer).

    Expected JSON body:
        source      (str, required) — originating service, e.g. "get_iplayer"
        event_type  (str, required) — e.g. "downloaded"
        title       (str, optional) — human-readable programme title
        metadata    (obj, optional) — arbitrary key/value pairs stored as JSONB

    get_iplayer substitution variables useful in metadata:
        pid, type, name, filename, dir, ext, episode, series

    Deduplication: if metadata contains a "pid" key, source_event_id is set to
    "{source}:{pid}" which maps to the unique index on media_events.source_event_id.
    Re-posting the same pid is silently ignored (returns duplicate: true).
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"status": "error", "detail": "invalid JSON body"},
            status_code=400,
        )

    source = body.get("source")
    event_type = body.get("event_type")

    if not source or not event_type:
        return JSONResponse(
            {"status": "error", "detail": "source and event_type are required"},
            status_code=400,
        )

    metadata = body.get("metadata") or {}
    pid = metadata.get("pid")

    # Build a deterministic dedup key from the pid when available.
    # get_iplayer's pid is stable and unique per BBC programme edition.
    source_event_id = f"{source}:{pid}" if pid else None

    event = {
        "source": source,
        "event_type": event_type,
        "title": body.get("title"),
        "metadata": metadata,
        "source_event_id": source_event_id,
    }

    inserted = await asyncio.to_thread(db.insert_event, event)
    logger.info(
        "Ingest: source=%s event_type=%s title=%r inserted=%s",
        source, event_type, body.get("title"), inserted,
    )
    return JSONResponse({
        "status": "ok",
        "inserted": inserted,
        "duplicate": not inserted,
    })


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
        libraries = [lib for lib in libraries if lib["source"] == source]
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
        if action.preview.get("action") in ("delete", "cancel_request", "delete_seerr_media"):
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
        # Execution failed — put the action back so the same ID can be retried
        # (get() removed it; without this a transient failure forces a re-preview).
        confirmation_store.reinstate(action)
        return json.dumps({
            "status": "error",
            "description": action.description,
            "error": str(e),
            "message": (
                "Execution failed; the action was NOT consumed. Retry with "
                f"mediastack_confirm('{action.confirmation_id}') once the cause is resolved."
            ),
        })


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


@mcp_app.tool()
def mediastack_prowlarr_search(
    query: str,
    indexers: list[str] | None = None,
    media_type: str | int | None = None,
    categories: list[int] | None = None,
    limit: int = 20,
) -> str:
    """Search Prowlarr's configured indexers (nzbgeek, drunkenslug, etc.).

    Unified search across every indexer Prowlarr manages — no need for
    per-indexer credentials here. Results are normalised across NZB and
    torrent sources.

    Args:
        query: Search term.
        indexers: Optional list of indexer names (case-insensitive). Defaults
            to all enabled indexers.
        media_type: Friendly label ("movie", "tv", "music", "book") resolved
            to Newznab category IDs via config, or a raw Newznab category
            number (e.g. 6000 for XXX, 2040 for Movies/HD). Numeric strings
            like "6000" are accepted.
        categories: Explicit Newznab category IDs; overrides media_type.
        limit: Max releases to return (default 20).
    """
    client = _get_poller_client("prowlarr")
    if not client:
        return json.dumps({"error": "prowlarr is not configured"})

    try:
        indexer_ids: list[int] | None = None
        if indexers:
            all_indexers = _run_async(client.get_indexers())
            wanted = {n.lower() for n in indexers}
            matches = [
                i for i in all_indexers
                if i.get("enable") and i.get("name", "").lower() in wanted
            ]
            if not matches:
                available = sorted(
                    i.get("name") for i in all_indexers if i.get("enable")
                )
                return json.dumps({
                    "error": f"No enabled Prowlarr indexer matched {indexers}",
                    "available": available,
                })
            indexer_ids = [i["id"] for i in matches]

        resolved_categories: list[int] | None = None
        if categories:
            resolved_categories = list(categories)
        elif media_type is not None:
            if isinstance(media_type, int):
                resolved_categories = [media_type]
            else:
                mt_str = str(media_type).strip()
                if mt_str.isdigit():
                    resolved_categories = [int(mt_str)]
                elif _config and mt_str.lower() in _config.prowlarr_categories:
                    resolved_categories = _config.prowlarr_categories[mt_str.lower()]
                else:
                    return json.dumps({
                        "error": f"Unknown media_type '{media_type}'. Use a label "
                                 f"(movie/tv/music/book) or a Newznab category number.",
                    })

        releases = _run_async(client.search(
            query,
            indexer_ids=indexer_ids,
            categories=resolved_categories,
        ))

        slim = []
        for r in releases[:limit]:
            entry = {
                "title": r.get("title"),
                "indexer": r.get("indexer"),
                "indexer_id": r.get("indexerId"),
                "guid": r.get("guid"),
                "size": _format_bytes(r.get("size") or 0),
                "categories": [
                    c.get("name") for c in (r.get("categories") or [])
                    if c.get("name")
                ],
            }
            if r.get("ageDays") is not None:
                entry["age_days"] = r["ageDays"]
            elif r.get("age") is not None:
                entry["age_days"] = r["age"]
            if r.get("seeders") is not None:
                entry["seeders"] = r["seeders"]
                entry["leechers"] = r.get("leechers")
            if r.get("grabs") is not None:
                entry["grabs"] = r["grabs"]
            if r.get("infoUrl"):
                entry["info_url"] = r["infoUrl"]
            slim.append(entry)

        return json.dumps(slim, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp_app.tool()
def mediastack_prowlarr_grab(guid: str, indexer_id: int, title: str) -> str:
    """Send a Prowlarr search result to the configured download client.

    Use the `guid` and `indexer_id` fields from a mediastack_prowlarr_search
    result. Returns a preview; requires mediastack_confirm to execute.

    Args:
        guid: Release GUID from the search result.
        indexer_id: Prowlarr indexer ID that produced the release.
        title: Release title (for confirmation display).
    """
    client = _get_poller_client("prowlarr")
    if not client:
        return json.dumps({"error": "prowlarr is not configured"})

    try:
        results = _run_async(client.get("/api/v1/search", params={
            "query": title,
            "type": "search",
            "indexerIds": indexer_id,
            "limit": 50,
        }))

        matching = next((r for r in results if r["guid"] == guid), None)
        if not matching:
            return json.dumps({"error": f"Release '{title}' not found in fresh search. It may have aged off the indexer."})

        download_url = matching.get("downloadUrl")
        if not download_url:
            return json.dumps({"error": f"No download URL found for '{title}'. Indexer configuration issue."})
    except Exception as e:
        return json.dumps({"error": f"Failed to fetch release details: {str(e)}"})

    preview = {
        "action": "prowlarr_grab",
        "title": title,
        "indexer_id": indexer_id,
        "guid": guid,
        "size": matching.get("size"),
    }
    desc = f"Grab '{title}' via Prowlarr (indexer {indexer_id})"

    async def execute():
        sabnzbd_client = _get_poller_client("sabnzbd")
        if not sabnzbd_client:
            raise ValueError("SABnzbd is not configured")

        return await sabnzbd_client.add_url(download_url)

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


@mcp_app.tool()
def mediastack_delete_seerr_media(media_id: int) -> str:
    """Delete a Seerr media tracking record. Returns a preview; requires confirmation.

    Removes Seerr's internal media entry so the title becomes re-requestable
    in the Seerr UI. This does NOT delete any files or touch Sonarr/Radarr —
    it only clears Seerr's memory that the title was ever requested or
    available. Useful after `mediastack_seerr_deletion_audit` identifies
    titles whose *arr files were deleted and you want Seerr to forget them.

    Args:
        media_id: Seerr's internal media id (not request_id, not tmdb_id).
                  Obtain via mediastack_seerr_deletion_audit.
    """
    client = _get_poller_client("seerr")
    if not client:
        return json.dumps({"error": "Seerr is not configured"})

    try:
        media = _run_async(client.get_media_by_id(media_id))
        media_type = media.get("mediaType", "unknown")
        title = media.get("title") or media.get("name") or f"media #{media_id}"
        status = media.get("status", 0)
        status_map = {1: "unknown", 2: "pending", 3: "processing", 4: "partially_available", 5: "available"}
        status_label = status_map.get(status, f"status_{status}")

        preview = {
            "action": "delete_seerr_media",
            "service": "seerr",
            "title": title,
            "media_type": media_type,
            "media_id": media_id,
            "tmdb_id": media.get("tmdbId"),
            "tvdb_id": media.get("tvdbId"),
            "current_status": status_label,
            "warning": (
                f"This removes Seerr's tracking record for '{title}'. "
                "No files are deleted and Sonarr/Radarr are not affected. "
                "The title will become re-requestable in Seerr's UI."
            ),
        }
        description = f"Delete Seerr media record for '{title}' (media #{media_id}, {status_label})"

        async def execute():
            return await client.delete_media(media_id)

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
        logger.warning(
            "Collection existence pre-check failed; proceeding with create",
            exc_info=True,
        )

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
        logger.warning(
            "Playlist existence pre-check failed; proceeding with create",
            exc_info=True,
        )

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


# -- Phase 5: Filesystem Tools (orphan & upgrade detection) --


def _check_filesystem_enabled() -> str | None:
    """Return an error JSON string if filesystem scanning is disabled, else None."""
    if not _config or not _config.filesystem_scan_enabled:
        return json.dumps({
            "error": "Filesystem scanning is disabled. "
            "Set FILESYSTEM_SCAN_ENABLED=true in .docker.env to enable.",
        })
    return None


@mcp_app.tool()
def mediastack_filesystem_scan(
    path: str,
    pattern: str | None = None,
    recursive: bool = True,
    limit: int = 500,
) -> str:
    """Scan a directory and return file metadata.

    Walks the given path and returns a list of files with their name, size,
    modification date, and extension. Restricted to configured MEDIA_ROOTS
    for safety — will reject paths outside allowed roots.

    Args:
        path: Directory path to scan (must be under a configured MEDIA_ROOT)
        pattern: Optional glob pattern to filter filenames (e.g. "*.mp4", "*.mkv")
        recursive: Recurse into subdirectories (default True)
        limit: Maximum files to return (default 500, max 5000)
    """
    from app.filesystem import validate_scan_path, scan_directory

    disabled = _check_filesystem_enabled()
    if disabled:
        return disabled

    # Validate path against allowed roots
    error = validate_scan_path(path, _config.media_roots)
    if error:
        return json.dumps({"error": error})

    limit = min(limit, 5000)

    try:
        files = scan_directory(path, pattern=pattern, recursive=recursive, limit=limit)
        total_size = sum(f["size_bytes"] for f in files)
        return json.dumps({
            "path": path,
            "files": files,
            "summary": {
                "file_count": len(files),
                "total_size_bytes": total_size,
                "total_size": _format_bytes(total_size),
                "truncated": len(files) >= limit,
            },
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp_app.tool()
def mediastack_orphan_detect(
    scan_path: str,
    library_name: str | None = None,
    extensions: str | None = None,
    include_ghosts: bool = True,
) -> str:
    """Detect orphaned media files not tracked in Jellyfin.

    Compares files on disk against Jellyfin library membership. Files
    present on disk but absent from Jellyfin are 'orphans' — likely
    manually downloaded content (e.g. get_iplayer) that was removed
    from the library but not from disk.

    Optionally also detects 'ghost entries' — items in Jellyfin whose
    underlying files no longer exist on disk.

    Requires FILESYSTEM_SCAN_ENABLED=true and Jellyfin to be configured.

    Args:
        scan_path: Directory to scan for media files (must be under MEDIA_ROOTS)
        library_name: Compare against a specific Jellyfin library (optional — all libraries if omitted)
        extensions: Comma-separated file extensions to check (default: mp4,mkv,avi,ts,m4v,m4a,mp3,flac,ogg,opus)
        include_ghosts: Also detect Jellyfin items with missing files (default True)
    """
    from app.filesystem import (
        validate_scan_path, detect_orphans, detect_ghosts,
        build_jellyfin_path_set, PathMapper, DEFAULT_MEDIA_EXTENSIONS,
    )

    disabled = _check_filesystem_enabled()
    if disabled:
        return disabled

    # Validate scan path
    error = validate_scan_path(scan_path, _config.media_roots)
    if error:
        return json.dumps({"error": error})

    # Check Jellyfin is available
    client = _get_poller_client("jellyfin")
    if not client:
        return json.dumps({"error": "Jellyfin is not configured"})

    # Parse extensions
    if extensions:
        ext_set = frozenset(e.strip().lower().lstrip(".") for e in extensions.split(",") if e.strip())
    else:
        ext_set = DEFAULT_MEDIA_EXTENSIONS

    try:
        # Resolve library parent_id if a specific library was requested
        parent_id = None
        if library_name:
            libraries = _run_async(client.get_libraries_with_ids())
            matches = [lib for lib in libraries if lib["name"].lower() == library_name.lower()]
            if not matches:
                available = [lib["name"] for lib in libraries]
                return json.dumps({
                    "error": f"Library '{library_name}' not found",
                    "available_libraries": available,
                })
            parent_id = matches[0]["item_id"]

        # Fetch all Jellyfin items with file paths
        jellyfin_items = _run_async(client.get_library_items_with_paths(parent_id=parent_id))

        # Set up path mapper if configured
        path_mapper = None
        if _config.media_path_mappings:
            path_mapper = PathMapper(_config.media_path_mappings)

        # Build the set of known Jellyfin paths (translated to container-space)
        jellyfin_paths = build_jellyfin_path_set(jellyfin_items, path_mapper)

        # Detect orphans: files on disk not in Jellyfin
        orphaned_files = detect_orphans(scan_path, jellyfin_paths, extensions=ext_set)

        # Optionally detect ghosts: Jellyfin items with missing files
        ghost_entries = []
        if include_ghosts:
            ghost_entries = detect_ghosts(scan_path, jellyfin_items, path_mapper)

        orphan_size = sum(f["size_bytes"] for f in orphaned_files)

        result = {
            "orphaned_files": orphaned_files,
            "ghost_entries": ghost_entries,
            "summary": {
                "scan_path": scan_path,
                "library_filter": library_name,
                "extensions_checked": sorted(ext_set),
                "total_files_scanned": len(orphaned_files) + len(jellyfin_paths),
                "total_in_library": len(jellyfin_paths),
                "orphan_count": len(orphaned_files),
                "orphan_total_size_bytes": orphan_size,
                "orphan_total_size": _format_bytes(orphan_size),
                "ghost_count": len(ghost_entries),
            },
        }

        return json.dumps(result, indent=2)
    except Exception as e:
        logger.exception("Orphan detection failed")
        return json.dumps({"error": str(e)})


@mcp_app.tool()
def mediastack_upgrade_detect(
    source: str,
    days: int = 30,
    codec_filter: str | None = None,
) -> str:
    """Detect recent media upgrades by Sonarr or Radarr.

    Queries the *arr history API for upgrade events (where a file was
    replaced with a higher quality version). Shows old vs new quality,
    file sizes, and total space impact.

    Useful for tracking x264 → HEVC migrations, quality profile upgrades,
    or understanding disk usage changes from automated upgrades.

    Args:
        source: Service to query — "sonarr" or "radarr"
        days: Look-back period in days (default 30, max 365)
        codec_filter: Optional codec/quality substring filter (e.g. "hevc", "x265", "remux")
    """
    if source not in ("sonarr", "radarr"):
        return json.dumps({"error": "source must be 'sonarr' or 'radarr'"})

    client = _get_poller_client(source)
    if not client:
        return json.dumps({"error": f"{source} is not configured"})

    days = min(days, 365)

    try:
        from datetime import datetime, timedelta, timezone as tz

        cutoff = datetime.now(tz.utc) - timedelta(days=days)

        # Page through history to find upgrade events within the time window
        upgrades = []
        page = 1
        page_size = 100
        done = False

        while not done:
            raw_events = _run_async(client.get_history(page=page, page_size=page_size))
            if not raw_events:
                break

            for event in raw_events:
                event_date_str = event.get("date", "")
                if not event_date_str:
                    continue

                # Parse the date — Sonarr/Radarr use ISO 8601
                try:
                    event_date = datetime.fromisoformat(event_date_str.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue

                if event_date < cutoff:
                    done = True
                    break

                event_type = event.get("eventType", "").lower()

                # Upgrade events: episodeFileDeleted/movieFileDeleted with reason=upgrade,
                # or downloadFolderImported with isUpgrade flag
                is_upgrade = False
                data = event.get("data", {})

                if event_type in ("episodefiledeleted", "moviefiledeleted"):
                    if data.get("reason", "").lower() == "upgrade":
                        is_upgrade = True
                elif event_type in ("downloadfolderimported",):
                    # The droppedPath/importedPath data indicates an upgrade
                    # when there's size info from before
                    pass

                if not is_upgrade:
                    continue

                # Extract quality info
                quality = event.get("quality", {}).get("quality", {})
                quality_name = quality.get("name", "Unknown")

                # Apply codec filter if specified
                if codec_filter:
                    filter_lower = codec_filter.lower()
                    quality_lower = quality_name.lower()
                    source_title = (event.get("sourceTitle") or "").lower()
                    if filter_lower not in quality_lower and filter_lower not in source_title:
                        continue

                # Build upgrade entry
                if source == "sonarr":
                    series = event.get("series", {})
                    episode = event.get("episode", {})
                    season = episode.get("seasonNumber")
                    ep_num = episode.get("episodeNumber")
                    title = series.get("title", "Unknown")
                    if isinstance(season, int) and isinstance(ep_num, int):
                        title = f"{title} S{season:02d}E{ep_num:02d}"
                else:
                    movie = event.get("movie", {})
                    title = f"{movie.get('title', 'Unknown')} ({movie.get('year', '?')})"

                size_bytes = 0
                try:
                    size_bytes = int(data.get("size", 0))
                except (ValueError, TypeError):
                    pass

                upgrades.append({
                    "title": title,
                    "old_quality": quality_name,
                    "deleted_size_bytes": size_bytes,
                    "deleted_size": _format_bytes(size_bytes) if size_bytes else "Unknown",
                    "date": event_date.isoformat(),
                    "source_title": event.get("sourceTitle", ""),
                })

            page += 1
            # Safety: don't page indefinitely
            if page > 50:
                break

        total_reclaimed = sum(u["deleted_size_bytes"] for u in upgrades)

        result = {
            "upgrades": upgrades,
            "summary": {
                "source": source,
                "days_checked": days,
                "codec_filter": codec_filter,
                "total_upgrades": len(upgrades),
                "total_space_reclaimed_bytes": total_reclaimed,
                "total_space_reclaimed": _format_bytes(total_reclaimed),
            },
        }

        return json.dumps(result, indent=2)
    except Exception as e:
        logger.exception("Upgrade detection failed")
        return json.dumps({"error": str(e)})


@mcp_app.tool()
def mediastack_ghost_history(
    source: str = "get_iplayer",
    media_type: str | None = None,
    days: int = 365,
    classify: bool = True,
) -> str:
    """Find previously downloaded files that no longer exist on disk.

    Queries the event log for download events (e.g. from get_iplayer)
    and checks whether each file still exists at its original download
    path. Files that have moved are cross-referenced against Jellyfin
    to distinguish:

    - **relocated**: original path gone but content exists in Jellyfin
      under a different path (e.g. moved from /downloads/ to /media/Movies/)
    - **truly_deleted**: gone from disk AND not found in Jellyfin

    Truly deleted entries are grouped by show name for compact output.

    For get_iplayer events, includes BBC programme URLs for re-downloading.

    Requires FILESYSTEM_SCAN_ENABLED=true. Set classify=false to skip
    the Jellyfin cross-reference (faster, returns raw ghost list).

    Args:
        source: Event source to query (default "get_iplayer")
        media_type: Filter by metadata type field — "tv" or "radio" (optional)
        days: Look-back period in days (default 365)
        classify: Cross-reference against Jellyfin to classify relocated vs truly deleted (default True)
    """
    import os
    from app.filesystem import (
        JellyfinNameIndex, PathMapper, classify_ghost, group_truly_deleted,
    )

    disabled = _check_filesystem_enabled()
    if disabled:
        return disabled

    # Build metadata filters
    metadata_filters = {}
    if media_type:
        metadata_filters["type"] = media_type

    limit = _config.ghost_history_limit if _config else 500

    try:
        events = db.get_events_filtered(
            source=source,
            days=days,
            event_type="downloaded",
            metadata_filters=metadata_filters or None,
            limit=limit,
        )

        if not events:
            filter_desc = f" (type={media_type})" if media_type else ""
            return json.dumps({
                "message": f"No '{source}' download events found in the last {days} days{filter_desc}.",
            })

        ghosts = []
        still_exist = 0

        for event in events:
            meta = event.get("metadata", {})
            filename = meta.get("filename", "")
            directory = meta.get("dir", "")

            if not filename:
                continue

            # Construct full path from dir + filename
            if directory:
                full_path = os.path.join(directory, filename)
            else:
                full_path = filename

            if os.path.exists(full_path):
                still_exist += 1
                continue

            # Build BBC programme URL from pid
            pid = meta.get("pid", "")
            programme_type = meta.get("type", "tv")
            if pid:
                if programme_type == "radio":
                    bbc_url = f"https://www.bbc.co.uk/sounds/play/{pid}"
                else:
                    bbc_url = f"https://www.bbc.co.uk/iplayer/episode/{pid}"
            else:
                bbc_url = None

            ghosts.append({
                "title": event.get("title", "Unknown"),
                "name": meta.get("name"),
                "episode": meta.get("episode"),
                "series": meta.get("series"),
                "seriesnum": meta.get("seriesnum"),
                "episodenum": meta.get("episodenum"),
                "channel": meta.get("channel"),
                "categories": meta.get("categories"),
                "type": programme_type,
                "filename": filename,
                "directory": directory,
                "original_path": full_path,
                "bbc_url": bbc_url,
                "pid": pid,
                "downloaded_at": event.get("timestamp"),
            })

        # -- Classify against Jellyfin if requested --
        classify_error = None
        if classify and ghosts:
            client = _get_poller_client("jellyfin")
            if client:
                try:
                    # Determine item types to query based on media_type filter
                    if media_type == "radio":
                        item_types = "Audio,MusicAlbum"
                    else:
                        item_types = "Movie,Series,Episode,MusicVideo,Video"

                    # Collect unique show names from ghosts — search Jellyfin
                    # per title instead of fetching the entire library
                    raw_names = {
                        g.get("name") or g.get("title", "")
                        for g in ghosts
                    }
                    raw_names.discard("")

                    # Build search variants: strip ": Series N" / ": Specials"
                    # / ": 2025" suffixes since Jellyfin stores the bare name
                    _series_suffix = re.compile(
                        r":\s*(?:Series\s+\d+|Specials|\d{4})$", re.IGNORECASE,
                    )
                    search_names: set[str] = set()
                    for name in raw_names:
                        search_names.add(name)
                        stripped = _series_suffix.sub("", name)
                        if stripped != name:
                            search_names.add(stripped)

                    logger.info(
                        "Ghost classify: searching Jellyfin for %d unique titles "
                        "(%d raw, expanded with suffix variants)",
                        len(search_names), len(raw_names),
                    )

                    # Search Jellyfin for every unique title concurrently
                    # (bounded) — one bridge call instead of a serial
                    # round-trip per title.
                    async def _search_all(names: list[str]) -> list:
                        sem = asyncio.Semaphore(4)

                        async def _one(title: str):
                            async with sem:
                                return await client.search_items_with_paths(
                                    query=title,
                                    include_item_types=item_types,
                                    limit=50,
                                )

                        return await asyncio.gather(
                            *(_one(t) for t in names), return_exceptions=True,
                        )

                    ordered_names = list(search_names)
                    search_results = _run_async(
                        _search_all(ordered_names), timeout=120,
                    )

                    all_jf_items: list[dict] = []
                    seen_ids: set[str] = set()
                    for title, results in zip(ordered_names, search_results):
                        if isinstance(results, BaseException):
                            logger.warning(
                                "Jellyfin search failed for '%s': %s", title, results,
                            )
                            continue
                        for item in results:
                            iid = item.get("item_id")
                            if iid not in seen_ids:
                                seen_ids.add(iid)
                                all_jf_items.append(item)

                    logger.info(
                        "Ghost classify: found %d Jellyfin items from %d searches",
                        len(all_jf_items), len(search_names),
                    )

                    # Build name index and path mapper
                    jf_index = JellyfinNameIndex(all_jf_items)
                    path_mapper = None
                    if _config and _config.media_path_mappings:
                        path_mapper = PathMapper(_config.media_path_mappings)

                    # Classify each ghost
                    for ghost in ghosts:
                        classify_ghost(ghost, jf_index, path_mapper)

                except Exception as e:
                    classify_error = str(e)
                    logger.error("Jellyfin cross-reference failed: %s", classify_error)
                    logger.exception("Full traceback:")
                    # Fall through — ghosts won't have classification key
            else:
                classify_error = "Jellyfin not configured"
                logger.warning("Jellyfin not configured — skipping classification")

        # -- Build response --
        classified_ok = classify and ghosts and ghosts[0].get("classification")
        if classified_ok:
            relocated = [g for g in ghosts if g.get("classification") == "relocated"]
            truly_deleted = [g for g in ghosts if g.get("classification") == "truly_deleted"]

            # Group truly_deleted by show name for compact output
            truly_deleted_grouped = group_truly_deleted(truly_deleted)

            result = {
                "truly_deleted_grouped": truly_deleted_grouped,
                "relocated": [{
                    "title": g.get("title"),
                    "name": g.get("name"),
                    "episode": g.get("episode"),
                    "channel": g.get("channel"),
                    "original_path": g.get("original_path"),
                    "current_path": g.get("current_path"),
                    "jellyfin_item_id": g.get("jellyfin_item_id"),
                    "jellyfin_type": g.get("jellyfin_type"),
                } for g in relocated],
                "summary": {
                    "source": source,
                    "media_type_filter": media_type,
                    "days_checked": days,
                    "total_events_checked": len(events),
                    "still_on_disk": still_exist,
                    "truly_deleted_count": len(truly_deleted),
                    "relocated_count": len(relocated),
                },
            }
        else:
            # Unclassified fallback (classify=false or Jellyfin unavailable)
            result = {
                "ghost_files": ghosts,
                "summary": {
                    "source": source,
                    "media_type_filter": media_type,
                    "days_checked": days,
                    "total_events_checked": len(events),
                    "still_on_disk": still_exist,
                    "missing_from_disk": len(ghosts),
                },
            }

        # Always include build version so we can verify code is deployed
        result["summary"]["build"] = MEDIASTACK_BUILD

        # Surface classify errors so failures are visible in the response
        if classify_error:
            result["summary"]["classify_error"] = classify_error

        return json.dumps(result, indent=2)
    except Exception as e:
        logger.exception("Ghost history check failed")
        return json.dumps({"error": str(e)})


@mcp_app.tool()
def mediastack_seerr_deletion_audit(days: int = 30, media_type: str | None = None) -> str:
    """Audit Seerr media records that have dropped from 'available' status.

    Surfaces titles whose *arr file was deleted (e.g. by a Trakt-list
    "Remove and Delete" rule) and which Seerr noticed via webhook. Each
    result includes the TMDB/TVDB IDs needed to re-add via
    mediastack_add_content or mediastack_request_content.

    The 'recovered' flag is True if a later poll observed the media
    returning to status >= 4 (e.g. you re-downloaded it).

    Args:
        days: How many days back to scan (default 30, max 365)
        media_type: Filter to 'movie' or 'tv' (default: both)
    """
    if days < 1 or days > 365:
        return json.dumps({"error": "days must be between 1 and 365"})
    if media_type and media_type not in ("movie", "tv"):
        return json.dumps({"error": "media_type must be 'movie', 'tv', or omitted"})

    try:
        # Pull all unavailable events in the window.
        rows = db.get_seerr_unavailable_events(days, media_type)

        # Resolve current status via one live Seerr API call (so 'recovered'
        # reflects right-now state, not the value frozen at drop time).
        out: list[dict] = []
        media_ids = [r["metadata"].get("media_id") for r in rows if r["metadata"].get("media_id")]
        client = _get_poller_client("seerr")
        live_status: dict[int, int] = {}
        if client and media_ids:
            try:
                current = _run_async(client.get_all_media())
                for m in current:
                    if m.get("id") in media_ids:
                        live_status[m["id"]] = m.get("status", 0)
            except Exception as e:
                logger.warning("seerr_deletion_audit: live status lookup failed: %s", e)

        for r in rows:
            meta = r["metadata"] or {}
            mid = meta.get("media_id")
            current_status = live_status.get(mid, meta.get("current_status"))
            recovered = bool(current_status and current_status >= 4)
            out.append({
                "media_id": mid,
                "title": r["title"],
                "media_type": meta.get("media_type"),
                "tmdb_id": meta.get("tmdb_id"),
                "tvdb_id": meta.get("tvdb_id"),
                "imdb_id": meta.get("imdb_id"),
                "dropped_at": r["timestamp"].isoformat(),
                "previous_status": meta.get("previous_status"),
                "current_status": current_status,
                "recovered": recovered,
            })

        return json.dumps({
            "count": len(out),
            "window_days": days,
            "results": out,
        }, indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


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
    import os
    import threading

    import uvicorn

    from app.auth import BearerAuthMiddleware

    global _config

    _config = Config.from_env()
    logger.info("MediaStack starting. %s", _config.describe())

    db.init(_config.db_url)

    # Start poller in a daemon thread (gets its own event loop)
    poller_thread = threading.Thread(
        target=_run_poller_thread, args=(_config,), daemon=True, name="poller",
    )
    poller_thread.start()

    # Build the Starlette app ourselves (equivalent to
    # mcp_app.run(transport="streamable-http")) so middleware can be attached.
    app = mcp_app.streamable_http_app()

    auth_token = os.environ.get("MEDIASTACK_AUTH_TOKEN", "").strip()
    if auth_token:
        app.add_middleware(BearerAuthMiddleware, token=auth_token)
        logger.info("Bearer auth enabled for /mcp and /ingest")
    else:
        logger.info("MEDIASTACK_AUTH_TOKEN not set — endpoints are unauthenticated")

    try:
        uvicorn.run(app, host=mcp_app.settings.host, port=mcp_app.settings.port)
    finally:
        # Graceful shutdown: stop polling loops and close persistent HTTP clients
        if _poller and _poller_loop and _poller_loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    _poller.stop(), _poller_loop,
                ).result(timeout=10)
            except Exception:
                logger.warning("Poller shutdown did not complete cleanly", exc_info=True)


if __name__ == "__main__":
    main()
