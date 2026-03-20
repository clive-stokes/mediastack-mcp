"""Boxarr client — box office tracker, adds movies to Radarr."""

from typing import Any

import httpx

from .base import DEFAULT_TIMEOUT


class BoxarrClient:
    """Boxarr FastAPI client. No authentication required."""

    def __init__(self, url: str):
        self.name = "boxarr"
        self.base_url = url

    async def get(self, path: str, params: dict | None = None) -> Any:
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()

    async def get_health(self) -> dict:
        """Health check with version, Radarr status, scheduler status."""
        return await self.get("/api/health")

    async def get_current_boxoffice(self) -> dict:
        """Current week's top 10 with Radarr matching status."""
        return await self.get("/api/boxoffice/current")

    async def get_scheduler_status(self) -> dict:
        """Scheduler status with next run, last run info."""
        return await self.get("/api/scheduler/status")

    async def get_scheduler_history(self, limit: int = 20) -> list[dict]:
        """Run history: timestamp, movies found/added per run."""
        return await self.get("/api/scheduler/history")

    async def ping(self) -> bool:
        try:
            await self.get_health()
            return True
        except Exception:
            return False
