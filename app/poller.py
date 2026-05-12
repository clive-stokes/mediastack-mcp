"""Background polling engine for MediaStack.

Periodically polls configured services, collects events/storage/health/libraries,
and writes them to the database. Designed to run as an asyncio background task.
"""

import asyncio
import logging
from datetime import datetime, timezone

from app import db
from app.config import Config
from app.clients.sonarr import SonarrClient
from app.clients.radarr import RadarrClient
from app.clients.lidarr import LidarrClient
from app.clients.prowlarr import ProwlarrClient
from app.clients.bazarr import BazarrClient
from app.clients.jellyfin import JellyfinClient
from app.clients.seerr import SeerrClient
from app.clients.sabnzbd import SabnzbdClient
from app.clients.qbittorrent import QBittorrentClient
from app.clients.audiobookshelf import AudiobookshelfClient
from app.clients.boxarr import BoxarrClient
from app.clients.dispatcharr import DispatcharrClient
from app.clients.suggestarr import SuggestarrClient

logger = logging.getLogger(__name__)


class Poller:
    """Background polling engine."""

    def __init__(self, config: Config):
        self.config = config
        self._clients: dict = {}
        self._qbt_prev_torrents: list[dict] = []
        self._seerr_prev_media_status: dict[int, int] = {}
        self._running = False

        self._init_clients()

    def _init_clients(self) -> None:
        """Create client instances for each discovered service."""
        for name, svc in self.config.arr_services.items():
            if name == "sonarr":
                self._clients["sonarr"] = SonarrClient(svc.name, svc.url, svc.api_key)
            elif name == "radarr":
                self._clients["radarr"] = RadarrClient(svc.name, svc.url, svc.api_key)
            elif name == "lidarr":
                self._clients["lidarr"] = LidarrClient(svc.name, svc.url, svc.api_key)
            elif name == "prowlarr":
                self._clients["prowlarr"] = ProwlarrClient(svc.name, svc.url, svc.api_key)
            elif name == "bazarr":
                self._clients["bazarr"] = BazarrClient(svc.url, svc.api_key)
            elif name == "jellyfin":
                import os
                jf_user = os.environ.get("JELLYFIN_USER_ID") or None
                self._clients["jellyfin"] = JellyfinClient(svc.url, svc.api_key, user_id=jf_user)
            elif name == "seerr":
                self._clients["seerr"] = SeerrClient(svc.url, svc.api_key)
            elif name == "audiobookshelf":
                self._clients["audiobookshelf"] = AudiobookshelfClient(svc.url, svc.api_key)

        for name, svc in self.config.credential_services.items():
            if name == "sabnzbd":
                self._clients["sabnzbd"] = SabnzbdClient(svc.url, svc.password)
            elif name == "qbittorrent":
                self._clients["qbittorrent"] = QBittorrentClient(svc.url, svc.username, svc.password)
            elif name == "boxarr":
                self._clients["boxarr"] = BoxarrClient(svc.url)
            elif name == "suggestarr":
                self._clients["suggestarr"] = SuggestarrClient(svc.url, svc.username, svc.password)
            elif name == "dispatcharr":
                self._clients["dispatcharr"] = DispatcharrClient(svc.url, svc.username, svc.password)

    async def start(self) -> None:
        """Start all polling loops."""
        self._running = True
        logger.info("Poller starting. %s", self.config.describe())

        tasks = [
            asyncio.create_task(self._poll_events_loop()),
            asyncio.create_task(self._poll_storage_loop()),
            asyncio.create_task(self._poll_health_loop()),
            asyncio.create_task(self._poll_libraries_loop()),
            asyncio.create_task(self._retention_loop()),
        ]
        await asyncio.gather(*tasks)

    async def stop(self) -> None:
        self._running = False

    # -- Event polling --

    async def _poll_events_loop(self) -> None:
        while self._running:
            try:
                await self._poll_events()
            except Exception:
                logger.exception("Error in event polling cycle")
            await asyncio.sleep(self.config.poll_interval)

    async def _poll_events(self) -> None:
        """Single pass: collect events from all configured sources."""
        # Sonarr / Radarr / Lidarr (common *arr history pattern)
        for name in ("sonarr", "radarr", "lidarr"):
            if name in self._clients:
                await self._poll_arr_history(name)

        # SABnzbd
        if "sabnzbd" in self._clients:
            await self._poll_sabnzbd()

        # qBittorrent
        if "qbittorrent" in self._clients:
            await self._poll_qbittorrent()

        # Bazarr (subtitle history)
        if "bazarr" in self._clients:
            await self._poll_bazarr()

        # Seerr (request pipeline)
        if "seerr" in self._clients:
            await self._poll_seerr()

        # Boxarr (box office additions)
        if "boxarr" in self._clients:
            await self._poll_boxarr()

        # Suggestarr (recommendation stats)
        if "suggestarr" in self._clients:
            await self._poll_suggestarr()

    async def _poll_arr_history(self, name: str) -> None:
        """Poll a Sonarr/Radarr/Lidarr-style history endpoint."""
        client = self._clients[name]
        try:
            raw_events = await client.get_history()
            events = [client.parse_history_event(e) for e in raw_events]
            inserted = db.insert_events(events)
            if inserted:
                logger.info("[%s] Recorded %d new events", name, inserted)
        except Exception:
            logger.exception("[%s] Failed to poll history", name)

    async def _poll_sabnzbd(self) -> None:
        client: SabnzbdClient = self._clients["sabnzbd"]
        try:
            slots = await client.get_history()
            events = [client.parse_history_event(s) for s in slots]
            inserted = db.insert_events(events)
            if inserted:
                logger.info("[sabnzbd] Recorded %d new events", inserted)
        except Exception:
            logger.exception("[sabnzbd] Failed to poll history")

    async def _poll_qbittorrent(self) -> None:
        client: QBittorrentClient = self._clients["qbittorrent"]
        try:
            current = await client.get_torrents()
            if self._qbt_prev_torrents:
                events = client.parse_torrent_events(current, self._qbt_prev_torrents)
                now = datetime.now(timezone.utc).isoformat()
                for e in events:
                    if not e["timestamp"]:
                        e["timestamp"] = now
                inserted = db.insert_events(events)
                if inserted:
                    logger.info("[qbittorrent] Recorded %d new events", inserted)
            self._qbt_prev_torrents = current
        except Exception:
            logger.exception("[qbittorrent] Failed to poll")

    async def _poll_bazarr(self) -> None:
        client: BazarrClient = self._clients["bazarr"]
        try:
            # Episode subtitle history
            ep_data = await client.get_history()
            ep_events = ep_data.get("data", []) if isinstance(ep_data, dict) else []
            events = [client.parse_history_event(e, "episode") for e in ep_events]

            # Movie subtitle history
            mov_data = await client.get_movie_history()
            mov_events = mov_data.get("data", []) if isinstance(mov_data, dict) else []
            events.extend(client.parse_history_event(e, "movie") for e in mov_events)

            inserted = db.insert_events(events)
            if inserted:
                logger.info("[bazarr] Recorded %d new events", inserted)
        except Exception:
            logger.exception("[bazarr] Failed to poll history")

    async def _poll_seerr(self) -> None:
        client: SeerrClient = self._clients["seerr"]
        try:
            data = await client.get_requests()
            requests = data.get("results", []) if isinstance(data, dict) else []
            events = [client.parse_request_event(r) for r in requests]
            inserted = db.insert_events(events)
            if inserted:
                logger.info("[seerr] Recorded %d new events", inserted)
        except Exception:
            logger.exception("[seerr] Failed to poll requests")

        # Media availability diff — separate from request pipeline.
        await self._poll_seerr_media()

    async def _poll_seerr_media(self) -> None:
        """Snapshot Seerr's full media list and diff for availability drops.

        Emits `seerr_media_unavailable` events when media drops below status 4
        (a Radarr/Sonarr deletion that Seerr has noticed). The previous snapshot
        is in-memory only — first cycle after restart reseeds without emitting.
        """
        client: SeerrClient = self._clients["seerr"]
        try:
            current = await client.get_all_media()
            if self._seerr_prev_media_status:
                events, new_snapshot = client.parse_media_diff_events(
                    current, self._seerr_prev_media_status,
                )
                inserted = db.insert_events(events)
                if inserted:
                    logger.info("[seerr] Recorded %d media availability drops", inserted)
                self._seerr_prev_media_status = new_snapshot
            else:
                # First cycle — seed the snapshot, emit nothing.
                _, new_snapshot = client.parse_media_diff_events(current, {})
                self._seerr_prev_media_status = new_snapshot
                logger.info("[seerr] Seeded media snapshot with %d titles", len(new_snapshot))
        except Exception:
            logger.exception("[seerr] Failed to poll media list")

    async def _poll_boxarr(self) -> None:
        client: BoxarrClient = self._clients["boxarr"]
        try:
            history = await client.get_scheduler_history()
            events = []
            for run in (history if isinstance(history, list) else []):
                events.append({
                    "source": "boxarr",
                    "event_type": "scheduler_run",
                    "title": f"Box office scan: {run.get('movies_found', 0)} found, {run.get('movies_added', 0)} added",
                    "timestamp": run.get("timestamp"),
                    "source_event_id": f"boxarr_run_{run.get('timestamp', '')}",
                    "metadata": {
                        "movies_found": run.get("movies_found"),
                        "movies_added": run.get("movies_added"),
                        "week": run.get("week"),
                        "success": run.get("success"),
                    },
                })
            inserted = db.insert_events(events)
            if inserted:
                logger.info("[boxarr] Recorded %d new events", inserted)
        except Exception:
            logger.exception("[boxarr] Failed to poll")

    async def _poll_suggestarr(self) -> None:
        client: SuggestarrClient = self._clients["suggestarr"]
        try:
            stats = await client.get_request_stats()
            # Record stats as a periodic snapshot event
            db.insert_event({
                "source": "suggestarr",
                "event_type": "stats_snapshot",
                "title": f"Suggestions: {stats.get('total', 0)} total, {stats.get('today', 0)} today",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source_event_id": f"suggestarr_stats_{datetime.now(timezone.utc).strftime('%Y%m%d%H')}",
                "metadata": stats,
            })
        except Exception:
            logger.exception("[suggestarr] Failed to poll stats")

    # -- Storage polling --

    async def _poll_storage_loop(self) -> None:
        while self._running:
            try:
                await self._poll_storage()
            except Exception:
                logger.exception("Error in storage polling cycle")
            await asyncio.sleep(self.config.storage_interval)

    async def _poll_storage(self) -> None:
        """Collect storage snapshots from *arr root folder APIs + filesystem."""
        seen_mounts: set[str] = set()
        count = 0

        for name in ("sonarr", "radarr", "lidarr"):
            client = self._clients.get(name)
            if not client or not hasattr(client, "get_root_folders"):
                continue
            try:
                folders = await client.get_root_folders()
                for f in folders:
                    path = f.get("path", "")
                    if path in seen_mounts:
                        continue
                    seen_mounts.add(path)

                    fs = await self._get_fs_stats(path)
                    if fs:
                        total, used = fs
                        db.insert_storage_snapshot(path, total, used, f"{name}_rootfolder")
                        count += 1
                    elif f.get("totalSpace", 0) > 0:
                        total = f["totalSpace"]
                        free = f.get("freeSpace", 0)
                        db.insert_storage_snapshot(path, total, total - free, f"{name}_rootfolder")
                        count += 1
            except Exception:
                logger.exception("[%s] Failed to poll root folders", name)

        if count:
            logger.info("Recorded %d storage snapshots", count)

    async def _get_fs_stats(self, container_path: str) -> tuple[int, int] | None:
        """Get total/used bytes via os.statvfs on the mounted media volume."""
        import os
        if os.path.exists(container_path):
            try:
                st = os.statvfs(container_path)
                total = st.f_frsize * st.f_blocks
                used = st.f_frsize * (st.f_blocks - st.f_bfree)
                return total, used
            except OSError:
                pass
        return None

    async def _get_dir_size(self, path: str) -> int | None:
        """Get the actual bytes consumed by a directory using du.

        Uses `du -sb` (summarise, apparent bytes) via subprocess in a thread pool
        so the event loop is not blocked. Suitable for large media directories.

        Returns None if the path doesn't exist or du fails.
        """
        import os
        import subprocess

        if not os.path.exists(path):
            logger.debug("_get_dir_size: path does not exist: %s", path)
            return None

        def _run_du() -> int | None:
            try:
                result = subprocess.run(
                    ["du", "-sb", path],
                    capture_output=True,
                    text=True,
                    timeout=120,  # Large directories (e.g. Trash TV) may take time
                )
                if result.returncode == 0:
                    # Output format: "<bytes>\t<path>"
                    return int(result.stdout.split()[0])
                logger.warning("du failed for %s: %s", path, result.stderr.strip())
            except subprocess.TimeoutExpired:
                logger.warning("du timed out for %s", path)
            except (ValueError, IndexError, OSError) as e:
                logger.warning("_get_dir_size error for %s: %s", path, e)
            return None

        return await asyncio.to_thread(_run_du)

    # -- Library snapshot polling --

    async def _poll_libraries_loop(self) -> None:
        while self._running:
            try:
                await self._poll_libraries()
            except Exception:
                logger.exception("Error in library polling cycle")

            if self.config.library_cron:
                secs = self._seconds_until_cron(self.config.library_cron)
                logger.info(
                    "[libraries] Next poll at %s (%.0fs)", self.config.library_cron, secs
                )
                await asyncio.sleep(secs)
            else:
                await asyncio.sleep(self.config.library_interval)

    def _seconds_until_cron(self, hh_mm: str) -> float:
        """Return seconds until the next daily occurrence of HH:MM (container local time).

        If the target time has already passed today, schedules for tomorrow.
        Relies on the container TZ env var being set correctly (e.g. TZ=Europe/London).
        """
        from datetime import datetime, timedelta
        h, m = map(int, hh_mm.split(":"))
        now = datetime.now()
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    async def _poll_libraries(self) -> None:
        """Collect library size/count snapshots."""
        count = 0

        # Sonarr: series count + total size
        if "sonarr" in self._clients:
            try:
                series = await self._clients["sonarr"].get_series()
                total_size = sum(s.get("statistics", {}).get("sizeOnDisk", 0) for s in series)
                db.insert_library_snapshot("sonarr", "TV Series", len(series), total_size)
                count += 1
            except Exception:
                logger.exception("[sonarr] Failed to poll library stats")

        # Radarr: movie count + total size
        if "radarr" in self._clients:
            try:
                movies = await self._clients["radarr"].get_movies()
                total_size = sum(m.get("sizeOnDisk", 0) for m in movies)
                db.insert_library_snapshot("radarr", "Movies", len(movies), total_size)
                count += 1
            except Exception:
                logger.exception("[radarr] Failed to poll library stats")

        # Lidarr: artist count + total size
        if "lidarr" in self._clients:
            try:
                artists = await self._clients["lidarr"].get_artists()
                total_size = sum(a.get("statistics", {}).get("sizeOnDisk", 0) for a in artists)
                db.insert_library_snapshot("lidarr", "Music Artists", len(artists), total_size)
                count += 1
            except Exception:
                logger.exception("[lidarr] Failed to poll library stats")

        # Jellyfin: per-library item counts + disk usage for unmanaged libraries.
        #
        # Strategy:
        #   - STRM/iFiesta libraries  → size_bytes = NULL (negligible .strm files)
        #   - arr-managed libraries   → size_bytes = NULL (Sonarr/Radarr/Lidarr record
        #                               accurate sizes via their own API calls above)
        #   - Everything else         → size_bytes measured with `du -sb` on the library path
        #
        # "arr-managed" is determined by path overlap: if any of the library's Locations
        # match a root folder configured in Sonarr, Radarr, or Lidarr, the arr tool owns it.
        if "jellyfin" in self._clients:
            try:
                # Collect arr root folder paths so we can skip arr-managed Jellyfin libraries.
                arr_root_paths: set[str] = set()
                for arr_name in ("sonarr", "radarr", "lidarr"):
                    arr_client = self._clients.get(arr_name)
                    if arr_client and hasattr(arr_client, "get_root_folders"):
                        try:
                            folders = await arr_client.get_root_folders()
                            for f in folders:
                                p = f.get("path", "").rstrip("/")
                                if p:
                                    arr_root_paths.add(p)
                        except Exception:
                            logger.warning("[%s] Could not fetch root folders for arr-path check", arr_name)

                libraries = await self._clients["jellyfin"].get_libraries()
                for lib in libraries:
                    lib_name = lib.get("Name", "Unknown")
                    lib_id = lib.get("ItemId", "")
                    if not lib_id:
                        continue

                    item_count = await self._clients["jellyfin"].get_library_items_count(lib_id)

                    # STRM/IPTV libraries: negligible disk usage (.strm files are tiny text files)
                    is_strm = "strm" in lib_name.lower() or "ifiesta" in lib_name.lower()
                    if is_strm:
                        db.insert_library_snapshot("jellyfin", lib_name, item_count, None)
                        count += 1
                        continue

                    # Check if this library's path is owned by an arr tool.
                    # Locations is a list of filesystem paths from /Library/VirtualFolders.
                    locations = lib.get("Locations", [])
                    lib_paths = {loc.rstrip("/") for loc in locations}
                    if lib_paths & arr_root_paths:
                        # Arr already records an accurate size for this library — store NULL
                        # to avoid a misleading duplicate entry with a different value.
                        db.insert_library_snapshot("jellyfin", lib_name, item_count, None)
                        count += 1
                        continue

                    # Unmanaged library: measure actual bytes used on disk.
                    size: int | None = None
                    for loc in locations:
                        measured = await self._get_dir_size(loc)
                        if measured is not None:
                            size = (size or 0) + measured

                    db.insert_library_snapshot("jellyfin", lib_name, item_count, size)
                    count += 1
            except Exception:
                logger.exception("[jellyfin] Failed to poll library stats")

        # Audiobookshelf: per-library stats (requires separate stats endpoint)
        if "audiobookshelf" in self._clients:
            try:
                libraries = await self._clients["audiobookshelf"].get_libraries()
                for lib in libraries:
                    lib_name = lib.get("name", "Unknown")
                    lib_id = lib.get("id", "")
                    try:
                        stats = await self._clients["audiobookshelf"].get_library_stats(lib_id)
                    except Exception:
                        stats = {}
                    item_count = stats.get("totalItems", 0)
                    size = stats.get("totalSize", None)
                    db.insert_library_snapshot("audiobookshelf", lib_name, item_count, size)
                    count += 1
            except Exception:
                logger.exception("[audiobookshelf] Failed to poll library stats")

        # Dispatcharr: channel count
        if "dispatcharr" in self._clients:
            try:
                channel_count = await self._clients["dispatcharr"].get_channel_count()
                db.insert_library_snapshot("dispatcharr", "IPTV Channels", channel_count, None)
                count += 1
            except Exception:
                logger.exception("[dispatcharr] Failed to poll library stats")

        if count:
            logger.info("Recorded %d library snapshots", count)

    # -- Health polling --

    async def _poll_health_loop(self) -> None:
        while self._running:
            try:
                await self._poll_health()
            except Exception:
                logger.exception("Error in health polling cycle")
            await asyncio.sleep(self.config.poll_interval)

    # Services whose get_health() returns *arr-style warning lists
    _ARR_HEALTH_SERVICES = {"sonarr", "radarr", "lidarr", "prowlarr", "bazarr"}

    async def _poll_health(self) -> None:
        """Ping all configured services and record health state changes."""
        for name, client in self._clients.items():
            try:
                alive = await client.ping()
                if alive:
                    detail = None
                    # Only check *arr-style health warnings for services that return them
                    if name in self._ARR_HEALTH_SERVICES and hasattr(client, "get_health"):
                        warnings = await client.get_health()
                        if isinstance(warnings, list) and warnings:
                            detail = "; ".join(w.get("message", "") for w in warnings[:3])
                            db.upsert_health(name, "degraded", detail)
                            continue
                    db.upsert_health(name, "healthy", detail)
                else:
                    db.upsert_health(name, "unreachable", "Ping failed")
            except Exception as e:
                db.upsert_health(name, "unreachable", str(e)[:200])

    # -- Retention --

    async def _retention_loop(self) -> None:
        """Run retention/rollup once daily (every 24 hours)."""
        # Wait 1 hour after startup before first run
        await asyncio.sleep(3600)
        while self._running:
            try:
                from app.retention import run_retention
                results = run_retention()
                logger.info("Retention completed: %s", results)
            except Exception:
                logger.exception("Error in retention job")
            await asyncio.sleep(86400)  # 24 hours
