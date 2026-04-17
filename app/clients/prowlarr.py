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

    async def search(
        self,
        query: str,
        indexer_ids: list[int] | None = None,
        categories: list[int] | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """GET /api/v1/search — unified search across configured indexers."""
        params: dict = {"query": query, "limit": limit, "type": "search"}
        if indexer_ids:
            params["indexerIds"] = indexer_ids
        if categories:
            params["categories"] = categories
        return await self.get("/api/v1/search", params=params)

    async def grab(self, guid: str, indexer_id: int) -> dict:
        """POST /api/v1/search — send a release to Prowlarr's download client."""
        return await self.post("/api/v1/search", json_data={
            "guid": guid,
            "indexerId": indexer_id,
        })

    async def ping(self) -> bool:
        try:
            await self.get("/api/v1/system/status")
            return True
        except Exception:
            return False
