"""Suggestarr client — AI-driven content suggestion engine."""

from typing import Any

import httpx

from .base import DEFAULT_TIMEOUT


class SuggestarrClient:
    """SuggestArr Flask API client with JWT auth."""

    def __init__(self, url: str, username: str, password: str):
        self.name = "suggestarr"
        self.base_url = url
        self.username = username
        self.password = password
        self._token: str | None = None

    async def _ensure_auth(self) -> str:
        """Authenticate and return JWT token."""
        if self._token:
            return self._token
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.post(
                f"{self.base_url}/api/auth/login",
                json={"username": self.username, "password": self.password},
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = data.get("access_token") or data.get("token")
            return self._token

    async def get(self, path: str, params: dict | None = None) -> Any:
        token = await self._ensure_auth()
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.get(
                url, headers={"Authorization": f"Bearer {token}"}, params=params,
            )
            if resp.status_code == 401:
                # Token expired — re-auth
                self._token = None
                token = await self._ensure_auth()
                resp = await client.get(
                    url, headers={"Authorization": f"Bearer {token}"}, params=params,
                )
            resp.raise_for_status()
            return resp.json()

    async def get_request_stats(self) -> dict:
        """Get request counts: total, today, this_week, this_month."""
        return await self.get("/api/automation/requests/stats")

    async def get_requests(self, page: int = 1) -> dict:
        """Get all requests with source media and rationale."""
        return await self.get("/api/automation/requests", params={"page": page})

    async def get_job_history(self, limit: int = 50) -> list[dict]:
        """Get recent job execution history."""
        return await self.get("/api/jobs/history", params={"limit": limit})

    async def get_queue_status(self) -> dict:
        """Get pending request counts by status."""
        return await self.get("/api/jobs/queue-status")

    async def ping(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                resp = await client.get(f"{self.base_url}/api/health/live")
                return resp.status_code == 200
        except Exception:
            return False
