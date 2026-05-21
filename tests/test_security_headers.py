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

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    """TestClient for the FastAPI app with minimal env config."""
    # Patch session secret so app.create_app() doesn't require full DB config.
    monkeypatch.setenv("WEBUI_SESSION_SECRET", "test-secret-for-security-header-tests")
    # Disable secure cookie so TestClient (plain HTTP) doesn't reject cookies.
    monkeypatch.setenv("WEBUI_SECURE_COOKIE", "0")

    from src.web_ui.app import create_app

    app = create_app()
    # TestClient uses loopback by default — satisfies _LoopbackOnlyMiddleware.
    return TestClient(app, raise_server_exceptions=False)


class TestCORSMiddlewareNoOp:
    """CORSMiddleware is present but configured as explicit no-op.

    When allow_origins=[], FastAPI's CORSMiddleware does NOT add any
    Access-Control-Allow-Origin header — not even for same-origin requests.
    This confirms the "no cross-origin access" intent is enforced.
    """

    def test_no_cors_allow_origin_on_simple_request(self, client):
        """Simple GET should not expose Access-Control-Allow-Origin."""
        resp = client.get("/api/auth/login", headers={"Origin": "https://evil.example.com"})
        assert "access-control-allow-origin" not in resp.headers

    def test_no_cors_allow_origin_on_preflight(self, client):
        """OPTIONS preflight from foreign origin should get no ACAO header."""
        resp = client.options(
            "/api/auth/login",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "access-control-allow-origin" not in resp.headers

    def test_no_cors_allow_credentials(self, client):
        """No Access-Control-Allow-Credentials should be set."""
        resp = client.get("/api/auth/login", headers={"Origin": "https://evil.example.com"})
        assert "access-control-allow-credentials" not in resp.headers


class TestSecurityHeadersFastAPI:
    """_SecurityHeadersMiddleware injects CSP + Permissions-Policy on every response.

    FastAPI is a JSON-only API (ADR-0015) — strictest CSP (default-src 'none')
    is appropriate because JSON responses never load resources.
    """

    def test_csp_header_present(self, client):
        """Content-Security-Policy must be present on every FastAPI response."""
        resp = client.get("/api/auth/login")
        assert "content-security-policy" in resp.headers

    def test_csp_default_src_none(self, client):
        """JSON API CSP must use default-src 'none' (strictest — no resource loading)."""
        resp = client.get("/api/auth/login")
        csp = resp.headers.get("content-security-policy", "")
        assert "default-src 'none'" in csp

    def test_csp_frame_ancestors_none(self, client):
        """frame-ancestors 'none' prevents clickjacking at CSP level."""
        resp = client.get("/api/auth/login")
        csp = resp.headers.get("content-security-policy", "")
        assert "frame-ancestors 'none'" in csp

    def test_permissions_policy_present(self, client):
        """Permissions-Policy must be present on every FastAPI response."""
        resp = client.get("/api/auth/login")
        assert "permissions-policy" in resp.headers

    def test_permissions_policy_camera_disabled(self, client):
        """camera=() must be present in Permissions-Policy."""
        resp = client.get("/api/auth/login")
        pp = resp.headers.get("permissions-policy", "")
        assert "camera=()" in pp

    def test_permissions_policy_microphone_disabled(self, client):
        """microphone=() must be present in Permissions-Policy."""
        resp = client.get("/api/auth/login")
        pp = resp.headers.get("permissions-policy", "")
        assert "microphone=()" in pp

    def test_permissions_policy_geolocation_disabled(self, client):
        """geolocation=() must be present in Permissions-Policy."""
        resp = client.get("/api/auth/login")
        pp = resp.headers.get("permissions-policy", "")
        assert "geolocation=()" in pp

    def test_security_headers_on_auth_endpoint(self, client):
        """Security headers apply to auth endpoints too (no exempt paths)."""
        resp = client.post("/api/auth/login", json={"username": "x", "password": "y"})
        assert "content-security-policy" in resp.headers
        assert "permissions-policy" in resp.headers


class TestNginxHeadersDocumented:
    """Placeholder to document which headers remain nginx-level (not FastAPI).

    These headers are still added by nginx/Caddy (requires sudo — out of scope):
      - Strict-Transport-Security
      - X-Frame-Options  (covered also by CSP frame-ancestors above)
      - X-Content-Type-Options
      - Referrer-Policy

    Verify after deploy:
      curl -sI https://odoo-semantic.viindoo.com/ | grep -E \
        'Strict-Transport-Security|X-Frame-Options|X-Content-Type-Options'
    """

    def test_placeholder(self):
        """Documents nginx-level headers; no runtime assertion needed here."""
        nginx_only_headers = [
            "Strict-Transport-Security",
            "X-Frame-Options",
            "X-Content-Type-Options",
            "Referrer-Policy",
        ]
        assert len(nginx_only_headers) > 0
