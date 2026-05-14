# tests/browser/admin/test_login.py
"""Browser tests for /admin/login page (M8 W7).

Tests:
  1. Login page renders with form (data-testid selectors)
  2. Invalid credentials shows inline error (no redirect)
  3. Unauthenticated access to /admin redirects to /admin/login
"""
import pytest
from playwright.sync_api import expect

pytestmark = [
    pytest.mark.browser,
    pytest.mark.postgres,
    # Every test in this file exercises the pre-auth flow; skip the autouse
    # admin-login fixture so the browser context starts with no session cookie.
    pytest.mark.unauthenticated,
]


class TestLoginPage:
    def test_login_form_renders(self, astro_server, page):
        """GET /admin/login → login form is visible with username + password inputs."""
        page.goto(f"{astro_server}/admin/login")
        page.wait_for_load_state("load")

        expect(page.get_by_test_id("login-form")).to_be_visible(timeout=5000)
        expect(page.get_by_test_id("login-username-input")).to_be_visible(timeout=5000)
        expect(page.get_by_test_id("login-password-input")).to_be_visible(timeout=5000)
        expect(page.get_by_test_id("login-submit-button")).to_be_visible(timeout=5000)

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
        expect(error_el).to_be_visible(timeout=5000)
        # Should NOT have navigated away from login
        assert "/admin/login" in page.url or "/login" in page.url

    def test_unauthenticated_admin_root_redirects_to_login(self, astro_server, page):
        """GET /admin (without auth cookie) → 302 redirect to /admin/login."""
        response = page.goto(f"{astro_server}/admin", wait_until="load")
        # After following redirects, we should be on the login page
        on_login = "/admin/login" in page.url
        if not on_login and response is not None and response.url:
            on_login = "/login" in response.url
        assert on_login
