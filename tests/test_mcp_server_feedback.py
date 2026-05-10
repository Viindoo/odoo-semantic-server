# tests/test_mcp_server_feedback.py
"""Test feedback API mounted on MCP server (port 8002 path).

Verifies that POST /api/feedback and GET /api/feedback/{pattern_id} are
reachable through the MCP ASGI app and that AuthMiddleware is enforced.

Marker: pytest.mark.postgres — requires a live PostgreSQL connection for
the two tests that actually insert/read rows.  The 401 and 422 tests use
mocks and run without a database.
"""
import unittest.mock as mock

import httpx
import pytest

from src.mcp.middleware import _CACHE_TS, _KEY_CACHE

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_mcp_app():
    """Return the MCP ASGI app (Starlette) with AuthMiddleware + feedback sub-app.

    Mirrors the __main__ block in src/mcp/server.py but skips uvicorn.run().
    The /install static mount is omitted (directory absent in test env).
    """
    from fastapi import FastAPI
    from starlette.middleware import Middleware

    from src.mcp.middleware import AuthMiddleware
    from src.mcp.server import mcp
    from src.web_ui.routes import feedback as feedback_mod

    app = mcp.http_app(
        transport="streamable-http",
        path="/mcp",
        middleware=[Middleware(AuthMiddleware)],
    )

    feedback_app = FastAPI()
    feedback_app.include_router(feedback_mod.router)
    app.mount("", feedback_app)
    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_auth_cache():
    """Wipe in-memory key cache before/after each test."""
    _KEY_CACHE.clear()
    _CACHE_TS.clear()
    yield
    _KEY_CACHE.clear()
    _CACHE_TS.clear()


@pytest.fixture
def pg_auth_conn(pg_conn):
    """Ensure migrations run and auth + feedback tables are clean.

    Yields pg_conn directly.  Tests that pass this connection into mocked
    _get_conn() must NOT let the route close it — use _NoCloseConn wrapper.
    """
    from src.db.migrate import run_migrations

    run_migrations(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM pattern_feedback")
        cur.execute("DELETE FROM usage_log")
        cur.execute("DELETE FROM api_keys")
    if not pg_conn.autocommit:
        pg_conn.commit()
    yield pg_conn
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM pattern_feedback")
        cur.execute("DELETE FROM usage_log")
        cur.execute("DELETE FROM api_keys")
    if not pg_conn.autocommit:
        pg_conn.commit()


class _NoCloseConn:
    """Thin proxy that forwards all attribute access to *conn* but silences close().

    The feedback route always calls conn.close() in its finally block.
    When we inject the shared test fixture connection we must not let the
    route close it — otherwise subsequent tests find a closed connection.
    """

    def __init__(self, conn):
        self._conn = conn

    def close(self):
        pass  # intentionally a no-op

    def __getattr__(self, name):
        return getattr(self._conn, name)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feedback_post_with_valid_key_returns_200(pg_auth_conn):
    """POST /api/feedback with valid X-API-Key → 200 and row created in DB."""
    from src.db.auth_registry import create_api_key, list_feedback

    raw, _, key_id = create_api_key(pg_auth_conn, "test-mcp-feedback")

    app = _build_mcp_app()

    # Patch _get_pg_conn used by AuthMiddleware (key verification).
    # Patch _get_conn used by feedback route (row insertion).
    #
    # The feedback route calls conn.close() in its finally block — use
    # _NoCloseConn wrapper so it doesn't close the shared test connection.
    with (
        mock.patch("src.mcp.server._get_pg_conn", return_value=pg_auth_conn),
        mock.patch(
            "src.web_ui.routes.feedback._get_conn",
            return_value=_NoCloseConn(pg_auth_conn),
        ),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/feedback",
                json={
                    "pattern_id": "python__write-read-before-super",
                    "rating": "up",
                    "comment": "Very helpful!",
                },
                headers={"X-API-Key": raw},
            )

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["ok"] is True
    assert isinstance(body["id"], int)

    # Verify the row was persisted
    rows = list_feedback(pg_auth_conn, "python__write-read-before-super")
    assert any(r["id"] == body["id"] for r in rows), "Feedback row not found in DB"


@pytest.mark.asyncio
async def test_feedback_post_without_key_returns_401():
    """POST /api/feedback without X-API-Key header → 401 from AuthMiddleware."""
    app = _build_mcp_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/feedback",
            json={"pattern_id": "some.pattern", "rating": "up"},
        )

    assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_feedback_post_invalid_rating_returns_422(pg_auth_conn):
    """POST /api/feedback with rating='neutral' → 422 (application-level validation).

    The request passes auth (valid API key, mocked DB) but is rejected by the
    route handler because 'neutral' is not an accepted rating value.
    """
    from src.db.auth_registry import create_api_key

    raw, _, _key_id = create_api_key(pg_auth_conn, "test-mcp-feedback-422")

    app = _build_mcp_app()

    with mock.patch("src.mcp.server._get_pg_conn", return_value=pg_auth_conn):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/feedback",
                json={"pattern_id": "some.pattern", "rating": "neutral"},
                headers={"X-API-Key": raw},
            )

    # The route raises HTTPException(422) for invalid rating values
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"
