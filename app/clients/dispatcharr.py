"""Dispatcharr client — IPTV DVR/EPG manager."""

from typing import Any

import httpx

from .base import DEFAULT_TIMEOUT


class DispatcharrClient:
    """Dispatcharr Django/DRF API client with JWT auth."""

    def __init__(self, url: str, username: str, password: str):
        self.name = "dispatcharr"
        self.base_url = url
        self.username = username
        self.password = password
        self._token: str | None = None
        self._client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def _ensure_auth(self) -> str:
        if self._token:
            return self._token
        resp = await self._client.post(
            f"{self.base_url}/api/accounts/token/",
            json={"username": self.username, "password": self.password},
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data.get("access")
        return self._token

    async def get(self, path: str, params: dict | None = None) -> Any:
        token = await self._ensure_auth()
        url = f"{self.base_url}{path}"
        resp = await self._client.get(
            url, headers={"Authorization": f"Bearer {token}"}, params=params,
        )
        if resp.status_code == 401:
            self._token = None
            token = await self._ensure_auth()
            resp = await self._client.get(
                url, headers={"Authorization": f"Bearer {token}"}, params=params,
            )
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
            resp = await self._client.get(f"{self.base_url}/api/core/version/")
            return resp.status_code == 200
        except Exception:
            return False
