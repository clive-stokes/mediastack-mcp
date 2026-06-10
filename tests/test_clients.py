"""Tier 2 — HTTP client behaviour against mocked transports (respx)."""

import asyncio

import httpx
import respx

from app.clients.base import ArrClient, JellyfinClient


@respx.mock
def test_arr_client_sends_x_api_key_header():
    route = respx.get("http://sonarr:8989/api/v3/system/status").mock(
        return_value=httpx.Response(200, json={"version": "4.0"}),
    )
    client = ArrClient("sonarr", "http://sonarr:8989", "arr-key")
    result = asyncio.run(client.get("/api/v3/system/status"))

    assert result == {"version": "4.0"}
    assert route.calls.last.request.headers["X-Api-Key"] == "arr-key"


@respx.mock
def test_jellyfin_client_sends_mediabrowser_token_header():
    route = respx.get("http://jellyfin:8096/System/Info").mock(
        return_value=httpx.Response(200, json={"Version": "10.9"}),
    )
    client = JellyfinClient("http://jellyfin:8096", "jf-key")
    asyncio.run(client.get("/System/Info"))

    auth = route.calls.last.request.headers["Authorization"]
    assert auth == 'MediaBrowser Token="jf-key"'


@respx.mock
def test_arr_delete_handles_204_empty_body():
    respx.delete("http://sonarr:8989/api/v3/series/5").mock(
        return_value=httpx.Response(204),
    )
    client = ArrClient("sonarr", "http://sonarr:8989", "k")
    result = asyncio.run(client.delete("/api/v3/series/5"))
    assert result == {"status": "deleted"}


@respx.mock
def test_jellyfin_delete_handles_204_empty_body():
    respx.delete("http://jellyfin:8096/Items/abc").mock(
        return_value=httpx.Response(204),
    )
    client = JellyfinClient("http://jellyfin:8096", "k")
    result = asyncio.run(client.delete("/Items/abc"))
    assert result == {"status": "ok"}


@respx.mock
def test_ping_returns_false_on_connect_error():
    respx.get("http://sonarr:8989/api/v3/system/status").mock(
        side_effect=httpx.ConnectError("refused"),
    )
    client = ArrClient("sonarr", "http://sonarr:8989", "k")
    assert asyncio.run(client.ping()) is False


@respx.mock
def test_ping_returns_false_on_http_error():
    respx.get("http://jellyfin:8096/System/Info").mock(
        return_value=httpx.Response(500),
    )
    client = JellyfinClient("http://jellyfin:8096", "k")
    assert asyncio.run(client.ping()) is False


@respx.mock
def test_client_reuses_one_underlying_httpx_client():
    respx.get("http://sonarr:8989/api/v3/health").mock(
        return_value=httpx.Response(200, json=[]),
    )
    client = ArrClient("sonarr", "http://sonarr:8989", "k")

    async def two_requests():
        first = client._client
        await client.get("/api/v3/health")
        await client.get("/api/v3/health")
        assert client._client is first

    asyncio.run(two_requests())
    asyncio.run(client.close())
