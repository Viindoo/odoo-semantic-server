# SPDX-License-Identifier: AGPL-3.0-or-later
"""AuthMiddleware returns 503 JSON when the PG pool is unavailable.

Pre-incident behaviour: any psycopg2.OperationalError during auth_store()
propagated as HTTP 500, which is indistinguishable from a 5xx in user
code. Post-fix: middleware catches RuntimeError (pool not initialised)
and psycopg2.OperationalError (transient outage) and returns a 503 with
a machine-readable body so MCP clients (and reverse proxies) can retry.
"""
import json

import psycopg2
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient


def _build_app(verify_side_effect):
    """Build a tiny Starlette app guarded by AuthMiddleware. `_do_verify` is
    overridden via monkeypatch in the tests below; the route just returns 200."""
    from src.mcp.middleware import AuthMiddleware

    async def _ok(request):
        return JSONResponse({"ok": True})  # noqa  - test stub (lint-json-response bypass: no datetime)

    app = Starlette(
        routes=[Route("/echo", _ok)],
        middleware=[Middleware(AuthMiddleware)],
    )
    return app


@pytest.fixture(autouse=True)
def _disable_rate_limit(monkeypatch):
    """The rate-limiter depends on config.get('auth', 'rate_limit_rpm') —
    just stub it to a large number so the path under test is auth-only."""
    from src.mcp import middleware as mw

    # Clear request cache between tests so cache-hits don't mask the verify path.
    mw._KEY_CACHE.clear()
    mw._CACHE_TS.clear()
    yield


def test_returns_503_when_pool_not_initialized(monkeypatch):
    """PoolNotInitializedError from get_pool() must surface as a 503 JSON."""
    from src.constants import PG_BG_RETRY_INTERVAL_SECONDS
    from src.db.exceptions import PoolNotInitializedError
    from src.mcp import middleware as mw

    def _raise_typed(*a, **kw):
        raise PoolNotInitializedError("PostgreSQL pool is not initialized.")

    # Patch the auth_store import inside the closure — easier to do at module level.
    import src.db.pg as pg_mod
    monkeypatch.setattr(pg_mod, "auth_store", _raise_typed)

    app = _build_app(_raise_typed)
    client = TestClient(app)

    resp = client.get("/echo", headers={"X-API-Key": "raw-test-key"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["pg"] == "unavailable"
    # Body MUST NOT echo the exception payload (CWE-209 defence-in-depth):
    # `psycopg2.OperationalError.__str__` from libpq includes internal
    # hostnames + DB usernames — keep that server-side in the log only.
    assert "reason" not in body
    # Retry-After is SSOT'd to PG_BG_RETRY_INTERVAL_SECONDS — guards against
    # someone reintroducing the hardcoded "30".
    assert resp.headers.get("Retry-After") == str(PG_BG_RETRY_INTERVAL_SECONDS)
    _ = mw  # silence unused


def test_unrelated_runtime_error_propagates_not_swallowed(monkeypatch):
    """Issue #3 regression guard: a generic RuntimeError must NOT be coerced to
    503. Only PoolNotInitializedError (and psycopg2.OperationalError) qualify
    for the degraded-mode path. Anything else should surface as 500 so ops
    sees the real root cause instead of a silent mask."""

    def _raise_generic(*a, **kw):
        raise RuntimeError("config sentinel — unrelated to pool")

    import src.db.pg as pg_mod
    monkeypatch.setattr(pg_mod, "auth_store", _raise_generic)

    app = _build_app(_raise_generic)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/echo", headers={"X-API-Key": "raw-test-key"})
    # Starlette default for an uncaught exception is 500.
    assert resp.status_code == 500
    # Must NOT be the degraded body — that would be the bug Issue #3 fixed.
    body_text = resp.text
    assert "degraded" not in body_text.lower()


def test_returns_503_when_psycopg_operational_error(monkeypatch, caplog):
    """psycopg2.OperationalError from verify_api_key must also surface as 503.

    Body is sanitised (no `reason` field) — the original exception text
    appears in the server log only. This is the defense-in-depth guard
    for CWE-209: libpq error strings can include internal hostnames /
    DB usernames that have no business being on an unauthenticated wire.
    """

    class _FakeStore:
        def verify_api_key(self, raw):
            raise psycopg2.OperationalError(
                "could not translate host name 'pg-internal.viindoo.local' to address",
            )

    import src.db.pg as pg_mod
    monkeypatch.setattr(pg_mod, "auth_store", lambda: _FakeStore())

    app = _build_app(None)
    client = TestClient(app)

    with caplog.at_level("WARNING", logger="src.mcp.middleware"):
        resp = client.get("/echo", headers={"X-API-Key": "raw-test-key"})

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["pg"] == "unavailable"
    # CWE-209 guard: internal hostname must NOT appear in the response body.
    assert "pg-internal.viindoo.local" not in resp.text
    assert "reason" not in body
    # But the server-side log MUST capture the cause so ops can debug.
    assert any(
        "pg-internal.viindoo.local" in r.message
        for r in caplog.records
    ), "exception detail must be logged server-side for diagnostics"


def test_missing_header_still_returns_401(monkeypatch):
    """Sanity guard — the degraded-mode handling must not steal the 401 path."""
    import src.db.pg as pg_mod
    # auth_store should never be called in this path.
    monkeypatch.setattr(pg_mod, "auth_store", lambda: (_ for _ in ()).throw(
        AssertionError("auth_store called despite missing header"),
    ))

    app = _build_app(None)
    client = TestClient(app)
    resp = client.get("/echo")  # no X-API-Key
    assert resp.status_code == 401


def test_public_paths_bypass_db_check(monkeypatch):
    """`/health` is in _PUBLIC_PATHS — it must NOT trigger an auth_store call."""

    def _explode(*a, **kw):
        raise AssertionError("auth_store called on public path")

    import src.db.pg as pg_mod
    monkeypatch.setattr(pg_mod, "auth_store", _explode)

    from src.mcp.middleware import AuthMiddleware

    async def _health(request):
        return JSONResponse({"status": "ok"})  # noqa  - test stub (lint-json-response bypass: no datetime)

    app = Starlette(
        routes=[Route("/health", _health)],
        middleware=[Middleware(AuthMiddleware)],
    )
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    # Belt-and-braces: response is plain JSON, not the 503 degraded body.
    assert "pg" not in body


def test_503_body_keeps_no_exception_payload_even_for_long_errors(monkeypatch):
    """A pathological psycopg error message must NOT leak to the wire at all.

    Previously the body carried `reason: str(e)[:300]` — which still leaked
    internal hostnames / DB usernames within the 300-char window. Now the
    body is fully static; only the server log holds the exception text.
    Confirms the CWE-209 fix even for very long error payloads.
    """

    long_message = "FATAL: password authentication failed for user 'osm_app' " + ("x" * 5000)

    class _ExplodingStore:
        def verify_api_key(self, raw):
            raise psycopg2.OperationalError(long_message)

    import src.db.pg as pg_mod
    monkeypatch.setattr(pg_mod, "auth_store", lambda: _ExplodingStore())

    app = _build_app(None)
    client = TestClient(app)
    resp = client.get("/echo", headers={"X-API-Key": "k"})
    assert resp.status_code == 503
    payload = json.loads(resp.content)
    # No exception payload field — the leak is fixed.
    assert "reason" not in payload
    # Sanity: no leakage of either the long padding or the user-identifying
    # phrase. Body should be ≤ ~100 bytes regardless of exception size.
    assert "xxxxx" not in resp.text
    assert "osm_app" not in resp.text
    assert len(resp.content) < 200
