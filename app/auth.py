"""Optional bearer-token authentication for the MCP and ingest endpoints.

When MEDIASTACK_AUTH_TOKEN is set, requests to /mcp and /ingest must carry
"Authorization: Bearer <token>" or they are rejected with 401. /health stays
open — the Docker healthcheck depends on it and it exposes nothing sensitive
beyond event counts.

When the variable is unset, no middleware is installed and behaviour is
unchanged (back-compat for existing deployments).
"""

import secrets

from starlette.requests import Request
from starlette.responses import JSONResponse

# Path prefixes that require a token. /mcp is the streamable-http MCP mount
# (the SDK serves /mcp and sub-paths); /ingest is the external event receiver.
PROTECTED_PREFIXES = ("/mcp", "/ingest")


class BearerAuthMiddleware:
    """Pure ASGI middleware — rejects unauthenticated requests to protected paths."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self._is_protected(scope["path"]):
            request = Request(scope)
            auth = request.headers.get("authorization", "")
            scheme, _, credentials = auth.partition(" ")
            # compare_digest avoids leaking the token via timing differences
            if scheme.lower() != "bearer" or not secrets.compare_digest(
                credentials.strip(), self.token
            ):
                response = JSONResponse(
                    {"status": "error", "detail": "unauthorised"}, status_code=401
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

    @staticmethod
    def _is_protected(path: str) -> bool:
        return any(
            path == prefix or path.startswith(prefix + "/")
            for prefix in PROTECTED_PREFIXES
        )
