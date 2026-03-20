"""qBittorrent client — torrent queue monitoring."""

import hashlib
import logging
from typing import Any

import httpx

from .base import DEFAULT_TIMEOUT

logger = logging.getLogger(__name__)


class QBittorrentClient:
    """qBittorrent Web API v2 client."""

    def __init__(self, url: str, username: str, password: str):
        self.name = "qbittorrent"
        self.base_url = url
        self.username = username
        self.password = password
        self._cookie: str | None = None

    async def _ensure_auth(self, client: httpx.AsyncClient) -> None:
        """Authenticate if we don't have a valid session."""
        if self._cookie:
            return
        resp = await client.post(
            f"{self.base_url}/api/v2/auth/login",
            data={"username": self.username, "password": self.password},
        )
        resp.raise_for_status()
        sid = resp.cookies.get("SID")
        if sid:
            self._cookie = sid

    async def _get(self, path: str, params: dict | None = None) -> Any:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            await self._ensure_auth(client)
            cookies = {"SID": self._cookie} if self._cookie else {}
            resp = await client.get(
                f"{self.base_url}{path}", params=params, cookies=cookies,
            )
            if resp.status_code == 403:
                # Session expired — re-auth
                self._cookie = None
                await self._ensure_auth(client)
                cookies = {"SID": self._cookie} if self._cookie else {}
                resp = await client.get(
                    f"{self.base_url}{path}", params=params, cookies=cookies,
                )
            resp.raise_for_status()
            return resp.json()

    async def get_torrents(self) -> list[dict]:
        """Fetch all torrents."""
        return await self._get("/api/v2/torrents/info")

    async def get_transfer_info(self) -> dict:
        """Fetch global transfer stats."""
        return await self._get("/api/v2/transfer/info")

    async def ping(self) -> bool:
        try:
            await self._get("/api/v2/app/version")
            return True
        except Exception:
            return False

    def parse_torrent_events(self, current: list[dict], previous: list[dict]) -> list[dict]:
        """Diff two torrent snapshots to detect state changes.

        We track: completed, errored, stalled (new states since last poll).
        """
        prev_map = {t["hash"]: t for t in previous}
        events = []

        for torrent in current:
            h = torrent["hash"]
            prev = prev_map.get(h)
            state = torrent.get("state", "")
            prev_state = prev.get("state", "") if prev else None

            if prev_state == state:
                continue

            # Detect interesting transitions
            event_type = None
            if state in ("uploading", "stalledUP", "forcedUP") and prev_state not in ("uploading", "stalledUP", "forcedUP"):
                event_type = "completed"
            elif "error" in state.lower():
                event_type = "errored"
            elif state == "stalledDL" and prev_state != "stalledDL":
                event_type = "stalled"

            if event_type:
                events.append({
                    "source": "qbittorrent",
                    "event_type": event_type,
                    "title": torrent.get("name", "Unknown"),
                    "timestamp": None,  # qBit doesn't give event timestamps; poller uses poll time
                    "source_event_id": f"qbt_{h}_{event_type}",
                    "metadata": {
                        "hash": h,
                        "category": torrent.get("category"),
                        "size_bytes": torrent.get("total_size"),
                        "ratio": torrent.get("ratio"),
                        "state": state,
                        "tracker": torrent.get("tracker"),
                    },
                })

        return events
