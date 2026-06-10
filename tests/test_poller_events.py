"""Tier 2 — *arr history records normalise to stable MediaStack events."""

from app.clients.radarr import RadarrClient
from app.clients.sonarr import SonarrClient

def make_sonarr():
    return SonarrClient("sonarr", "http://sonarr:8989", "k")


def test_sonarr_history_parses_to_expected_events(fixture):
    records = fixture("sonarr_history.json")
    events = [make_sonarr().parse_history_event(r) for r in records]

    grabbed = events[0]
    assert grabbed["source"] == "sonarr"
    assert grabbed["event_type"] == "grabbed"
    assert grabbed["title"] == "Test Show S01E01"
    assert grabbed["timestamp"] == "2026-06-01T12:00:00Z"
    assert grabbed["source_event_id"] == "sonarr_1001"
    assert grabbed["metadata"]["quality"] == "WEBDL-1080p"
    assert grabbed["metadata"]["size_bytes"] == 1234567890
    assert grabbed["metadata"]["series_id"] == 5
    assert grabbed["metadata"]["season"] == 1
    assert grabbed["metadata"]["episode"] == 1

    imported = events[1]
    assert imported["event_type"] == "downloadfolderimported"
    assert imported["title"] == "Test Show S01E02"
    assert imported["source_event_id"] == "sonarr_1002"


def test_sonarr_external_id_sentinels_coerced_to_none(fixture):
    records = fixture("sonarr_history.json")
    events = [make_sonarr().parse_history_event(r) for r in records]

    # Record 1: tmdbId=0 and imdbId="" are Sonarr's "unknown" sentinels
    assert events[0]["metadata"]["tvdb_id"] == 123456
    assert events[0]["metadata"]["tmdb_id"] is None
    assert events[0]["metadata"]["imdb_id"] is None

    # Record 2 has real values
    assert events[1]["metadata"]["tmdb_id"] == 99887
    assert events[1]["metadata"]["imdb_id"] == "tt7654321"


def test_radarr_history_parses_to_expected_events(fixture):
    records = fixture("radarr_history.json")
    client = RadarrClient("radarr", "http://radarr:7878", "k")
    events = [client.parse_history_event(r) for r in records]

    assert events[0]["source"] == "radarr"
    assert events[0]["title"] == "Test Movie (2024)"
    assert events[0]["source_event_id"] == "radarr_2001"
    assert events[0]["metadata"]["tmdb_id"] == 654321
    assert events[0]["metadata"]["movie_id"] == 7


def test_same_fixture_polled_twice_produces_identical_ids(fixture):
    """Dedup precondition: source_event_id must be deterministic."""
    records = fixture("sonarr_history.json")
    client = make_sonarr()
    first = [client.parse_history_event(r)["source_event_id"] for r in records]
    second = [client.parse_history_event(r)["source_event_id"] for r in records]
    assert first == second
    assert len(set(first)) == len(first)
