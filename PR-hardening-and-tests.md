# PR: Operational hardening, test suite, and documentation fixes

**Branch:** `fix/hardening-and-tests` → `main`
**Type:** Fix / Hardening / Tests
**Breaking changes:** None (one new optional env var)

-----

## Summary

Closes the gaps identified in the v1.0.0 code review: a broken quick start, an unsubstantiated tests claim in the README, blocking database calls inside the asyncio event loop, per-request HTTP client construction, an unauthenticated `/mcp` and `/ingest` surface on the Docker network, an unpinned MCP SDK underneath a monkey-patch of its internals, and documentation drift (Python version, threading model).

No tool signatures change. Existing MCP clients continue to work unmodified; the only behavioural change is that requests without a bearer token are rejected **when `MEDIASTACK_AUTH_TOKEN` is set** (unset = current behaviour, so existing deployments are unaffected until they opt in).

-----

## Motivation

This server can permanently delete media files. The confirmation protocol is sound by inspection, but “by inspection” is not a regression guard — the delete/confirm path has zero test coverage. Separately, every container on `npm-network` can currently reach the MCP endpoint (and therefore the delete tools) without credentials, and the event loop stalls on every database write because psycopg2 is called synchronously from coroutines.

-----

## Changes

### Commit 1 — Documentation fixes (no code)

- Add `.env.example` with placeholder values for all 13 services, database variables, polling intervals, and the new `MEDIASTACK_AUTH_TOKEN`. Unblocks step 2 of the quick start, which currently references a file that does not exist.
- README: correct Python version (3.11 → 3.14, matching the Dockerfile digest pin).
- README: Architecture section — “background daemon thread” → “asyncio background tasks” (matches `poller.py`).
- README: remove the claim that tests were written, replaced in Commit 5 by a claim that is actually true.

### Commit 2 — Bearer token auth on `/mcp` and `/ingest`

- New optional env var `MEDIASTACK_AUTH_TOKEN`. When set, a Starlette middleware rejects requests to `/mcp` and `/ingest` lacking `Authorization: Bearer <token>` with 401. `/health` remains open (Docker healthcheck depends on it and it exposes nothing sensitive beyond counts).
- Token comparison uses `secrets.compare_digest` to avoid timing side-channels. Cheap insurance, two lines.
- README: document token setup for Claude Code / Claude Desktop via `mcp-remote`’s `--header` flag.

**Rationale:** host port binding to 127.0.0.1 protects against the LAN, not against the ~15 other containers sharing `npm-network`. Any compromised container currently has unauthenticated access to file deletion tools.

### Commit 3 — Persistent HTTP clients

- `ArrClient` and `JellyfinClient` create one `httpx.AsyncClient` in `__init__` (with the service’s base URL and headers) and reuse it for all requests; add `async def close()`.
- `Poller.stop()` closes all clients; wired into server shutdown.
- Removes per-request TCP/TLS handshakes — at default intervals across 13 services this is several thousand avoidable connections per day.

### Commit 4 — Stop blocking the event loop on database I/O

- Introduce `psycopg2.pool.ThreadedConnectionPool` (min 1, max 5) in `db.py`; replace per-call `psycopg2.connect()`.
- All `db.*` calls made from coroutines in `poller.py` are wrapped in `asyncio.to_thread(...)`, consistent with the existing `du` scan pattern at `poller.py:364`.
- Sync MCP tools continue to call `db.*` directly — they already run in FastMCP’s worker threads, so no change needed there.

**Deliberately out of scope:** migration to psycopg3 async. It would be cleaner but touches every query; the pool + `to_thread` combination removes the event-loop stall with a fraction of the diff. Candidate for a future PR.

### Commit 5 — Test suite (detail in Test Plan below)

- `tests/` package with pytest, respx, and fixtures; `requirements-dev.txt`.
- Recorded JSON fixtures for Sonarr/Radarr/Lidarr item lookups (sanitised — no real titles, paths, or keys).

### Commit 6 — Pin the MCP SDK against the monkey-patch

- `requirements.txt`: `mcp[cli]>=1.0.0` → a pinned compatible range (e.g. `mcp[cli]>=1.0,<2.0`, exact ceiling set after checking which versions the patch survives).
- Comment block beside the `_validate_accept_header` patch in `server.py`: why it exists, which SDK versions it was verified against, and the upstream issue to watch for its removal.
- A unit test asserts the patched attribute still exists on import, so an SDK upgrade that renames the internal fails loudly in CI instead of silently at runtime.

