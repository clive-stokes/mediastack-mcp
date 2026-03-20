"""Lidarr client — music event collection and management."""

from typing import Any

from .base import ArrClient


class LidarrClient(ArrClient):
    """Lidarr API v3 client."""

    async def get_history(self, page: int = 1, page_size: int = 50) -> list[dict]:
        """Fetch recent history events."""
        data = await self.get("/api/v1/history", params={
            "sortKey": "date",
            "sortDirection": "descending",
            "page": page,
            "pageSize": page_size,
            "includeArtist": True,
            "includeAlbum": True,
        })
        return data.get("records", [])

    async def get_queue(self) -> list[dict]:
        data = await self.get("/api/v1/queue", params={"pageSize": 100})
        return data.get("records", [])

    async def get_artists(self) -> list[dict]:
        return await self.get("/api/v1/artist")

    async def get_root_folders(self) -> list[dict]:
        return await self.get("/api/v1/rootfolder")

    async def get_health(self) -> list[dict]:
        return await self.get("/api/v1/health")

    async def ping(self) -> bool:
        try:
            await self.get("/api/v1/system/status")
            return True
        except Exception:
            return False

    def parse_history_event(self, event: dict) -> dict[str, Any]:
        """Normalise a Lidarr history event into a MediaStack event."""
        artist = event.get("artist") or {}
        album = event.get("album") or {}

        artist_name = artist.get("artistName", "")
        album_title = album.get("title", "")

        if artist_name and album_title:
            title = f"{artist_name} — {album_title}"
        elif artist_name:
            title = artist_name
        else:
            title = event.get("sourceTitle", "Unknown")

        return {
            "source": "lidarr",
            "event_type": event.get("eventType", "unknown").lower(),
            "title": title,
            "timestamp": event.get("date"),
            "source_event_id": f"lidarr_{event.get('id')}",
            "metadata": {
                "quality": event.get("quality", {}).get("quality", {}).get("name"),
                "size_bytes": event.get("data", {}).get("size"),
                "indexer": event.get("data", {}).get("indexer"),
                "download_client": event.get("data", {}).get("downloadClient"),
                "artist_id": artist.get("id"),
                "album_id": album.get("id"),
            },
        }
