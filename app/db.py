"""Database abstraction layer for MediaStack.

Routes to db_postgres or db_sqlite based on DB_ENGINE env var.
All callers import from this module — the engine choice is transparent.
"""

import os
import types

_impl: types.ModuleType | None = None


def init(db_url: str) -> None:
    """Initialise the database engine and create schema."""
    global _impl
    engine = os.environ.get("DB_ENGINE", "postgres").lower()
    if engine == "sqlite":
        from app import db_sqlite as mod
    else:
        from app import db_postgres as mod
    _impl = mod
    _impl.init(db_url)


def get_engine() -> str:
    """Return the active engine name ('postgres' or 'sqlite')."""
    if _impl is None:
        return "uninitialised"
    return "sqlite" if "sqlite" in _impl.__name__ else "postgres"


# -- Delegated public API --
# Every function below delegates to the active engine module.

def _conn():
    return _impl._conn()


def insert_event(event: dict) -> bool:
    return _impl.insert_event(event)


def insert_events(events: list[dict]) -> int:
    return _impl.insert_events(events)


def get_timeline(hours: int = 24, source: str | None = None,
                 event_type: str | None = None, limit: int = 100) -> list[dict]:
    return _impl.get_timeline(hours=hours, source=source, event_type=event_type, limit=limit)


def search_events(query: str, days: int = 30, source: str | None = None) -> list[dict]:
    return _impl.search_events(query=query, days=days, source=source)


def insert_library_snapshot(source: str, library_name: str, item_count: int,
                            size_bytes: int | None = None) -> None:
    _impl.insert_library_snapshot(source, library_name, item_count, size_bytes)


def get_libraries_current() -> list[dict]:
    return _impl.get_libraries_current()


def get_library_delta(source: str, library_name: str) -> dict | None:
    return _impl.get_library_delta(source, library_name)


def get_event_summary(hours: int = 24) -> list[dict]:
    return _impl.get_event_summary(hours=hours)


def insert_storage_snapshot(mount_point: str, total_bytes: int, used_bytes: int, source: str) -> None:
    _impl.insert_storage_snapshot(mount_point, total_bytes, used_bytes, source)


def get_storage_current() -> list[dict]:
    return _impl.get_storage_current()


def get_storage_growth(mount_point: str, days: int = 7) -> dict | None:
    return _impl.get_storage_growth(mount_point, days=days)


def get_latest_health() -> list[dict]:
    return _impl.get_latest_health()


def upsert_health(service: str, status: str, detail: str | None = None) -> None:
    _impl.upsert_health(service, status, detail)


def get_stats() -> dict:
    return _impl.get_stats()
