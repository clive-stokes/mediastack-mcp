# Seerr Deletion Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface "what got deleted from Radarr/Sonarr" forensically via Seerr's media-tracking pipeline, and add the ability to purge Seerr's media record so a title becomes re-requestable.

**Architecture:**
- A new poller hook snapshots Seerr's full `/api/v1/media` list each cycle, diffs status against the previous snapshot, and emits a `seerr_media_unavailable` event into `media_events` whenever a media drops below status 4 (`PARTIALLY_AVAILABLE`). Pattern mirrors the existing qBittorrent state-diff at `app/poller.py:156-171`.
- A new read tool `mediastack_seerr_deletion_audit(days, media_type)` queries those events from `media_events`, returning `[{tmdb_id, tvdb_id, title, media_type, dropped_at, previous_status, current_status, recovered}]`, ready to feed back into `mediastack_add_content` / `mediastack_request_content`.
- A new write tool `mediastack_delete_seerr_media(media_id)` calls `DELETE /api/v1/media/{id}` via the existing confirmation protocol (5-min expiry; no file deletion involved so no shortened expiry).
- The snapshot is in-memory only (like `_qbt_prev_torrents`). After container restart the first cycle reseeds with no events emitted — correct: we cannot infer state changes when we don't know the prior state.

**Tech Stack:** Python 3.14, FastMCP, httpx async, PostgreSQL (existing `media_events` table — no schema changes needed). Verification via `docker compose build && up -d`, `/health` curl, `docker logs`, and direct MCP tool calls.

**Note on testing:** mediastack-mcp has no `tests/` directory. The project pattern is integration-via-running, so every task ends with a build + log + tool-call smoke test rather than pytest. This deliberately follows the established convention rather than introducing test infra for one feature.

---

## File Map

| File | Change |
|------|--------|
| `app/clients/seerr.py` | Add `get_all_media()`, `get_media_by_id()`, `delete_media()`, `parse_media_diff_events()` |
| `app/poller.py` | Add `_seerr_prev_media_status: dict[int, int]`, call `_poll_seerr_media()` from `_poll_seerr()` |
| `app/server.py` | Add `mediastack_seerr_deletion_audit` (read) + `mediastack_delete_seerr_media` (write); extend the confirm-event-type map at line 346 to include `delete_seerr_media`; bump `MEDIASTACK_BUILD` |
| `CLAUDE.md` (mediastack-mcp root) | Update tool counts (16→17 read, 14→15 write) and Seerr service row |
| `/volume3/docker/docs/CHANGELOG.md` | Top-of-file entry for 2026-05-12 |
| `/volume3/docker/CLAUDE.md` | Recent Changes top-of-list entry |

---

### Task 1: Seerr client — media-list endpoints

**Files:**
- Modify: `app/clients/seerr.py`

- [ ] **Step 1: Add `get_all_media()` paginating through `/api/v1/media`**

Append to `app/clients/seerr.py` after `get_request_count()` (around line 36):

```python
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
```

- [ ] **Step 2: Add `get_media_by_id()` and `delete_media()`**

Append after `get_all_media()`:

```python
    async def get_media_by_id(self, media_id: int) -> dict:
        """Fetch a single Seerr media record by its internal id."""
        return await self.get(f"/api/v1/media/{media_id}")

    async def delete_media(self, media_id: int) -> dict:
        """Delete a Seerr media record (does NOT touch *arr files).

        Removes Seerr's tracking entry so the title becomes re-requestable.
        Returns {"status": "deleted"} on a 204.
        """
        return await self.delete(f"/api/v1/media/{media_id}")
```

- [ ] **Step 3: Add `parse_media_diff_events()` state-diff helper**

Append to the `SeerrClient` class (after `parse_request_event`):

```python
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
```

Note: Seerr's `/api/v1/media` results carry `mediaType` not `media_type` — keep the original key in the API call, normalise into snake_case only in the event metadata. The title field on a media record is typically `title` for movies and `name` for TV; the chain above handles both.

- [ ] **Step 4: Build & confirm the container compiles**

Run:
```bash
cd /volume3/docker/mediastack-mcp
docker compose build
```
Expected: build succeeds, no syntax errors.

- [ ] **Step 5: Commit**

```bash
cd /volume3/docker/mediastack-mcp
git add app/clients/seerr.py
git commit -m "feat(seerr): add media-list, media-by-id, delete-media, and diff-event helper"
```

