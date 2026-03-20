"""Prowlarr client — indexer stats and health."""

from .base import ArrClient


class ProwlarrClient(ArrClient):
    """Prowlarr API v1 client."""

    async def get_indexer_stats(self) -> dict:
        """Fetch indexer statistics (queries, grabs, failures)."""
        return await self.get("/api/v1/indexerstats")

    async def get_indexers(self) -> list[dict]:
        return await self.get("/api/v1/indexer")

    async def get_health(self) -> list[dict]:
        return await self.get("/api/v1/health")

    async def ping(self) -> bool:
        try:
            await self.get("/api/v1/system/status")
            return True
        except Exception:
            return False
