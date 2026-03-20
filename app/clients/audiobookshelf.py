"""Audiobookshelf client — audiobook/podcast library stats."""

from typing import Any

import httpx

from .base import DEFAULT_TIMEOUT


class AudiobookshelfClient:
    """Audiobookshelf API client."""

    def __init__(self, url: str, api_key: str):
        self.name = "audiobookshelf"
        self.base_url = url
        self.api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def get(self, path: str, params: dict | None = None) -> Any:
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.get(url, headers=self._headers(), params=params)
            resp.raise_for_status()
            return resp.json()

    async def get_libraries(self) -> list[dict]:
        """Get all libraries."""
        data = await self.get("/api/libraries")
        return data.get("libraries", [])

    async def get_library_stats(self, library_id: str) -> dict:
        """Get stats for a specific library."""
        return await self.get(f"/api/libraries/{library_id}/stats")

    async def ping(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                resp = await client.get(f"{self.base_url}/healthcheck")
                return resp.status_code == 200
        except Exception:
            return False
