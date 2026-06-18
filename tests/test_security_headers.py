# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for security header configuration — M9 W-HD / CSP hardening.

Scope:
- FastAPI-level: CORSMiddleware explicit no-op (allow_origins=[] → no CORS headers).
- FastAPI-level: _SecurityHeadersMiddleware emits CSP + Permissions-Policy on every
  response (M9 CSP hardening — replaces previous nginx-only placeholder).

Note: HSTS, X-Frame-Options, X-Content-Type-Options, Referrer-Policy are still added
by nginx/Caddy (requires sudo — out of session scope). Verify after deploy:
  curl -sI https://odoo-semantic.viindoo.com/ | grep -E \
    'Content-Security-Policy|Permissions-Policy|X-Frame-Options'
"""

import httpx
import pytest

pytestmark = pytest.mark.http


@pytest.fixture()
async def client(monkeypatch):
    """In-process httpx client driving the FastAPI app via ASGITransport.

    Uses httpx.AsyncClient + ASGITransport instead of fastapi/starlette TestClient:
    the latter emits a StarletteDeprecationWarning ("install httpx2") since
    starlette 1.3 (#319). ASGITransport is async-only, so the tests are async.
    """
    # Patch session secret so app.create_app() doesn't require full DB config.
    monkeypatch.setenv("WEBUI_SESSION_SECRET", "test-secret-for-security-header-tests")
    # Disable secure cookie so the plain-HTTP client doesn't reject cookies.
    monkeypatch.setenv("WEBUI_SECURE_COOKIE", "0")

    from src.web_ui.app import create_app

    app = create_app()
    # ASGITransport defaults the client address to 127.0.0.1 — satisfies _LoopbackOnlyMiddleware.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        yield client


class TestCORSMiddlewareNoOp:
    """CORSMiddleware is present but configured as explicit no-op.

    When allow_origins=[], FastAPI's CORSMiddleware does NOT add any
    Access-Control-Allow-Origin header — not even for same-origin requests.
    This confirms the "no cross-origin access" intent is enforced.
    """

    async def test_no_cors_allow_origin_on_simple_request(self, client):
        """Simple GET should not expose Access-Control-Allow-Origin."""
        resp = await client.get("/api/auth/login", headers={"Origin": "https://evil.example.com"})
        assert "access-control-allow-origin" not in resp.headers

    async def test_no_cors_allow_origin_on_preflight(self, client):
        """OPTIONS preflight from foreign origin should get no ACAO header."""
        resp = await client.options(
            "/api/auth/login",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "access-control-allow-origin" not in resp.headers

    async def test_no_cors_allow_credentials(self, client):
        """No Access-Control-Allow-Credentials should be set."""
        resp = await client.get("/api/auth/login", headers={"Origin": "https://evil.example.com"})
        assert "access-control-allow-credentials" not in resp.headers


class TestSecurityHeadersFastAPI:
    """_SecurityHeadersMiddleware injects CSP + Permissions-Policy on every response.

    FastAPI is a JSON-only API (ADR-0015) — strictest CSP (default-src 'none')
    is appropriate because JSON responses never load resources.
    """

    async def test_csp_header_present(self, client):
        """Content-Security-Policy must be present on every FastAPI response."""
        resp = await client.get("/api/auth/login")
        assert "content-security-policy" in resp.headers

    async def test_csp_default_src_none(self, client):
        """JSON API CSP must use default-src 'none' (strictest — no resource loading)."""
        resp = await client.get("/api/auth/login")
        csp = resp.headers.get("content-security-policy", "")
        assert "default-src 'none'" in csp

    async def test_csp_frame_ancestors_none(self, client):
        """frame-ancestors 'none' prevents clickjacking at CSP level."""
        resp = await client.get("/api/auth/login")
        csp = resp.headers.get("content-security-policy", "")
        assert "frame-ancestors 'none'" in csp

    async def test_permissions_policy_present(self, client):
        """Permissions-Policy must be present on every FastAPI response."""
        resp = await client.get("/api/auth/login")
        assert "permissions-policy" in resp.headers

    async def test_permissions_policy_camera_disabled(self, client):
        """camera=() must be present in Permissions-Policy."""
        resp = await client.get("/api/auth/login")
        pp = resp.headers.get("permissions-policy", "")
        assert "camera=()" in pp

    async def test_permissions_policy_microphone_disabled(self, client):
        """microphone=() must be present in Permissions-Policy."""
        resp = await client.get("/api/auth/login")
        pp = resp.headers.get("permissions-policy", "")
        assert "microphone=()" in pp

    async def test_permissions_policy_geolocation_disabled(self, client):
        """geolocation=() must be present in Permissions-Policy."""
        resp = await client.get("/api/auth/login")
        pp = resp.headers.get("permissions-policy", "")
        assert "geolocation=()" in pp

    async def test_security_headers_on_auth_endpoint(self, client):
        """Security headers apply to auth endpoints too (no exempt paths)."""
        resp = await client.post("/api/auth/login", json={"username": "x", "password": "y"})
        assert "content-security-policy" in resp.headers
        assert "permissions-policy" in resp.headers
