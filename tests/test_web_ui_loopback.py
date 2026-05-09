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
            resp = await client.get("/")
        assert resp.status_code != 403, "loopback request must not be rejected"

    @pytest.mark.asyncio
    async def test_non_loopback_request_rejected(self, web_app):
        """GET from external IP must get 403 (I6 — CSRF mitigation)."""
        import httpx

        transport = httpx.ASGITransport(app=web_app, client=("8.8.8.8", 1234))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/")
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
            resp = await client.get("/")
        assert resp.status_code != 403, "IPv6 loopback must not be rejected"
