# tests/browser/admin/test_login.py
"""Browser tests for /admin/login page (M8 W7).

Tests:
  1. Login page renders with form (data-testid selectors)
  2. Invalid credentials shows inline error (no redirect)
  3. Unauthenticated access to /admin redirects to /admin/login
"""
import pytest

pytestmark = [pytest.mark.browser, pytest.mark.postgres]


class TestLoginPage:
    def test_login_form_renders(self, astro_server, page):
        """GET /admin/login → login form is visible with username + password inputs."""
        page.goto(f"{astro_server}/admin/login")
        page.wait_for_load_state("load")

        assert page.get_by_test_id("login-form").is_visible()
        assert page.get_by_test_id("login-username-input").is_visible()
        assert page.get_by_test_id("login-password-input").is_visible()
        assert page.get_by_test_id("login-submit-button").is_visible()

    def test_invalid_credentials_shows_error(self, astro_server, page):
        """Submit wrong credentials → inline error visible, no redirect to /admin."""
        page.goto(f"{astro_server}/admin/login")
        page.wait_for_load_state("load")

        page.get_by_test_id("login-username-input").fill("wrong-user")
        page.get_by_test_id("login-password-input").fill("wrong-pass")
        page.get_by_test_id("login-submit-button").click()

        # Wait briefly for async fetch to complete
        page.wait_for_timeout(800)

        # Error element should become visible (hidden attr removed)
        error_el = page.get_by_test_id("login-error")
        assert error_el.is_visible()
        # Should NOT have navigated away from login
        assert "/admin/login" in page.url or "/login" in page.url

    def test_unauthenticated_admin_root_redirects_to_login(self, astro_server, page):
        """GET /admin (without auth cookie) → 302 redirect to /admin/login."""
        response = page.goto(f"{astro_server}/admin", wait_until="load")
        # After following redirects, we should be on the login page
        assert "/admin/login" in page.url or (response is not None and response.url and "/login" in response.url)
