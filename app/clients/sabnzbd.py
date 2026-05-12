"""SABnzbd client — Usenet download history and queue."""

import hashlib
from typing import Any

import httpx

from .base import DEFAULT_TIMEOUT


class SabnzbdClient:
    """SABnzbd API client."""

    def __init__(self, url: str, api_key: str):
        self.name = "sabnzbd"
        self.base_url = url
        self.api_key = api_key

    async def _api(self, mode: str, extra: dict | None = None) -> Any:
        params = {"mode": mode, "apikey": self.api_key, "output": "json"}
        if extra:
            params.update(extra)
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.get(f"{self.base_url}/api", params=params)
            resp.raise_for_status()
            return resp.json()

    async def get_history(self, limit: int = 50) -> list[dict]:
        """Fetch recent download history."""
        data = await self._api("history", {"limit": str(limit)})
        return data.get("history", {}).get("slots", [])

    async def get_queue(self) -> list[dict]:
        """Fetch active download queue."""
        data = await self._api("queue")
        return data.get("queue", {}).get("slots", [])

    async def get_server_stats(self) -> dict:
        """Fetch server transfer statistics."""
        return await self._api("server_stats")

    async def ping(self) -> bool:
        try:
            await self._api("version")
            return True
        except Exception:
            return False

    async def add_url(self, nzb_url: str) -> dict:
        """Submit NZB URL to SABnzbd."""
        return await self._api("addurl", {"name": nzb_url})

    def parse_history_event(self, slot: dict) -> dict[str, Any]:
        """Normalise a SABnzbd history slot into a MediaStack event."""
        from datetime import datetime, timezone

        status = slot.get("status", "").lower()
        event_type = "downloaded" if status == "completed" else "failed" if "fail" in status else status

        # SABnzbd slots don't have unique IDs — derive one from nzo_id
        nzo_id = slot.get("nzo_id", "")
        source_id = f"sabnzbd_{nzo_id}" if nzo_id else f"sabnzbd_{hashlib.md5(slot.get('name', '').encode()).hexdigest()[:12]}"

        # SABnzbd returns epoch timestamps
        completed_ts = slot.get("completed")
        if isinstance(completed_ts, (int, float)) and completed_ts > 0:
            timestamp = datetime.fromtimestamp(completed_ts, tz=timezone.utc).isoformat()
        else:
            timestamp = None

        return {
            "source": "sabnzbd",
            "event_type": event_type,
            "title": slot.get("name", "Unknown"),
            "timestamp": timestamp,
            "source_event_id": source_id,
            "metadata": {
                "category": slot.get("category"),
                "size_bytes": int(slot.get("bytes", 0)),
                "download_time_secs": slot.get("download_time"),
                "status": status,
                "failure_reason": slot.get("fail_message"),
                "nzo_id": nzo_id,
            },
        }
