"""Base HTTP client for *arr-style services."""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0


class ArrClient:
    """Base client for Sonarr/Radarr/Lidarr/Prowlarr-style APIs.

    One httpx.AsyncClient is created per service and reused for all
    requests — avoids a TCP/TLS handshake on every poll.
    """

    def __init__(self, name: str, url: str, api_key: str):
        self.name = name
        self.base_url = url
        self.api_key = api_key
        self._client = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT, headers=self._headers(),
        )

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.api_key}

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def get(self, path: str, params: dict | None = None) -> Any:
        """GET request to the service API."""
        resp = await self._client.get(f"{self.base_url}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    async def post(self, path: str, json_data: dict | None = None) -> Any:
        """POST request to the service API."""
        resp = await self._client.post(f"{self.base_url}{path}", json=json_data)
        resp.raise_for_status()
        return resp.json()

    async def delete(self, path: str, params: dict | None = None) -> Any:
        """DELETE request to the service API."""
        resp = await self._client.delete(f"{self.base_url}{path}", params=params)
        resp.raise_for_status()
        # DELETE often returns 204 No Content
        if resp.status_code == 204 or not resp.content:
            return {"status": "deleted"}
        return resp.json()

    async def ping(self) -> bool:
        """Health check — returns True if the service responds."""
        try:
            await self.get("/api/v3/system/status")
            return True
        except Exception:
            return False


class JellyfinClient:
    """Client for Jellyfin API (different auth pattern)."""

    def __init__(self, url: str, api_key: str):
        self.name = "jellyfin"
        self.base_url = url
        self.api_key = api_key
        self._client = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT, headers=self._headers(),
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f'MediaBrowser Token="{self.api_key}"'}

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def get(self, path: str, params: dict | None = None,
                  timeout: float | None = None) -> Any:
        resp = await self._client.get(
            f"{self.base_url}{path}", params=params,
            timeout=timeout or DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    async def post(self, path: str, json_data: dict | None = None,
                   params: dict | None = None) -> Any:
        """POST request to the Jellyfin API."""
        resp = await self._client.post(
            f"{self.base_url}{path}", json=json_data, params=params,
        )
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            return {"status": "ok"}
        return resp.json()

    async def delete(self, path: str, params: dict | None = None) -> Any:
        """DELETE request to the Jellyfin API."""
        resp = await self._client.delete(f"{self.base_url}{path}", params=params)
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            return {"status": "ok"}
        return resp.json()

    async def ping(self) -> bool:
        try:
            await self.get("/System/Info")
            return True
        except Exception:
            return False