---

### Task 2: Poller integration — snapshot and diff

**Files:**
- Modify: `app/poller.py:36, 192-202`

- [ ] **Step 1: Add `_seerr_prev_media_status` to `Poller.__init__`**

In `app/poller.py`, find the `__init__` block (around line 33-40):

```python
    def __init__(self, config: Config):
        self.config = config
        self._clients: dict = {}
        self._qbt_prev_torrents: list[dict] = []
        self._running = False

        self._init_clients()
```

Change to:

```python
    def __init__(self, config: Config):
        self.config = config
        self._clients: dict = {}
        self._qbt_prev_torrents: list[dict] = []
        self._seerr_prev_media_status: dict[int, int] = {}
        self._running = False

        self._init_clients()
```

- [ ] **Step 2: Add `_poll_seerr_media()` method**

In `app/poller.py`, after the existing `_poll_seerr()` (currently ends around line 202), add a sibling method:

```python
    async def _poll_seerr_media(self) -> None:
        """Snapshot Seerr's full media list and diff for availability drops.

        Emits `seerr_media_unavailable` events when media drops below status 4
        (a Radarr/Sonarr deletion that Seerr has noticed). The previous snapshot
        is in-memory only — first cycle after restart reseeds without emitting.
        """
        client: SeerrClient = self._clients["seerr"]
        try:
            current = await client.get_all_media()
            if self._seerr_prev_media_status:
                events, new_snapshot = client.parse_media_diff_events(
                    current, self._seerr_prev_media_status,
                )
                inserted = db.insert_events(events)
                if inserted:
                    logger.info("[seerr] Recorded %d media availability drops", inserted)
                self._seerr_prev_media_status = new_snapshot
            else:
                # First cycle — seed the snapshot, emit nothing.
                _, new_snapshot = client.parse_media_diff_events(current, {})
                self._seerr_prev_media_status = new_snapshot
                logger.info("[seerr] Seeded media snapshot with %d titles", len(new_snapshot))
        except Exception:
            logger.exception("[seerr] Failed to poll media list")
```

- [ ] **Step 3: Wire the call into `_poll_seerr()`**

Find the existing `_poll_seerr` (currently lines 192-202):

```python
    async def _poll_seerr(self) -> None:
        client: SeerrClient = self._clients["seerr"]
        try:
            data = await client.get_requests()
            requests = data.get("results", []) if isinstance(data, dict) else []
            events = [client.parse_request_event(r) for r in requests]
            inserted = db.insert_events(events)
            if inserted:
                logger.info("[seerr] Recorded %d new events", inserted)
        except Exception:
            logger.exception("[seerr] Failed to poll requests")
```

Replace with (adds a single trailing call):

```python
    async def _poll_seerr(self) -> None:
        client: SeerrClient = self._clients["seerr"]
        try:
            data = await client.get_requests()
            requests = data.get("results", []) if isinstance(data, dict) else []
            events = [client.parse_request_event(r) for r in requests]
            inserted = db.insert_events(events)
            if inserted:
                logger.info("[seerr] Recorded %d new events", inserted)
        except Exception:
            logger.exception("[seerr] Failed to poll requests")

        # Media availability diff — separate from request pipeline.
        await self._poll_seerr_media()
```

- [ ] **Step 4: Build & deploy**

Run:
```bash
cd /volume3/docker/mediastack-mcp
docker compose build && docker compose up -d
```
Expected: build succeeds, container restarts cleanly.

- [ ] **Step 5: Verify first cycle seeds, second cycle diffs**

Wait one full poll interval (default 300s — check `MEDIASTACK_POLL_INTERVAL` env), then:

```bash
docker logs --tail 200 mediastack-mcp 2>&1 | grep -i seerr
```
Expected: a `[seerr] Seeded media snapshot with <N> titles` line on first cycle, no `Recorded N media availability drops` (because nothing has changed yet).

Then leave it running for ≥10 minutes. If nothing changes in Seerr, no drop events are emitted (correct). To force a synthetic event for verification, temporarily mark one Seerr media record as unavailable via Seerr's web UI ("Remove from Plex/Jellyfin" or equivalent), then check:

