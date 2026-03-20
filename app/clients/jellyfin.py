"""Jellyfin client — library stats and activity."""

from .base import JellyfinClient as BaseJellyfinClient


class JellyfinClient(BaseJellyfinClient):
    """Extended Jellyfin client for MediaStack."""

    async def get_item_counts(self) -> dict:
        """Get total item counts across all libraries."""
        return await self.get("/Items/Counts")

    async def get_libraries(self) -> list[dict]:
        """Get all virtual folders (libraries)."""
        return await self.get("/Library/VirtualFolders")

    async def get_activity_log(self, limit: int = 50) -> list[dict]:
        """Get recent activity log entries."""
        data = await self.get("/System/ActivityLog/Entries", params={
            "limit": limit,
        })
        return data.get("Items", [])

    async def get_library_items_count(self, parent_id: str) -> int:
        """Get item count for a specific library."""
        data = await self.get("/Items", params={
            "ParentId": parent_id,
            "Recursive": True,
            "Limit": 0,
        })
        return data.get("TotalRecordCount", 0)
