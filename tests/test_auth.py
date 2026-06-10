"""Tier 1 — bearer-token middleware behaviour."""

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.auth import BearerAuthMiddleware

TOKEN = "test-token-0123456789abcdef"


async def ok(request):
    return JSONResponse({"status": "ok"})


def build_app(token: str | None):
    app = Starlette(routes=[
        Route("/mcp", ok, methods=["GET", "POST"]),
        Route("/mcp/sub", ok, methods=["GET"]),
        Route("/ingest", ok, methods=["POST"]),
        Route("/health", ok, methods=["GET"]),
    ])
    if token:
        app.add_middleware(BearerAuthMiddleware, token=token)
    return TestClient(app)


def test_token_set_rejects_missing_header():
    client = build_app(TOKEN)
    assert client.post("/mcp").status_code == 401
    assert client.post("/ingest", json={}).status_code == 401


def test_token_set_rejects_wrong_token():
    client = build_app(TOKEN)
    resp = client.post("/mcp", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_token_set_rejects_non_bearer_scheme():
    client = build_app(TOKEN)
    resp = client.post("/mcp", headers={"Authorization": f"Basic {TOKEN}"})
    assert resp.status_code == 401


def test_token_set_accepts_correct_token():
    client = build_app(TOKEN)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    assert client.post("/mcp", headers=headers).status_code == 200
    assert client.post("/ingest", json={}, headers=headers).status_code == 200


def test_mcp_subpaths_protected():
    client = build_app(TOKEN)
    assert client.get("/mcp/sub").status_code == 401
    headers = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get("/mcp/sub", headers=headers).status_code == 200


def test_health_open_either_way():
    assert build_app(TOKEN).get("/health").status_code == 200
    assert build_app(None).get("/health").status_code == 200


def test_token_unset_everything_open():
    client = build_app(None)
    assert client.post("/mcp").status_code == 200
    assert client.post("/ingest", json={}).status_code == 200
