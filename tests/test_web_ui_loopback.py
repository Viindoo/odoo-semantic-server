# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for Web UI loopback-only middleware (I6)."""
import pytest


@pytest.fixture
def web_app():
    from src.web_ui.app import create_app

    return create_app()


class TestLoopbackMiddleware:
    """I6: Web UI must reject requests from non-loopback IP addresses."""

    @pytest.mark.asyncio
    async def test_loopback_request_allowed(self, web_app):
        """GET from 127.0.0.1 must pass through middleware."""
        import httpx

        # Default ASGITransport sets client=("127.0.0.1", 123)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web_app), base_url="http://test"
        ) as client:
            resp = await client.get("/api/auth/verify")
        assert resp.status_code != 403, "loopback request must not be rejected"

    @pytest.mark.asyncio
    async def test_non_loopback_request_rejected(self, web_app):
        """GET from external IP must get 403 (I6 — CSRF mitigation)."""
        import httpx

        transport = httpx.ASGITransport(app=web_app, client=("8.8.8.8", 1234))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/api/auth/verify")
        assert resp.status_code == 403
        assert resp.json()["error"] == "forbidden"

    @pytest.mark.asyncio
    async def test_ipv6_loopback_allowed(self, web_app):
        """GET from ::1 (IPv6 loopback) must pass through."""
        import httpx

        transport = httpx.ASGITransport(app=web_app, client=("::1", 1234))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/api/auth/verify")
        assert resp.status_code != 403, "IPv6 loopback must not be rejected"

    @pytest.mark.asyncio
    async def test_x_forwarded_for_under_proxy_headers_trips_loopback(self, web_app):
        """Document WHY src/web_ui/__main__.py must pass proxy_headers=False.

        With uvicorn's ProxyHeadersMiddleware active (the default), an
        X-Forwarded-For header from a trusted TCP peer rewrites
        scope["client"] to the forwarded IP. LoopbackOnly then sees the
        external IP and returns 403, breaking every external /api/* request
        when nginx is in front. The companion regression
        test_main_passes_proxy_headers_false locks in the fix.
        """
        import httpx
        from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

        wrapped = ProxyHeadersMiddleware(web_app, trusted_hosts="*")
        transport = httpx.ASGITransport(app=wrapped)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get(
                "/api/auth/verify",
                headers={"X-Forwarded-For": "8.8.8.8"},
            )
        assert resp.status_code == 403
        assert resp.json()["error"] == "forbidden"


def test_main_passes_proxy_headers_false(monkeypatch):
    """Regression: src/web_ui/__main__.py must call uvicorn.run with
    proxy_headers=False — see ADR-0015 §6 post-merge correction.

    Without this, every external /api/* request is rejected by
    LoopbackOnly with 403 because uvicorn rewrites scope["client"] to
    the X-Forwarded-For value forwarded by nginx.
    """
    from unittest.mock import patch

    monkeypatch.setenv("FERNET_KEY", "dGVzdF9rZXlfMzJfYnl0ZXNfYmFzZTY0X2VuY29kZWQ=")

    with patch("uvicorn.run") as mock_run:
        from src.web_ui import __main__ as main_mod

        main_mod.main()

    assert mock_run.called, "uvicorn.run must be invoked"
    kwargs = mock_run.call_args.kwargs
    assert kwargs.get("proxy_headers") is False, (
        "proxy_headers must be False so X-Forwarded-For from nginx does not "
        "rewrite scope.client and trip LoopbackOnlyMiddleware. See ADR-0015 §6."
    )
