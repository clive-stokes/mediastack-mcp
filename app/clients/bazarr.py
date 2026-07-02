"""Bazarr client — subtitle history and management."""

from typing import Any

import httpx

from .base import DEFAULT_TIMEOUT


class BazarrClient:
    """Bazarr API client."""

    def __init__(self, url: str, api_key: str):
        self.name = "bazarr"
        self.base_url = url
        self.api_key = api_key
        self._client = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT, headers=self._headers(),
        )

    def _headers(self) -> dict[str, str]:
        return {"X-API-KEY": self.api_key}

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def get(self, path: str, params: dict | None = None) -> Any:
        resp = await self._client.get(f"{self.base_url}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_history(self, page: int = 1, page_size: int = 50) -> dict:
        """Fetch subtitle history (movies + episodes)."""
        return await self.get("/api/episodes/history", params={
            "page": page, "length": page_size,
        })

    async def get_movie_history(self, page: int = 1, page_size: int = 50) -> dict:
        return await self.get("/api/movies/history", params={
            "page": page, "length": page_size,
        })

    async def get_system_health(self) -> list[dict]:
        return await self.get("/api/system/health")

    async def post(self, path: str, json_data: dict | None = None) -> Any:
        resp = await self._client.post(f"{self.base_url}{path}", json=json_data)
        resp.raise_for_status()
        return resp.json()

    async def search_subtitles_episode(self, episode_id: int, language: str | None = None) -> dict:
        """Trigger subtitle search for an episode."""
        params: dict = {"episodeid": episode_id}
        if language:
            params["language"] = language
        return await self.post("/api/episodes/subtitles", json_data=params)

    async def search_subtitles_movie(self, movie_id: int, language: str | None = None) -> dict:
        """Trigger subtitle search for a movie."""
        params: dict = {"radarrid": movie_id}
        if language:
            params["language"] = language
        return await self.post("/api/movies/subtitles", json_data=params)

    async def ping(self) -> bool:
        try:
            await self.get("/api/system/status")
            return True
        except Exception:
            return False

    def parse_history_event(self, event: dict, media_type: str = "episode") -> dict[str, Any]:
        """Normalise a Bazarr history event into a MediaStack event."""
        title_parts = []
        if media_type == "episode":
            series = event.get("seriesTitle", "")
            ep_info = event.get("episode_number", "")
            if series:
                title_parts.append(series)
            if ep_info:
                title_parts.append(ep_info)
        else:
            movie_title = event.get("title", "")
            if movie_title:
                title_parts.append(movie_title)

        title = " ".join(title_parts) or "Unknown"
        language = event.get("language", {})
        lang_name = language.get("name", "") if isinstance(language, dict) else str(language)

        action = event.get("action", 0)
        # Bazarr actions: 1=downloaded, 2=erased, 3=upgraded
        event_type_map = {1: "subtitle_downloaded", 2: "subtitle_erased", 3: "subtitle_upgraded"}
        event_type = event_type_map.get(action, f"subtitle_action_{action}")

        return {
            "source": "bazarr",
            "event_type": event_type,
            "title": title,
            "timestamp": event.get("timestamp"),
            "source_event_id": f"bazarr_{media_type}_{event.get('id', '')}",
            "metadata": {
                "language": lang_name,
                "provider": event.get("provider"),
                "score": event.get("score"),
                "media_type": media_type,
            },
        }
