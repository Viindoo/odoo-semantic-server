# tests/browser/public/test_404.py
"""Browser tests for 404 behaviour on public paths (M8 W7).

Astro SSR (output='server') returns HTTP 404 for unknown public routes.
Admin path 404 belongs in tests/browser/admin/test_dashboard.py because
the Astro middleware calls /api/auth/verify which needs FastAPI running.
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
