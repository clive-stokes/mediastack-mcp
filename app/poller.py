"""Background polling engine for MediaStack.

Periodically polls configured services, collects events/storage/health,
and writes them to the database. Designed to run as an asyncio background task.
"""

import asyncio
import logging
from datetime import datetime, timezone

from app import db
from app.config import Config, ServiceConfig, CredentialConfig
from app.clients.sonarr import SonarrClient
from app.clients.radarr import RadarrClient
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
            # Phase 2: lidarr, prowlarr, bazarr, seerr, jellyfin

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
        ]
        await asyncio.gather(*tasks)

    async def stop(self) -> None:
        self._running = False

    # -- Event polling --

    async def _poll_events_loop(self) -> None:
        """Poll event sources on the configured interval."""
        while self._running:
            try:
                await self._poll_events()
            except Exception:
                logger.exception("Error in event polling cycle")
            await asyncio.sleep(self.config.poll_interval)

    async def _poll_events(self) -> None:
        """Single pass: collect events from all configured sources."""
        # Sonarr
        if "sonarr" in self._clients:
            await self._poll_arr_history("sonarr")

        # Radarr
        if "radarr" in self._clients:
            await self._poll_arr_history("radarr")

        # SABnzbd
        if "sabnzbd" in self._clients:
            await self._poll_sabnzbd()

        # qBittorrent
        if "qbittorrent" in self._clients:
            await self._poll_qbittorrent()

    async def _poll_arr_history(self, name: str) -> None:
        """Poll a Sonarr/Radarr-style history endpoint."""
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
                # Set timestamps to now for qBit events (no native timestamps)
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

    # -- Storage polling --

    async def _poll_storage_loop(self) -> None:
        """Poll storage on the configured interval."""
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

                    # Try filesystem stats first (more complete — gives total + used)
                    fs = await self._get_fs_stats(path)
                    if fs:
                        total, used = fs
                        db.insert_storage_snapshot(path, total, used, f"{name}_rootfolder")
                        count += 1
                    elif f.get("totalSpace", 0) > 0:
                        # Fallback to API data if available
                        total = f["totalSpace"]
                        free = f.get("freeSpace", 0)
                        db.insert_storage_snapshot(path, total, total - free, f"{name}_rootfolder")
                        count += 1
            except Exception:
                logger.exception("[%s] Failed to poll root folders", name)

        if count:
            logger.info("Recorded %d storage snapshots", count)

    async def _get_fs_stats(self, container_path: str) -> tuple[int, int] | None:
        """Get total/used bytes via os.statvfs on the mounted media volume.

        The *arr containers map /media/* from /volume1/MagnoliaMedia.
        Our container mounts /volume1/MagnoliaMedia at /media, so the
        paths match directly.
        """
        import os
        # The path from the *arr API (e.g. /media/TV) matches our mount
        if os.path.exists(container_path):
            try:
                st = os.statvfs(container_path)
                total = st.f_frsize * st.f_blocks
                used = st.f_frsize * (st.f_blocks - st.f_bfree)
                return total, used
            except OSError:
                pass
        return None

    # -- Health polling --

    async def _poll_health_loop(self) -> None:
        """Check service health on the event poll interval."""
        while self._running:
            try:
                await self._poll_health()
            except Exception:
                logger.exception("Error in health polling cycle")
            await asyncio.sleep(self.config.poll_interval)

    async def _poll_health(self) -> None:
        """Ping all configured services and record health state changes."""
        all_clients = {**self._clients}
        for name, client in all_clients.items():
            try:
                alive = await client.ping()
                if alive:
                    # Check for *arr-specific health warnings
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
