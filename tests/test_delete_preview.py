"""Tier 2 — delete preview content and confirmation safety, against fixtures.

Exercises the real mediastack_delete_content tool with a fake poller whose
clients talk to respx-mocked services. No confirm/execute calls are made —
executing deletes belongs to the confirmation-store tests (tier 1) and the
manual NAS verification.
"""

import json

import httpx
import pytest
import respx

from app import server
from app.clients.lidarr import LidarrClient
from app.clients.radarr import RadarrClient
from app.clients.sonarr import SonarrClient


class FakePoller:
    def __init__(self, clients):
        self._clients = clients


@pytest.fixture
def poller_with(monkeypatch, clean_confirmation_store):
    """Install a fake poller exposing the given clients on app.server."""
    def install(**clients):
        monkeypatch.setattr(server, "_poller", FakePoller(clients))
        monkeypatch.setattr(server, "_poller_loop", None)
        return clean_confirmation_store
    return install


@respx.mock
def test_tv_preview_contents(poller_with, fixture):
    store = poller_with(sonarr=SonarrClient("sonarr", "http://sonarr:8989", "k"))
    respx.get("http://sonarr:8989/api/v3/series/5").mock(
        return_value=httpx.Response(200, json=fixture("sonarr_series.json")),
    )

    result = json.loads(server.mediastack_delete_content("tv", 5))
    preview = result["preview"]

    assert preview["title"] == "Test Show"
    assert preview["path"] == "/media/Series/Test Show"
    assert preview["size_on_disk"] == "5.0 GB"
    assert preview["tvdbId"] == 123456
    assert preview["delete_files"] is False
    assert len(store._pending) == 1


@respx.mock
def test_movie_preview_contents(poller_with, fixture):
    poller_with(radarr=RadarrClient("radarr", "http://radarr:7878", "k"))
    respx.get("http://radarr:7878/api/v3/movie/7").mock(
        return_value=httpx.Response(200, json=fixture("radarr_movie.json")),
    )

    result = json.loads(server.mediastack_delete_content("movie", 7))
    preview = result["preview"]

    assert preview["title"] == "Test Movie (2024)"
    assert preview["size_on_disk"] == "2.0 GB"
    assert preview["tmdbId"] == 654321


@respx.mock
def test_music_preview_contents(poller_with, fixture):
    poller_with(lidarr=LidarrClient("lidarr", "http://lidarr:8686", "k"))
    respx.get("http://lidarr:8686/api/v1/artist/9").mock(
        return_value=httpx.Response(200, json=fixture("lidarr_artist.json")),
    )

    result = json.loads(server.mediastack_delete_content("music", 9))
    preview = result["preview"]

    assert preview["title"] == "Test Artist"
    assert preview["size_on_disk"] == "1.0 GB"
    assert preview["foreignArtistId"] == "00000000-1111-2222-3333-444444444444"


@respx.mock
def test_delete_files_true_shortens_expiry_and_warns(poller_with, fixture):
    poller_with(sonarr=SonarrClient("sonarr", "http://sonarr:8989", "k"))
    respx.get("http://sonarr:8989/api/v3/series/5").mock(
        return_value=httpx.Response(200, json=fixture("sonarr_series.json")),
    )

    result = json.loads(server.mediastack_delete_content("tv", 5, delete_files=True))

    assert result["expires_in_seconds"] == 120
    assert "PERMANENTLY DELETE" in result["preview"]["warning"]
    assert "5.0 GB" in result["preview"]["warning"]
    assert "permanently deleted" in result["message"]


@respx.mock
def test_delete_files_false_keeps_standard_expiry(poller_with, fixture):
    poller_with(sonarr=SonarrClient("sonarr", "http://sonarr:8989", "k"))
    respx.get("http://sonarr:8989/api/v3/series/5").mock(
        return_value=httpx.Response(200, json=fixture("sonarr_series.json")),
    )

    result = json.loads(server.mediastack_delete_content("tv", 5, delete_files=False))

    assert result["expires_in_seconds"] == 300
    assert "will NOT be deleted" in result["preview"]["warning"]


@respx.mock
def test_service_404_returns_error_and_creates_no_confirmation(poller_with):
    store = poller_with(sonarr=SonarrClient("sonarr", "http://sonarr:8989", "k"))
    respx.get("http://sonarr:8989/api/v3/series/999").mock(
        return_value=httpx.Response(404, json={"message": "NotFound"}),
    )

    result = json.loads(server.mediastack_delete_content("tv", 999))

    assert "error" in result
    assert "confirmation_id" not in result
    assert len(store._pending) == 0


def test_invalid_media_type_rejected(poller_with):
    store = poller_with()
    result = json.loads(server.mediastack_delete_content("podcast", 1))
    assert "error" in result
    assert len(store._pending) == 0


def test_unconfigured_service_rejected(poller_with):
    store = poller_with()  # no clients at all
    result = json.loads(server.mediastack_delete_content("tv", 5))
    assert result["error"] == "sonarr is not configured"
    assert len(store._pending) == 0
