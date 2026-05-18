# tests/browser/admin/test_account_anon_redirect.py
"""Browser regression test: anonymous users hitting /account/* must redirect
to /admin/login (single global login flow).

Bug fixed: WI5 shipped /account/api-keys without an auth gate in the Astro
middleware catch-all branch. Anon hit the page, saw an empty self-service UI,
clicked Generate Key, and got a confusing 401 from the backend. Backend was
correctly returning 401, but the page render path leaked the layout chrome.

Regression covers:
  - /account              → 302 to /admin/login
  - /account/api-keys     → 302 to /admin/login
"""
import pytest
from playwright.sync_api import expect

pytestmark = [
    pytest.mark.browser,
    pytest.mark.postgres,
    # Pre-auth flow; skip the autouse admin-login fixture so the browser
    # context starts with no session cookie.
    pytest.mark.unauthenticated,
]


class TestAccountAnonRedirect:
    def test_anon_account_root_redirects_to_login(self, astro_server, page):
        """GET /account (no auth cookie) → ends on /admin/login."""
        page.goto(f"{astro_server}/account", wait_until="load")
        assert "/admin/login" in page.url, (
            f"Expected /admin/login in final URL, got {page.url!r}"
        )
        # Sanity: the login form is actually visible (not just URL match).
        expect(page.get_by_test_id("login-form")).to_be_visible(timeout=5000)

    def test_anon_account_api_keys_redirects_to_login(self, astro_server, page):
        """GET /account/api-keys (no auth cookie) → ends on /admin/login."""
        page.goto(f"{astro_server}/account/api-keys", wait_until="load")
        assert "/admin/login" in page.url, (
            f"Expected /admin/login in final URL, got {page.url!r}"
        )
        expect(page.get_by_test_id("login-form")).to_be_visible(timeout=5000)