```bash
docker exec -i postgres psql -U "$DB_MEDIASTACK_USER" -d "$DB_MEDIASTACK_NAME" -c "SELECT id, timestamp, title, metadata FROM media_events WHERE event_type='seerr_media_unavailable' ORDER BY id DESC LIMIT 5;"
```

Expected: at least one row with tmdb_id populated in metadata. Revert the Seerr UI change after verifying.

- [ ] **Step 6: Commit**

```bash
cd /volume3/docker/mediastack-mcp
git add app/poller.py
git commit -m "feat(seerr): snapshot media list and emit seerr_media_unavailable on availability drops"
```

---

### Task 3: MCP tool — `mediastack_seerr_deletion_audit`

**Files:**
- Modify: `app/server.py` (insert new tool below `mediastack_ghost_history`, before the Write tools section)

- [ ] **Step 1: Find the insertion point**

Locate where `mediastack_ghost_history` ends. Search for `def mediastack_ghost_history` and find the closing `return json.dumps(...)` of that function. The new tool inserts directly below it, before the `# -- Write Operations --` section header (or equivalent — search for the next tool, likely `mediastack_search_content`).

- [ ] **Step 2: Insert `mediastack_seerr_deletion_audit`**

Add this tool (preserving the surrounding section header that already exists):

```python
@mcp_app.tool()
def mediastack_seerr_deletion_audit(days: int = 30, media_type: str | None = None) -> str:
    """Audit Seerr media records that have dropped from 'available' status.

    Surfaces titles whose *arr file was deleted (e.g. by a Trakt-list
    "Remove and Delete" rule) and which Seerr noticed via webhook. Each
    result includes the TMDB/TVDB IDs needed to re-add via
    mediastack_add_content or mediastack_request_content.

    The 'recovered' flag is True if a later poll observed the media
    returning to status >= 4 (e.g. you re-downloaded it).

    Args:
        days: How many days back to scan (default 30, max 365)
        media_type: Filter to 'movie' or 'tv' (default: both)
    """
    if days < 1 or days > 365:
        return json.dumps({"error": "days must be between 1 and 365"})
    if media_type and media_type not in ("movie", "tv"):
        return json.dumps({"error": "media_type must be 'movie', 'tv', or omitted"})

    try:
        # Pull all unavailable events in the window.
        conn = db._conn()
        try:
            import psycopg2.extras
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if media_type:
                    cur.execute(
                        """
                        SELECT id, timestamp, title, metadata
                        FROM media_events
                        WHERE event_type = 'seerr_media_unavailable'
                          AND timestamp >= NOW() - (%s || ' days')::INTERVAL
                          AND metadata->>'media_type' = %s
                        ORDER BY timestamp DESC
                        """,
                        (days, media_type),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, timestamp, title, metadata
                        FROM media_events
                        WHERE event_type = 'seerr_media_unavailable'
                          AND timestamp >= NOW() - (%s || ' days')::INTERVAL
                        ORDER BY timestamp DESC
                        """,
                        (days,),
                    )
                rows = cur.fetchall()
        finally:
            conn.close()

        # Resolve current status via one live Seerr API call (so 'recovered'
        # reflects right-now state, not the value frozen at drop time).
        out: list[dict] = []
        media_ids = [r["metadata"].get("media_id") for r in rows if r["metadata"].get("media_id")]
        client = _get_poller_client("seerr")
        live_status: dict[int, int] = {}
        if client and media_ids:
            try:
                current = _run_async(client.get_all_media())
                for m in current:
                    if m.get("id") in media_ids:
                        live_status[m["id"]] = m.get("status", 0)
            except Exception as e:
                logger.warning("seerr_deletion_audit: live status lookup failed: %s", e)

        for r in rows:
            meta = r["metadata"] or {}
            mid = meta.get("media_id")
            current_status = live_status.get(mid, meta.get("current_status"))
            recovered = bool(current_status and current_status >= 4)
            out.append({
                "media_id": mid,
                "title": r["title"],
                "media_type": meta.get("media_type"),
                "tmdb_id": meta.get("tmdb_id"),
                "tvdb_id": meta.get("tvdb_id"),
                "imdb_id": meta.get("imdb_id"),
                "dropped_at": r["timestamp"].isoformat(),
                "previous_status": meta.get("previous_status"),
                "current_status": current_status,
                "recovered": recovered,
            })

        return json.dumps({
            "count": len(out),
            "window_days": days,
            "results": out,
        }, indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})
```

