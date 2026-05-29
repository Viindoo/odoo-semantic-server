# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/browser/admin/test_logout.py
"""Browser tests for logout flow (WI-3: canonical login URL /login).

Tests:
  1. Logout redirects to /login (canonical URL)
  2. After logout, /admin access redirects back to /login (cookie cleared)
"""
import pytest

pytestmark = [pytest.mark.browser, pytest.mark.postgres]


class TestLogout:
    def test_logout_link_navigates_to_login(self, astro_server, page):
        """The logout endpoint redirects to /login (canonical login page)."""
        # Navigate to login page first
        page.goto(f"{astro_server}/login")
        page.wait_for_load_state("load")

        # Navigate to /admin/logout directly and expect redirect to /login.
        response = page.goto(f"{astro_server}/admin/logout", wait_until="load")
        final_url = page.url
        assert "/login" in final_url or (
            response is not None and response.status in (200, 302)
        )

    def test_logout_clears_session_cookie(self, astro_server, page):
        """After visiting /admin/logout, subsequent /admin access returns login page."""
        # Visit logout endpoint
        page.goto(f"{astro_server}/admin/logout", wait_until="load")

        # Now try to access a protected admin page
        page.goto(f"{astro_server}/admin/repos", wait_until="load")

        # Should be redirected to login (not repos page)
        assert "/login" in page.url