### Commit 7 — CI

- GitHub Actions workflow: lint (ruff), then pytest tiers 1–3 on push/PR. Postgres provided as a `services:` block. No secrets required — the live tier is excluded by marker.

-----

## Test Plan

Four tiers. Tiers 1–3 run anywhere (laptop, CI) with no media services and no credentials. Tier 4 is manual, opt-in, and read-only.

### Tier 1 — Pure logic (no I/O)

`tests/test_confirmations.py`

- Create → confirm executes exactly once; second confirm with same ID returns not-found.
- Expired action: `get()` returns None; `get_expired()` returns it; renewal issues a fresh ID with reset expiry and the original `execute_fn` (asserted via a recording fake).
- Expired **destructive** action cannot execute without a second explicit confirm after renewal — the property the whole protocol exists to guarantee.
- Cleanup purges actions past expiry + retention window; concurrent create/get from threads leaves the store consistent.

`tests/test_filesystem.py`

- `validate_scan_path`: traversal escape (`/media/../etc`) rejected; symlink pointing outside media roots rejected after resolution; exact root and nested paths accepted; empty roots rejected.

`tests/test_config.py`

- Env var permutations via `monkeypatch`: URL without key → service skipped; valid `LIBRARY_CRON` accepted; malformed (`25:00`, `0230`) falls back to interval with a warning.

`tests/test_auth.py`

- Token set: `/mcp` and `/ingest` 401 without header, 200 with; `/health` open either way. Token unset: everything open (back-compat).

### Tier 2 — Mocked HTTP (respx, fixture replay)

`tests/test_delete_preview.py`

- For each media type: preview contains correct title, path, human-readable size, and external ID parsed from the fixture.
- `delete_files=True` → warning contains the permanent-deletion text and expiry is 120 s; `False` → 300 s and “will NOT be deleted” phrasing.
- Service returns 404 → tool returns a JSON error, **no pending confirmation is created**.

`tests/test_clients.py`

- Correct auth header per client type (X-Api-Key vs MediaBrowser token); 204/empty-body DELETE handled; `ping()` returns False on connect error rather than raising.

`tests/test_poller_events.py`

- *arr history fixture → expected event dicts with stable `source_event_id`s; same fixture polled twice produces identical IDs (dedup precondition).

### Tier 3 — Real (throwaway) PostgreSQL

Via `testcontainers` locally, `services:` block in CI.

`tests/test_db.py`

- Schema creation idempotent (run twice).
- Dedup: same `source_event_id` inserted twice → one row; `insert_events` returns the new-row count, not the batch size; NULL `source_event_id` rows never collide (partial index behaviour).
- Retention rollup: seed 100 events >90 days old → daily summary rows created with correct per-type counts, raw rows purged, recent rows untouched; second run is a no-op.
- Storage growth: seed snapshots with known slope → `bytes_per_day` within tolerance.

### Tier 4 — Live smoke test (manual, opt-in)

`pytest -m live`, excluded from CI, requires the production `.env`, **read-only tools only**: `/health` returns ok with expected build string; `mediastack_health`, `mediastack_stats`, `mediastack_timeline(hours=1)` return parseable JSON. No write or confirm tools are ever exercised live — Tier 2 owns that path against fixtures.

### Manual verification (this PR, on the NAS)

1. `docker compose build && docker compose up -d` — healthcheck goes healthy within `start_period`.
1. Without `MEDIASTACK_AUTH_TOKEN`: existing Claude Code connection works unchanged.
1. Set token, restart: connection without header rejected; with header, `mediastack_timeline` returns events.
1. One full delete cycle on a sacrificial library entry (`delete_files=False`): preview → confirm → entry removed from Sonarr, files intact.
1. Watch logs over one polling cycle for connection-pool or client-reuse errors.

-----

## Risk and rollback

- Auth is opt-in; worst case is a misconfigured token producing 401s, fixed by unsetting the variable.
- Pool + `to_thread` changes are mechanical; failure mode is loud (connection errors in logs), not silent data loss, and dedup makes re-polling safe.
- Rollback: revert and redeploy. Schema is untouched.

## Checklist

- [ ] `.env.example` committed with placeholders only — verified no real keys (`git diff` eyeballed + gitleaks pass)
- [ ] Tiers 1–3 green in CI
- [ ] Tier 4 + manual verification run on the NAS
- [ ] README quick start followed verbatim from a clean clone, completes without error
- [ ] RELEASE_NOTES.md entry added