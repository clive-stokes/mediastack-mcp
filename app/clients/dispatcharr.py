"""Dispatcharr client — IPTV DVR/EPG manager."""

from typing import Any

import httpx

from .base import DEFAULT_TIMEOUT


class DispatcharrClient:
    """Dispatcharr Django/DRF API client."""

    def __init__(self, url: str, api_key: str):
        self.name = "dispatcharr"
        self.base_url = url
        self.api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key}

    async def get(self, path: str, params: dict | None = None) -> Any:
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.get(url, headers=self._headers(), params=params)
            resp.raise_for_status()
            return resp.json()

    async def get_channel_count(self) -> int:
        """Get total channel count."""
        data = await self.get("/api/channels/channels/", params={"page": 1, "page_size": 1})
        return data.get("count", 0)

    async def get_epg_sources(self) -> list[dict]:
        """Get EPG sources with status, last update, data count."""
        return await self.get("/api/epg/sources/")

    async def get_m3u_accounts(self) -> list[dict]:
        """Get M3U accounts with status, last refresh."""
        return await self.get("/api/m3u/accounts/")

    async def get_system_events(self, limit: int = 20) -> list[dict]:
        """Get recent system events."""
        data = await self.get("/api/core/system-events/", params={"limit": limit})
        return data.get("results", []) if isinstance(data, dict) else data

    async def ping(self) -> bool:
        try:
            await self.get("/api/core/version/")
            return True
        except Exception:
            return False
