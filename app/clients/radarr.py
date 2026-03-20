"""Radarr client — movie event collection and management."""

from typing import Any

from .base import ArrClient


class RadarrClient(ArrClient):
    """Radarr API v3 client."""

    async def get_history(self, page: int = 1, page_size: int = 50) -> list[dict]:
        """Fetch recent history events."""
        data = await self.get("/api/v3/history", params={
            "sortKey": "date",
            "sortDirection": "descending",
            "page": page,
            "pageSize": page_size,
            "includeMovie": True,
        })
        return data.get("records", [])

    async def get_queue(self) -> list[dict]:
        """Fetch current download queue."""
        data = await self.get("/api/v3/queue", params={
            "pageSize": 100,
            "includeUnknownMovieItems": True,
        })
        return data.get("records", [])

    async def get_calendar(self, days: int = 30) -> list[dict]:
        """Fetch upcoming movies."""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        start = now.strftime("%Y-%m-%d")
        end = (now + timedelta(days=days)).strftime("%Y-%m-%d")
        return await self.get("/api/v3/calendar", params={"start": start, "end": end})

    async def get_movies(self) -> list[dict]:
        """Fetch all movies in library."""
        return await self.get("/api/v3/movie")

    async def get_root_folders(self) -> list[dict]:
        """Fetch root folders with free space."""
        return await self.get("/api/v3/rootfolder")

    async def get_health(self) -> list[dict]:
        """Fetch health check warnings."""
        return await self.get("/api/v3/health")

    def parse_history_event(self, event: dict) -> dict[str, Any]:
        """Normalise a Radarr history event into a MediaStack event."""
        movie = event.get("movie", {})
        title = f"{movie.get('title', 'Unknown')} ({movie.get('year', '?')})"

        return {
            "source": "radarr",
            "event_type": event.get("eventType", "unknown").lower(),
            "title": title,
            "timestamp": event.get("date"),
            "source_event_id": f"radarr_{event.get('id')}",
            "metadata": {
                "quality": event.get("quality", {}).get("quality", {}).get("name"),
                "size_bytes": event.get("data", {}).get("size"),
                "indexer": event.get("data", {}).get("indexer"),
                "download_client": event.get("data", {}).get("downloadClient"),
                "movie_id": movie.get("id"),
                "tmdb_id": movie.get("tmdbId"),
                "year": movie.get("year"),
            },
        }
