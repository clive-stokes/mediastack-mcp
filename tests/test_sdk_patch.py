"""Guard for the _validate_accept_header monkey-patch in app/server.py.

The patch targets a private MCP SDK internal. If an SDK upgrade renames or
removes it, this must fail loudly in CI rather than silently at runtime.
"""

import mcp.server.streamable_http as shttp


def test_patch_target_still_exists_on_sdk():
    assert hasattr(shttp.StreamableHTTPServerTransport, "_validate_accept_header"), (
        "MCP SDK no longer exposes StreamableHTTPServerTransport."
        "_validate_accept_header — the Accept-header monkey-patch in "
        "app/server.py needs updating (or removing, if the SDK relaxed "
        "validation upstream)."
    )


def test_importing_server_applies_relaxed_patch():
    import app.server  # noqa: F401 — import applies the patch

    patched = shttp.StreamableHTTPServerTransport._validate_accept_header
    assert patched.__name__ == "_relaxed_validate"
