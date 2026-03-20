"""Seerr/Overseerr client — request pipeline monitoring."""

from typing import Any

import httpx

from .base import DEFAULT_TIMEOUT


class SeerrClient:
    """Seerr/Overseerr API v1 client."""

    def __init__(self, url: str, api_key: str):
        self.name = "seerr"
        self.base_url = url
        self.api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.api_key}

    async def get(self, path: str, params: dict | None = None) -> Any:
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.get(url, headers=self._headers(), params=params)
            resp.raise_for_status()
            return resp.json()

    async def get_requests(self, take: int = 50, skip: int = 0) -> dict:
        """Fetch recent requests."""
        return await self.get("/api/v1/request", params={
            "take": take, "skip": skip, "sort": "added", "filter": "all",
        })

    async def get_request_count(self) -> dict:
        """Get request counts by status."""
        return await self.get("/api/v1/request/count")

    async def post(self, path: str, json_data: dict | None = None) -> Any:
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.post(url, headers=self._headers(), json=json_data)
            resp.raise_for_status()
            return resp.json()

    async def search(self, query: str) -> list[dict]:
        """Search for media in Seerr."""
        data = await self.get("/api/v1/search", params={"query": query, "page": 1, "language": "en"})
        return data.get("results", [])

    async def request_media(self, media_type: str, media_id: int) -> dict:
        """Create a media request."""
        return await self.post("/api/v1/request", json_data={
            "mediaType": media_type,
            "mediaId": media_id,
        })

    async def ping(self) -> bool:
        try:
            await self.get("/api/v1/status")
            return True
        except Exception:
            return False

    def parse_request_event(self, request: dict) -> dict[str, Any]:
        """Normalise a Seerr request into a MediaStack event."""
        media = request.get("media", {})
        media_type = request.get("type", "unknown")

        # Build title from media info
        title = "Unknown"
        if media:
            if media_type == "movie":
                title = media.get("originalTitle") or media.get("title", "Unknown")
            elif media_type == "tv":
                title = media.get("name") or media.get("originalName", "Unknown")

        # Map status to event type
        status = request.get("status", 0)
        # 1=pending, 2=approved, 3=declined, 4=available (implementation varies)
        status_map = {1: "request_pending", 2: "request_approved", 3: "request_declined"}
        event_type = status_map.get(status, f"request_status_{status}")

        # Check if media is available
        if media.get("status") == 5:  # available
            event_type = "request_available"

        requested_by = request.get("requestedBy", {})
        user_name = requested_by.get("displayName") or requested_by.get("email", "unknown")

        return {
            "source": "seerr",
            "event_type": event_type,
            "title": title,
            "timestamp": request.get("createdAt"),
            "source_event_id": f"seerr_{request.get('id')}",
            "metadata": {
                "media_type": media_type,
                "status": status,
                "requested_by": user_name,
                "tmdb_id": media.get("tmdbId"),
                "tvdb_id": media.get("tvdbId"),
                "media_status": media.get("status"),
            },
        }
