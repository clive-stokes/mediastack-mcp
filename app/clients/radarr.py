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

    # -- Write operations --

    async def lookup(self, term: str) -> list[dict]:
        """Search for movies to add."""
        return await self.get("/api/v3/movie/lookup", params={"term": term})

    async def get_quality_profiles(self) -> list[dict]:
        return await self.get("/api/v3/qualityprofile")

    async def add_movie(self, tmdb_id: int, title: str,
                        quality_profile_id: int, root_folder_path: str,
                        monitored: bool = True) -> dict:
        """Add a movie to Radarr."""
        results = await self.get("/api/v3/movie/lookup", params={"term": f"tmdb:{tmdb_id}"})
        if not results:
            raise ValueError(f"Movie with tmdbId {tmdb_id} not found")
        movie_data = results[0]
        movie_data.update({
            "qualityProfileId": quality_profile_id,
            "rootFolderPath": root_folder_path,
            "monitored": monitored,
            "addOptions": {"searchForMovie": True},
        })
        return await self.post("/api/v3/movie", json_data=movie_data)

    async def search_movie(self, movie_id: int) -> dict:
        """Trigger search for a movie."""
        return await self.post("/api/v3/command", json_data={
            "name": "MoviesSearch", "movieIds": [movie_id],
        })

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