- [ ] **Step 3: Bump `MEDIASTACK_BUILD` (server.py line 38)**

Find:
```python
MEDIASTACK_BUILD = "2026-04-17.1"
```

Change to:
```python
MEDIASTACK_BUILD = "2026-05-12.1"
```

- [ ] **Step 4: Build & deploy**

```bash
cd /volume3/docker/mediastack-mcp
docker compose build && docker compose up -d
```

- [ ] **Step 5: Verify the tool is registered and returns data**

```bash
curl -sf http://127.0.0.1:9202/health | jq .build
```
Expected: `"2026-05-12.1"`.

Then invoke the tool via an MCP client (nanobot or `claude mcp call`):
```
mediastack_seerr_deletion_audit(days=30)
```
Expected: JSON object with `count`, `window_days: 30`, `results: [...]`. If no drops have happened, `count: 0` is correct.

- [ ] **Step 6: Commit**

```bash
cd /volume3/docker/mediastack-mcp
git add app/server.py
git commit -m "feat(seerr): add mediastack_seerr_deletion_audit read tool"
```

---

### Task 4: MCP tool — `mediastack_delete_seerr_media`

**Files:**
- Modify: `app/server.py` (insert below `mediastack_cancel_request`, around line 1004; extend confirm event-type map at line 346)

- [ ] **Step 1: Insert the delete tool**

Locate the closing brace of `mediastack_cancel_request` (around line 1004). Insert below it, before `# -- Jellyfin User Operations --`:

```python
@mcp_app.tool()
def mediastack_delete_seerr_media(media_id: int) -> str:
    """Delete a Seerr media tracking record. Returns a preview; requires confirmation.

    Removes Seerr's internal media entry so the title becomes re-requestable
    in the Seerr UI. This does NOT delete any files or touch Sonarr/Radarr —
    it only clears Seerr's memory that the title was ever requested or
    available. Useful after `mediastack_seerr_deletion_audit` identifies
    titles whose *arr files were deleted and you want Seerr to forget them.

    Args:
        media_id: Seerr's internal media id (not request_id, not tmdb_id).
                  Obtain via mediastack_seerr_deletion_audit.
    """
    client = _get_poller_client("seerr")
    if not client:
        return json.dumps({"error": "Seerr is not configured"})

    try:
        media = _run_async(client.get_media_by_id(media_id))
        media_type = media.get("mediaType", "unknown")
        title = media.get("title") or media.get("name") or f"media #{media_id}"
        status = media.get("status", 0)
        status_map = {1: "unknown", 2: "pending", 3: "processing", 4: "partially_available", 5: "available"}
        status_label = status_map.get(status, f"status_{status}")

        preview = {
            "action": "delete_seerr_media",
            "service": "seerr",
            "title": title,
            "media_type": media_type,
            "media_id": media_id,
            "tmdb_id": media.get("tmdbId"),
            "tvdb_id": media.get("tvdbId"),
            "current_status": status_label,
            "warning": (
                f"This removes Seerr's tracking record for '{title}'. "
                "No files are deleted and Sonarr/Radarr are not affected. "
                "The title will become re-requestable in Seerr's UI."
            ),
        }
        description = f"Delete Seerr media record for '{title}' (media #{media_id}, {status_label})"

        async def execute():
            return await client.delete_media(media_id)

        action = confirmation_store.create(description, preview, execute)
        return json.dumps({
            "preview": preview,
            "confirmation_id": action.confirmation_id,
            "message": f"{description}. Call mediastack_confirm('{action.confirmation_id}') to execute.",
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})
```

- [ ] **Step 2: Extend confirm event-type mapping**

Find line 346 in `app/server.py`:

```python
        if action.preview.get("action") in ("delete", "cancel_request"):
            event_type = "delete_confirmed"
```

Change to:

```python
        if action.preview.get("action") in ("delete", "cancel_request", "delete_seerr_media"):
            event_type = "delete_confirmed"
```

- [ ] **Step 3: Build & deploy**

```bash
cd /volume3/docker/mediastack-mcp
docker compose build && docker compose up -d
```

- [ ] **Step 4: Verify the tool is registered**

```bash
curl -sf http://127.0.0.1:9202/health | jq
```
Expected: 200 OK.

