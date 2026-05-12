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

    async def get_all_media(self, page_size: int = 100) -> list[dict]:
        """Fetch every media record Seerr tracks.

        Pages through /api/v1/media until exhausted. The endpoint returns
        every title Seerr knows about — including ones whose *arr file has
        since been deleted (status reverts to <5).
        """
        results: list[dict] = []
        skip = 0
        while True:
            page = await self.get("/api/v1/media", params={
                "take": page_size, "skip": skip, "sort": "mediaAdded", "filter": "all",
            })
            batch = page.get("results", []) if isinstance(page, dict) else []
            results.extend(batch)
            page_info = page.get("pageInfo", {}) if isinstance(page, dict) else {}
            total_pages = page_info.get("pages", 1)
            current_page = page_info.get("page", 1)
            if current_page >= total_pages or not batch:
                break
            skip += page_size
        return results

    async def get_media_by_id(self, media_id: int) -> dict:
        """Fetch a single Seerr media record by its internal id."""
        return await self.get(f"/api/v1/media/{media_id}")

    async def delete_media(self, media_id: int) -> dict:
        """Delete a Seerr media record (does NOT touch *arr files).

        Removes Seerr's tracking entry so the title becomes re-requestable.
        Returns {"status": "deleted"} on a 204.
        """
        return await self.delete(f"/api/v1/media/{media_id}")

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

    async def delete(self, path: str, params: dict | None = None) -> Any:
        """DELETE request to the Seerr API."""
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.delete(url, headers=self._headers(), params=params)
            resp.raise_for_status()
            if resp.status_code == 204 or not resp.content:
                return {"status": "deleted"}
            return resp.json()

    async def get_request_by_id(self, request_id: int) -> dict:
        """Fetch a single request by ID."""
        return await self.get(f"/api/v1/request/{request_id}")

    async def delete_request(self, request_id: int) -> dict:
        """Cancel/delete a Seerr request."""
        return await self.delete(f"/api/v1/request/{request_id}")

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

    # Status drops from >= AVAILABILITY_THRESHOLD to anything below trigger an event.
    # Seerr status codes: 1=UNKNOWN, 2=PENDING, 3=PROCESSING, 4=PARTIALLY_AVAILABLE, 5=AVAILABLE.
    AVAILABILITY_THRESHOLD = 4

    def parse_media_diff_events(
        self, current: list[dict], previous: dict[int, int],
    ) -> tuple[list[dict], dict[int, int]]:
        """Diff current media list against a prior {media_id: status} snapshot.

        Emits `seerr_media_unavailable` events for any media whose status
        dropped from >=4 (partially or fully available) to <4 — the signature
        of a Radarr/Sonarr file deletion that Seerr has noticed via webhook.

        Returns (events, new_snapshot).
        """
        from datetime import datetime, timezone

        events: list[dict] = []
        new_snapshot: dict[int, int] = {}
        now_iso = datetime.now(timezone.utc).isoformat()

        for m in current:
            mid = m.get("id")
            status = m.get("status", 0)
            if mid is None:
                continue
            new_snapshot[mid] = status

            prev_status = previous.get(mid)
            if prev_status is None:
                continue  # First time we've seen this id — no diff possible.
            if prev_status >= self.AVAILABILITY_THRESHOLD and status < self.AVAILABILITY_THRESHOLD:
                events.append({
                    "source": "seerr",
                    "event_type": "seerr_media_unavailable",
                    "title": m.get("title") or m.get("name") or f"media #{mid}",
                    "timestamp": now_iso,
                    "source_event_id": f"seerr_media_drop_{mid}_{now_iso}",
                    "metadata": {
                        "media_id": mid,
                        "media_type": m.get("mediaType"),
                        "tmdb_id": m.get("tmdbId"),
                        "tvdb_id": m.get("tvdbId"),
                        "imdb_id": m.get("imdbId"),
                        "previous_status": prev_status,
                        "current_status": status,
                    },
                })

        return events, new_snapshot
