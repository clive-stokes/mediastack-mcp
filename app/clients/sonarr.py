"""Sonarr client — TV series event collection and management."""

from typing import Any

from .base import ArrClient


class SonarrClient(ArrClient):
    """Sonarr API v3 client."""

    async def get_history(self, page: int = 1, page_size: int = 50) -> list[dict]:
        """Fetch recent history events."""
        data = await self.get("/api/v3/history", params={
            "sortKey": "date",
            "sortDirection": "descending",
            "page": page,
            "pageSize": page_size,
            "includeSeries": True,
            "includeEpisode": True,
        })
        return data.get("records", [])

    async def get_queue(self) -> list[dict]:
        """Fetch current download queue."""
        data = await self.get("/api/v3/queue", params={
            "pageSize": 100,
            "includeUnknownSeriesItems": True,
        })
        return data.get("records", [])

    async def get_calendar(self, days: int = 7) -> list[dict]:
        """Fetch upcoming episodes."""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        start = now.strftime("%Y-%m-%d")
        end = (now + timedelta(days=days)).strftime("%Y-%m-%d")
        return await self.get("/api/v3/calendar", params={"start": start, "end": end})

    async def get_series(self) -> list[dict]:
        """Fetch all series in library."""
        return await self.get("/api/v3/series")

    async def get_root_folders(self) -> list[dict]:
        """Fetch root folders with free space."""
        return await self.get("/api/v3/rootfolder")

    async def get_health(self) -> list[dict]:
        """Fetch health check warnings."""
        return await self.get("/api/v3/health")

    def parse_history_event(self, event: dict) -> dict[str, Any]:
        """Normalise a Sonarr history event into a MediaStack event."""
        series = event.get("series") or {}
        episode = event.get("episode") or {}

        season = episode.get("seasonNumber")
        ep_num = episode.get("episodeNumber")
        series_title = series.get("title", "")

        if series_title and isinstance(season, int) and isinstance(ep_num, int):
            title = f"{series_title} S{season:02d}E{ep_num:02d}"
        elif series_title:
            title = series_title
        else:
            # Fallback to sourceTitle (e.g. "Show.S01E05.720p.WEB...")
            title = event.get("sourceTitle", "Unknown")

        return {
            "source": "sonarr",
            "event_type": event.get("eventType", "unknown").lower(),
            "title": title,
            "timestamp": event.get("date"),
            "source_event_id": f"sonarr_{event.get('id')}",
            "metadata": {
                "quality": event.get("quality", {}).get("quality", {}).get("name"),
                "size_bytes": event.get("data", {}).get("size"),
                "indexer": event.get("data", {}).get("indexer"),
                "download_client": event.get("data", {}).get("downloadClient"),
                "series_id": series.get("id"),
                "episode_id": episode.get("id"),
                "season": season,
                "episode": ep_num,
            },
        }
