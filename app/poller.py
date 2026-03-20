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

logger = logging.getLogger(__name__)


class Poller:
    """Background polling engine."""

    def __init__(self, config: Config):
        self.config = config
        self._clients: dict = {}
        self._qbt_prev_torrents: list[dict] = []
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
                self._clients["jellyfin"] = JellyfinClient(svc.url, svc.api_key)
            elif name == "seerr":
                self._clients["seerr"] = SeerrClient(svc.url, svc.api_key)

        for name, svc in self.config.credential_services.items():
            if name == "sabnzbd":
                self._clients["sabnzbd"] = SabnzbdClient(svc.url, svc.password)
            elif name == "qbittorrent":
                self._clients["qbittorrent"] = QBittorrentClient(svc.url, svc.username, svc.password)

    async def start(self) -> None:
        """Start all polling loops."""
        self._running = True
        logger.info("Poller starting. %s", self.config.describe())

        tasks = [
            asyncio.create_task(self._poll_events_loop()),
            asyncio.create_task(self._poll_storage_loop()),
            asyncio.create_task(self._poll_health_loop()),
            asyncio.create_task(self._poll_libraries_loop()),
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

    # -- Library snapshot polling --

    async def _poll_libraries_loop(self) -> None:
        while self._running:
            try:
                await self._poll_libraries()
            except Exception:
                logger.exception("Error in library polling cycle")
            await asyncio.sleep(self.config.library_interval)

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

        # Jellyfin: per-library item counts
        if "jellyfin" in self._clients:
            try:
                libraries = await self._clients["jellyfin"].get_libraries()
                for lib in libraries:
                    lib_name = lib.get("Name", "Unknown")
                    lib_id = lib.get("ItemId", "")
                    if lib_id:
                        item_count = await self._clients["jellyfin"].get_library_items_count(lib_id)
                        # STRM libraries have negligible disk usage
                        is_strm = "strm" in lib_name.lower() or "ifiesta" in lib_name.lower()
                        size = None if is_strm else 0  # size tracked via *arr, not Jellyfin
                        db.insert_library_snapshot("jellyfin", lib_name, item_count, size)
                        count += 1
            except Exception:
                logger.exception("[jellyfin] Failed to poll library stats")

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

    async def _poll_health(self) -> None:
        """Ping all configured services and record health state changes."""
        for name, client in self._clients.items():
            try:
                alive = await client.ping()
                if alive:
                    detail = None
                    if hasattr(client, "get_health"):
                        warnings = await client.get_health()
                        if warnings:
                            detail = "; ".join(w.get("message", "") for w in warnings[:3])
                            db.upsert_health(name, "degraded", detail)
                            continue
                    db.upsert_health(name, "healthy", detail)
                else:
                    db.upsert_health(name, "unreachable", "Ping failed")
            except Exception as e:
                db.upsert_health(name, "unreachable", str(e)[:200])
