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


@respx.mock
def test_seerr_client_persistent_and_sends_api_key():
    from app.clients.seerr import SeerrClient

    route = respx.get("http://seerr:5055/api/v1/status").mock(
        return_value=httpx.Response(200, json={"version": "2.0"}),
    )
    client = SeerrClient("http://seerr:5055", "seerr-key")

    async def two_requests():
        first = client._client
        await client.get("/api/v1/status")
        await client.get("/api/v1/status")
        assert client._client is first

    asyncio.run(two_requests())
    assert route.calls.last.request.headers["X-Api-Key"] == "seerr-key"
    asyncio.run(client.close())


@respx.mock
def test_qbittorrent_reauths_on_403():
    from app.clients.qbittorrent import QBittorrentClient

    login = respx.post("http://qbit:8080/api/v2/auth/login").mock(
        return_value=httpx.Response(
            200, headers={"set-cookie": "SID=abc123; path=/"},
        ),
    )
    info = respx.get("http://qbit:8080/api/v2/torrents/info").mock(
        side_effect=[
            httpx.Response(403),          # stale session
            httpx.Response(200, json=[]), # after re-auth
        ],
    )
    client = QBittorrentClient("http://qbit:8080", "u", "p")
    client._authed = True  # pretend an old session existed

    result = asyncio.run(client.get_torrents())
    assert result == []
    assert login.called            # re-auth happened
    assert info.call_count == 2
    asyncio.run(client.close())


@respx.mock
def test_dispatcharr_refreshes_token_on_401():
    from app.clients.dispatcharr import DispatcharrClient

    token = respx.post("http://disp:9191/api/accounts/token/").mock(
        return_value=httpx.Response(200, json={"access": "new-jwt"}),
    )
    src = respx.get("http://disp:9191/api/epg/sources/").mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, json=[]),
        ],
    )
    client = DispatcharrClient("http://disp:9191", "u", "p")
    client._token = "stale-jwt"

    result = asyncio.run(client.get_epg_sources())
    assert result == []
    assert token.called
    assert src.calls.last.request.headers["Authorization"] == "Bearer new-jwt"
    asyncio.run(client.close())
