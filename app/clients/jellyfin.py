"""Jellyfin client — library stats, search, user data, collections, playlists."""

from .base import JellyfinClient as BaseJellyfinClient


class JellyfinClient(BaseJellyfinClient):
    """Extended Jellyfin client for MediaStack."""

    def __init__(self, url: str, api_key: str, user_id: str | None = None):
        super().__init__(url, api_key)
        self._user_id: str | None = user_id

    # -- User ID (lazily cached) --

    async def get_user_id(self) -> str:
        """Fetch and cache the user ID.

        Priority: JELLYFIN_USER_ID env var > /Users/Me > first non-generic admin > first admin.
        """
        if self._user_id is None:
            try:
                data = await self.get("/Users/Me")
                self._user_id = data["Id"]
            except Exception:
                # Server API keys don't support /Users/Me — fall back to user list
                users = await self.get("/Users")
                admins = [u for u in users if u.get("Policy", {}).get("IsAdministrator")]
                # Prefer a named admin over the generic "admin" account
                named_admins = [u for u in admins if u["Name"].lower() != "admin"]
                if named_admins:
                    self._user_id = named_admins[0]["Id"]
                elif admins:
                    self._user_id = admins[0]["Id"]
                elif users:
                    self._user_id = users[0]["Id"]
                if self._user_id is None:
                    raise RuntimeError("No Jellyfin users found")
        return self._user_id

    async def resolve_user_id(self, user_name: str | None = None) -> str:
        """Resolve a user ID — by name if given, otherwise the default user."""
        if not user_name:
            return await self.get_user_id()
        # Cache the user list to avoid repeated calls
        if not hasattr(self, "_users_cache"):
            self._users_cache: dict[str, str] = {}
        if not self._users_cache:
            users = await self.get("/Users")
            self._users_cache = {u["Name"].lower(): u["Id"] for u in users}
        uid = self._users_cache.get(user_name.lower())
        if not uid:
            available = ", ".join(sorted(self._users_cache.keys()))
            raise ValueError(f"User '{user_name}' not found. Available: {available}")
        return uid

    # -- Library stats (existing) --

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

    # -- Search & Browse --

    async def search_items(self, query: str, limit: int = 20,
                            user_name: str | None = None) -> list[dict]:
        """Search library items by name. Returns slim item dicts with user data."""
        user_id = await self.resolve_user_id(user_name)
        data = await self.get(f"/Users/{user_id}/Items", params={
            "SearchTerm": query,
            "Recursive": True,
            "Limit": limit,
            "Fields": "Overview,UserData",
            "IncludeItemTypes": "Movie,Series,Episode,Audio,MusicAlbum,MusicArtist",
        })
        return self._slim_items(data.get("Items", []))

    async def get_genres(self, media_type: str | None = None) -> list[dict]:
        """List genres, optionally filtered by media type."""
        params: dict = {}
        if media_type:
            params["IncludeItemTypes"] = media_type
        data = await self.get("/Genres", params=params)
        items = data.get("Items", [])
        return [{"id": g["Id"], "name": g["Name"]} for g in items]

    async def get_items_by_genre(self, genre: str,
                                  media_type: str | None = None,
                                  limit: int = 50,
                                  user_name: str | None = None) -> list[dict]:
        """Browse items by genre name."""
        user_id = await self.resolve_user_id(user_name)
        params: dict = {
            "Genres": genre,
            "Recursive": True,
            "Limit": limit,
            "Fields": "Overview,UserData",
            "SortBy": "SortName",
            "SortOrder": "Ascending",
        }
        if media_type:
            params["IncludeItemTypes"] = media_type
        data = await self.get(f"/Users/{user_id}/Items", params=params)
        return self._slim_items(data.get("Items", []))

    async def get_favorites(self, media_type: str | None = None,
                             limit: int = 50,
                             user_name: str | None = None) -> list[dict]:
        """Get a user's favourite items."""
        user_id = await self.resolve_user_id(user_name)
        params: dict = {
            "IsFavorite": True,
            "Recursive": True,
            "Limit": limit,
            "Fields": "Overview,UserData",
            "SortBy": "SortName",
            "SortOrder": "Ascending",
        }
        if media_type:
            params["IncludeItemTypes"] = media_type
        data = await self.get(f"/Users/{user_id}/Items", params=params)
        return self._slim_items(data.get("Items", []))

    # -- User data: favourites & watched --

    async def set_favorite(self, item_id: str, favorite: bool,
                            user_name: str | None = None) -> dict:
        """Add or remove an item from favourites."""
        user_id = await self.resolve_user_id(user_name)
        path = f"/Users/{user_id}/FavoriteItems/{item_id}"
        if favorite:
            return await self.post(path)
        else:
            return await self.delete(path)

    async def set_played(self, item_id: str, played: bool,
                          user_name: str | None = None) -> dict:
        """Mark an item as played or unplayed."""
        user_id = await self.resolve_user_id(user_name)
        path = f"/Users/{user_id}/PlayedItems/{item_id}"
        if played:
            return await self.post(path)
        else:
            return await self.delete(path)

    async def get_collections(self, limit: int = 50) -> list[dict]:
        """List all collections."""
        user_id = await self.get_user_id()
        data = await self.get(f"/Users/{user_id}/Items", params={
            "IncludeItemTypes": "BoxSet",
            "Recursive": True,
            "Limit": limit,
            "SortBy": "SortName",
            "SortOrder": "Ascending",
        })
        items = data.get("Items", [])
        return [{"item_id": i["Id"], "name": i.get("Name", "Unknown"),
                 "child_count": i.get("ChildCount", 0)} for i in items]

    async def get_collection_items(self, collection_id: str) -> list[dict]:
        """List items in a collection."""
        user_id = await self.get_user_id()
        data = await self.get(f"/Users/{user_id}/Items", params={
            "ParentId": collection_id,
            "Fields": "Overview,UserData",
        })
        return self._slim_items(data.get("Items", []))

    async def get_playlists(self, limit: int = 50) -> list[dict]:
        """List all playlists."""
        user_id = await self.get_user_id()
        data = await self.get(f"/Users/{user_id}/Items", params={
            "IncludeItemTypes": "Playlist",
            "Recursive": True,
            "Limit": limit,
            "SortBy": "SortName",
            "SortOrder": "Ascending",
        })
        items = data.get("Items", [])
        return [{"item_id": i["Id"], "name": i.get("Name", "Unknown"),
                 "media_type": i.get("MediaType", "Unknown"),
                 "child_count": i.get("ChildCount", 0)} for i in items]

    async def get_playlist_items(self, playlist_id: str) -> list[dict]:
        """List items in a playlist (ordered)."""
        user_id = await self.get_user_id()
        data = await self.get(f"/Playlists/{playlist_id}/Items", params={
            "UserId": user_id,
            "Fields": "Overview,UserData",
        })
        return self._slim_items(data.get("Items", []))

    # -- Collections --

    async def create_collection(self, name: str,
                                 item_ids: list[str] | None = None) -> dict:
        """Create a new collection, optionally with initial items."""
        params: dict = {"Name": name}
        if item_ids:
            params["Ids"] = ",".join(item_ids)
        return await self.post("/Collections", params=params)

    async def add_to_collection(self, collection_id: str,
                                 item_ids: list[str]) -> dict:
        """Add items to an existing collection."""
        return await self.post(
            f"/Collections/{collection_id}/Items",
            params={"Ids": ",".join(item_ids)},
        )

    async def remove_from_collection(self, collection_id: str,
                                      item_ids: list[str]) -> dict:
        """Remove items from a collection."""
        return await self.delete(
            f"/Collections/{collection_id}/Items",
            params={"Ids": ",".join(item_ids)},
        )

    # -- Playlists --

    async def create_playlist(self, name: str,
                               item_ids: list[str] | None = None,
                               media_type: str = "Video") -> dict:
        """Create a new playlist, optionally with initial items."""
        user_id = await self.get_user_id()
        body: dict = {
            "Name": name,
            "UserId": user_id,
            "MediaType": media_type,
        }
        if item_ids:
            body["Ids"] = item_ids
        return await self.post("/Playlists", json_data=body)

    async def add_to_playlist(self, playlist_id: str,
                               item_ids: list[str]) -> dict:
        """Add items to an existing playlist."""
        return await self.post(
            f"/Playlists/{playlist_id}/Items",
            params={"Ids": ",".join(item_ids)},
        )

    async def remove_from_playlist(self, playlist_id: str,
                                    item_ids: list[str]) -> dict:
        """Remove items from a playlist."""
        return await self.delete(
            f"/Playlists/{playlist_id}/Items",
            params={"Ids": ",".join(item_ids)},
        )

    async def move_playlist_item(self, playlist_id: str, item_id: str,
                                  new_index: int) -> dict:
        """Move an item to a new position in a playlist."""
        return await self.post(
            f"/Playlists/{playlist_id}/Items/{item_id}/Move/{new_index}",
        )

    # -- Helpers --

    @staticmethod
    def _slim_items(items: list[dict]) -> list[dict]:
        """Extract useful fields from Jellyfin item dicts."""
        results = []
        for item in items:
            user_data = item.get("UserData", {})
            results.append({
                "item_id": item["Id"],
                "name": item.get("Name", "Unknown"),
                "year": item.get("ProductionYear"),
                "type": item.get("Type"),
                "overview": (item.get("Overview") or "")[:200],
                "is_favourite": user_data.get("IsFavorite", False),
                "is_played": user_data.get("Played", False),
            })
        return results