Invoke the tool via MCP client against a known-disposable media id (you can identify one from `mediastack_seerr_deletion_audit` — pick a title you already removed and don't intend to re-request):

```
mediastack_delete_seerr_media(media_id=<N>)
```
Expected: JSON with `preview`, `confirmation_id`. Then call `mediastack_confirm(<id>)`. Expected: `status: success`. Then verify in Seerr UI that the title is now requestable again.

- [ ] **Step 5: Commit**

```bash
cd /volume3/docker/mediastack-mcp
git add app/server.py
git commit -m "feat(seerr): add mediastack_delete_seerr_media write tool with confirmation"
```

---

### Task 5: Documentation updates

**Files:**
- Modify: `/volume3/docker/mediastack-mcp/CLAUDE.md`
- Modify: `/volume3/docker/docs/CHANGELOG.md`
- Modify: `/volume3/docker/CLAUDE.md`

- [ ] **Step 1: Update mediastack-mcp CLAUDE.md tool counts and Seerr row**

In `/volume3/docker/mediastack-mcp/CLAUDE.md`:

Change `### Read (16 tools)` to `### Read (17 tools)` and add after `mediastack_ghost_history` (around line 61):

```markdown
- `mediastack_seerr_deletion_audit` — Forensic audit of media that dropped from Seerr's "available" status (e.g. after a Radarr Trakt-list deletion); returns tmdb/tvdb IDs ready for re-add
```

Change `### Write (14 tools, all require confirmation)` to `### Write (15 tools, all require confirmation)` and add after `mediastack_cancel_request` (around line 71):

```markdown
- `mediastack_delete_seerr_media` — Delete Seerr's media tracking record (makes title re-requestable; no files touched)
```

In the Services table, update the Seerr row's "Write" column from `request` to `request, delete-media-record`.

In the "Key Design Decisions" section, append:

```markdown
- Seerr media diff: poller snapshots `/api/v1/media` each cycle and emits `seerr_media_unavailable` events when status drops from ≥4 to <4 (recovery signal for Radarr/Sonarr file deletions); in-memory prev-state map, first cycle after restart seeds without emitting
```

- [ ] **Step 2: Add top-of-file CHANGELOG entry**

Edit `/volume3/docker/docs/CHANGELOG.md` — add a new entry dated 2026-05-12 at the top of the chronological list. Example:

```markdown
**2026-05-12:** mediastack-mcp gained Seerr deletion forensics — new `mediastack_seerr_deletion_audit` read tool surfaces media that Seerr noticed dropping from "available" (e.g. Radarr Trakt-list "Remove and Delete" accidents), returning tmdb/tvdb IDs ready to feed into `mediastack_add_content`. Companion write tool `mediastack_delete_seerr_media` purges Seerr's tracking record so a title becomes re-requestable without touching files or *arr state. Poller diffs `/api/v1/media` snapshots each cycle, emits `seerr_media_unavailable` events into `media_events`. Build: `2026-05-12.1`.
```

- [ ] **Step 3: Add top-of-list entry to /volume3/docker/CLAUDE.md "Recent Changes"**

Edit `/volume3/docker/CLAUDE.md` — add at the top of the "Recent Changes" section:

```markdown
**2026-05-12:** mediastack-mcp gained `mediastack_seerr_deletion_audit` (read) + `mediastack_delete_seerr_media` (write) for recovering from Radarr Trakt-list deletion accidents. Audit tool surfaces tmdb/tvdb IDs for media Seerr noticed dropping from "available"; delete tool purges Seerr's tracking record so titles become re-requestable. Full notes in `docs/CHANGELOG.md`.
```

- [ ] **Step 4: Commit — split per repo**

Two repos are involved (per the `project_mediastack_mcp_local_clone` memory: mediastack-mcp source lives in `/volume3/docker/mediastack-mcp.repo`, while `/volume3/docker` is the NASgnolia infrastructure repo).

mediastack-mcp source repo commit (covers everything under `/volume3/docker/mediastack-mcp/` that is tracked there — `app/`, `CLAUDE.md`, the plan doc):

```bash
cd /volume3/docker/mediastack-mcp.repo
git add app/ CLAUDE.md docs/superpowers/plans/2026-05-12-seerr-deletion-audit.md
git commit -m "docs: update tool index for Seerr deletion-audit tools"
```

NASgnolia infrastructure repo commit (covers the top-level CHANGELOG and CLAUDE.md):

```bash
cd /volume3/docker
git add docs/CHANGELOG.md CLAUDE.md
git commit -m "docs: changelog entry for mediastack-mcp Seerr deletion-audit tools"
```

If `git status` in either repo shows unexpected staged paths, stop and inspect before committing. Per the `project_nasgnolia_remote_misconfig` memory, always `git remote -v` before any push.

---

### Task 6: End-to-end smoke test

**Files:** none (verification only)

- [ ] **Step 1: Confirm build version surfaces**

```bash
curl -sf http://127.0.0.1:9202/health | jq .
```
Expected: `"build": "2026-05-12.1"`, `"status": "ok"`.

- [ ] **Step 2: Confirm seed log message appears on first cycle**

```bash
docker logs --tail 500 mediastack-mcp 2>&1 | grep -i "Seeded media snapshot"
```
Expected: exactly one line, e.g. `[seerr] Seeded media snapshot with 247 titles`.

- [ ] **Step 3: Confirm both new tools are listed**

Via MCP client (nanobot, claude-code, or `claude mcp list-tools mediastack`):
```
grep mediastack_seerr_deletion_audit
grep mediastack_delete_seerr_media
```
Both must appear.

- [ ] **Step 4: Run audit tool live**

```
mediastack_seerr_deletion_audit(days=30)
```
Even if `count: 0` on first run, the call should succeed and return well-formed JSON.

- [ ] **Step 5: Confirm no regression in existing Seerr request flow**

```
mediastack_cancel_request(request_id=<a-known-id>)
```
Then call confirm. Should still work — the diff loop is additive.

- [ ] **Step 6: Final commit only if any tweaks needed**

If any of the above surfaced a bug requiring an inline fix:
```bash
cd /volume3/docker/mediastack-mcp
git add <files>
git commit -m "fix(seerr): <specific>"
```
Otherwise no commit.

---

## Risks & Considerations

- **Pagination cost on large Seerr libraries.** A library of 1000 tracked media at `take=100` = 10 sequential API calls per poll cycle. At the default 300s poll interval that's manageable. If profiling shows this is too noisy, factor out a separate `seerr_media_diff_interval` env var with a longer default (e.g. 600s). Not done up-front per YAGNI.
- **Title field shape on TV records.** Seerr's media records use `name` for TV and `title` for movies (mirroring TMDB). The diff helper handles both via `m.get("title") or m.get("name")`. Verify this matches your Seerr version's actual JSON by hitting `curl -s -H "X-Api-Key: $SEERR_API_KEY" http://seerr.nasgnolia.uk/api/v1/media?take=2` early in Task 1 and confirming the field names.
- **First-cycle silence is correct.** No events emit on initial seed (or after restart) because we cannot reconstruct prior state. The user's existing-historical-deletion case is unrecoverable via this mechanism — they need Seerr's own DB, which is what they already did manually. This plan addresses *future* deletions.
- **Audit tool makes a live Seerr API call per invocation** to resolve `recovered`. If Seerr is down, the tool gracefully falls back to the `current_status` recorded at drop time (status from before the drop). Acceptable.
- **`get_media_by_id` may 404** if the user supplies an invalid id. Falls through to `httpx.HTTPStatusError`, caught by the outer `try/except` in the tool, returning a clean error JSON.
- **No schema migration needed.** `seerr_media_unavailable` is just a new `event_type` string in the existing `media_events` table.

---

## Self-Review Checklist (run before handing off)

- [ ] Every step has either runnable code or a concrete shell command — no "TBD" / "appropriate" / "as needed".
- [ ] Method names line up across tasks: `get_all_media`, `get_media_by_id`, `delete_media`, `parse_media_diff_events`, `_poll_seerr_media`, `_seerr_prev_media_status`, `mediastack_seerr_deletion_audit`, `mediastack_delete_seerr_media` — used identically wherever referenced.
- [ ] Event type string `seerr_media_unavailable` matches between the diff helper, the audit tool query, and the documentation.
- [ ] The action key `delete_seerr_media` matches between the write tool's preview dict and the confirm-tool event-type mapping at server.py:346.
- [ ] Confirm protocol expiry: 5 minutes (default `EXPIRY_SECONDS`) — appropriate since no file deletion is involved.
- [ ] Build version bumped (`MEDIASTACK_BUILD`).
- [ ] Tool counts in CLAUDE.md (read 16→17, write 14→15) match the actual additions.
