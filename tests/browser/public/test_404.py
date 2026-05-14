# tests/browser/public/test_404.py
"""Browser tests for 404 behaviour (M8 W7).

Astro SSR (output='server') returns HTTP 404 for unknown routes.
Tests verify that unknown paths result in a 404 response and that
the response is not a crash (5xx).
"""
import pytest

pytestmark = pytest.mark.browser


class Test404:
    def test_unknown_path_returns_404(self, astro_server, page):
        """GET /this-page-does-not-exist → response status 404."""
        response = page.goto(
            f"{astro_server}/this-page-does-not-exist-m8-w7-test",
            wait_until="load",
        )
        assert response is not None
        assert response.status == 404

    def test_unknown_admin_subpath_redirects_or_404(self, astro_server, page):
        """GET /admin/nonexistent → 404 or redirect to /admin/login (auth guard)."""
        response = page.goto(
            f"{astro_server}/admin/nonexistent-page-m8-w7",
            wait_until="load",
        )
        # Either 404 (page not found) or auth redirect to /admin/login
        final_url = page.url
        assert response is not None
        assert response.status in (200, 302, 404) and (
            response.status == 404
            or "/admin/login" in final_url
            or "/login" in final_url
        )
