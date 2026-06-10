"""Tier 1 — service auto-discovery from environment variables."""

import pytest

from app.config import Config

# Every env var Config.from_env() reads — cleared before each test so the
# host environment can't leak into assertions.
ALL_VARS = [
    "MEDIASTACK_DB_URL", "DB_MEDIASTACK_USER", "DB_MEDIASTACK_PASS",
    "DB_MEDIASTACK_NAME", "POSTGRES_HOST", "POSTGRES_PORT",
    "MEDIASTACK_POLL_INTERVAL", "MEDIASTACK_STORAGE_INTERVAL",
    "MEDIASTACK_LIBRARY_INTERVAL", "MEDIASTACK_LIBRARY_CRON",
    "SONARR_URL", "SONARR_API_KEY", "RADARR_URL", "RADARR_API_KEY",
    "LIDARR_URL", "LIDARR_API_KEY", "PROWLARR_URL", "PROWLARR_API_KEY",
    "BAZARR_URL", "BAZARR_API_KEY", "SEERR_URL", "SEERR_API_KEY",
    "JELLYFIN_URL", "JELLYFIN_API_KEY",
    "AUDIOBOOKSHELF_URL", "AUDIOBOOKSHELF_API_KEY",
    "SABNZBD_URL", "SABNZBD_API_KEY",
    "QBITTORRENT_URL", "QBITTORRENT_USERNAME", "QBITTORRENT_USER",
    "QBITTORRENT_PASSWORD", "QBITTORRENT_PASS",
    "DISPATCHARR_URL", "DISPATCHARR_USERNAME", "DISPATCHARR_PASSWORD",
    "BOXARR_URL",
    "SUGGESTARR_URL", "SUGGESTARR_USERNAME", "SUGGESTARR_PASSWORD",
    "FILESYSTEM_SCAN_ENABLED", "MEDIA_ROOTS", "MEDIA_PATH_MAPPINGS",
    "GHOST_HISTORY_LIMIT",
    "PROWLARR_CATEGORIES_MOVIE", "PROWLARR_CATEGORIES_TV",
    "PROWLARR_CATEGORIES_MUSIC", "PROWLARR_CATEGORIES_BOOK",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ALL_VARS:
        monkeypatch.delenv(var, raising=False)


def test_url_without_key_skips_service(monkeypatch):
    monkeypatch.setenv("SONARR_URL", "http://sonarr:8989")
    cfg = Config.from_env()
    assert "sonarr" not in cfg.arr_services


def test_key_without_url_skips_service(monkeypatch):
    monkeypatch.setenv("RADARR_API_KEY", "abc")
    cfg = Config.from_env()
    assert "radarr" not in cfg.arr_services


def test_url_and_key_discovers_service_and_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("SONARR_URL", "http://sonarr:8989/")
    monkeypatch.setenv("SONARR_API_KEY", "abc")
    cfg = Config.from_env()
    assert cfg.arr_services["sonarr"].url == "http://sonarr:8989"
    assert cfg.arr_services["sonarr"].api_key == "abc"


def test_credential_service_requires_all_three(monkeypatch):
    monkeypatch.setenv("QBITTORRENT_URL", "http://qbt:8080")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "admin")
    cfg = Config.from_env()
    assert "qbittorrent" not in cfg.credential_services

    monkeypatch.setenv("QBITTORRENT_PASSWORD", "pw")
    cfg = Config.from_env()
    assert cfg.credential_services["qbittorrent"].username == "admin"


def test_valid_library_cron_accepted(monkeypatch):
    monkeypatch.setenv("MEDIASTACK_LIBRARY_CRON", "02:30")
    cfg = Config.from_env()
    assert cfg.library_cron == "02:30"


@pytest.mark.parametrize("bad", ["25:00", "0230", "2:60", "abc", "02:30:00"])
def test_malformed_library_cron_falls_back_to_interval(monkeypatch, bad):
    monkeypatch.setenv("MEDIASTACK_LIBRARY_CRON", bad)
    cfg = Config.from_env()
    assert cfg.library_cron is None
    assert cfg.library_interval == 21600


def test_prowlarr_category_override(monkeypatch):
    monkeypatch.setenv("PROWLARR_CATEGORIES_MOVIE", "2010, 2040")
    cfg = Config.from_env()
    assert cfg.prowlarr_categories["movie"] == [2010, 2040]
    # Untouched types keep defaults
    assert cfg.prowlarr_categories["tv"] == [5000]
    assert cfg.prowlarr_categories["book"] == [7000, 8000]


def test_prowlarr_category_garbage_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("PROWLARR_CATEGORIES_MOVIE", "not,numbers")
    cfg = Config.from_env()
    assert cfg.prowlarr_categories["movie"] == [2000]


def test_media_roots_and_path_mappings(monkeypatch):
    monkeypatch.setenv("MEDIA_ROOTS", "/media, /mnt/extra/")
    monkeypatch.setenv(
        "MEDIA_PATH_MAPPINGS", "/media/Movies:/data/Movies,/media/Series:/data/TV",
    )
    cfg = Config.from_env()
    assert cfg.media_roots == ["/media", "/mnt/extra"]
    assert cfg.media_path_mappings == {
        "/media/Movies": "/data/Movies",
        "/media/Series": "/data/TV",
    }


def test_db_url_assembled_from_parts(monkeypatch):
    monkeypatch.setenv("DB_MEDIASTACK_USER", "ms")
    monkeypatch.setenv("DB_MEDIASTACK_PASS", "pw")
    monkeypatch.setenv("POSTGRES_HOST", "pg")
    monkeypatch.setenv("DB_MEDIASTACK_NAME", "msdb")
    cfg = Config.from_env()
    assert cfg.db_url == "postgresql://ms:pw@pg:5432/msdb"
