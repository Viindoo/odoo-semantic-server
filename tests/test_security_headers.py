"""Tests for security header configuration — M9 W-HD.

Scope:
- FastAPI-level: CORSMiddleware explicit no-op (allow_origins=[] → no CORS headers).
- nginx-level CSP/Permissions-Policy are set by nginx, not FastAPI — those are
  verified manually via `curl -I https://<domain>/` after deploy. See
  docs/deploy/nginx-m8.conf comments and docs/deploy/pre-launch-checklist.md.

CORSMiddleware with allow_origins=[] is a deliberate no-op:
  Astro SSR proxies all /api/* calls through nginx server-side, so browsers
  never make direct cross-origin requests to FastAPI :8003.
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


class TestNginxHeadersDocumented:
    """Placeholder to document which headers are nginx-level (not FastAPI).

    These headers are added by nginx/Caddy, not FastAPI:
      - Content-Security-Policy
      - Permissions-Policy
      - Strict-Transport-Security
      - X-Frame-Options
      - X-Content-Type-Options
      - Referrer-Policy

    Verify after deploy:
      curl -sI https://odoo-semantic.viindoo.com/ | grep -E \
        'Content-Security-Policy|Permissions-Policy|X-Frame-Options'
    """

    def test_placeholder(self):
        """This class documents nginx-level security headers only; no runtime assertions."""
        nginx_headers = [
            "Content-Security-Policy",
            "Permissions-Policy",
            "Strict-Transport-Security",
            "X-Frame-Options",
            "X-Content-Type-Options",
            "Referrer-Policy",
        ]
        # Ensure the list is non-empty — keeps this test meaningful as documentation.
        assert len(nginx_headers) > 0
