# tests/browser/admin/test_dashboard.py
"""Browser tests for /admin/ dashboard page (M8 W7).

Consolidated from TestDashboard + TestNavigation in tests/test_web_ui_browser.py.
URL prefix: /admin/ (was / in old Jinja2 UI).
Selectors: data-testid (was .stat .number, nav a:has-text, etc.).
"""
import pytest
from playwright.sync_api import expect

pytestmark = [pytest.mark.browser, pytest.mark.postgres]

ADMIN_BASE = "/admin"


class TestDashboard:
    def test_dashboard_title_visible(self, astro_server, clean_browser, page):
        """GET /admin/ → Dashboard heading and stats grid are visible."""
        page.goto(f"{astro_server}{ADMIN_BASE}/")
        page.wait_for_load_state("load")

        # Stats cards (data-testid from StatsCard component)
        expect(page.get_by_test_id("stat-profiles")).to_be_visible(timeout=5000)
        expect(page.get_by_test_id("stat-repos")).to_be_visible(timeout=5000)
        expect(page.get_by_test_id("stat-api-keys")).to_be_visible(timeout=5000)

    def test_empty_state_profiles_visible(self, astro_server, clean_browser, page):
        """Empty DB → profiles-empty-state element visible on dashboard."""
        page.goto(f"{astro_server}{ADMIN_BASE}/")
        page.wait_for_load_state("load")

        expect(page.get_by_test_id("profiles-empty-state")).to_be_visible(timeout=5000)

    def test_empty_state_has_link_to_repos(self, astro_server, clean_browser, page):
        """Dashboard empty state includes a link to /admin/repos."""
        page.goto(f"{astro_server}{ADMIN_BASE}/")
        page.wait_for_load_state("load")

        # go-to-repos-link or profiles-empty-state contains a repos link
        expect(page.get_by_test_id("go-to-repos-link")).to_be_visible(timeout=5000)


class TestNavigation:
    def test_nav_links_reach_all_pages(self, astro_server, clean_browser, page):
        """Admin nav links navigate to the correct /admin/* pages."""
        page.goto(f"{astro_server}{ADMIN_BASE}/")
        page.wait_for_load_state("load")

        page.get_by_test_id("nav-api-keys").click()
        page.wait_for_load_state("load")
        assert "/admin/api-keys" in page.url

        page.get_by_test_id("nav-ssh-keys").click()
        page.wait_for_load_state("load")
        assert "/admin/ssh-keys" in page.url

        page.get_by_test_id("nav-repos").click()
        page.wait_for_load_state("load")
        assert "/admin/repos" in page.url

        page.get_by_test_id("nav-dashboard").click()
        page.wait_for_load_state("load")
        assert "/admin" in page.url

    def test_quick_links_present(self, astro_server, clean_browser, page):
        """Dashboard quick-link cards are present for each admin section."""
        page.goto(f"{astro_server}{ADMIN_BASE}/")
        page.wait_for_load_state("load")

        expect(page.get_by_test_id("quicklink-repositories")).to_be_visible(timeout=5000)
        expect(page.get_by_test_id("quicklink-api-keys")).to_be_visible(timeout=5000)
        expect(page.get_by_test_id("quicklink-ssh-keys")).to_be_visible(timeout=5000)
        expect(page.get_by_test_id("quicklink-operations")).to_be_visible(timeout=5000)

    def test_operations_nav_link_reaches_operations_page(
        self, astro_server, clean_browser, page
    ):
        """nav-operations link navigates to /admin/operations."""
        page.goto(f"{astro_server}{ADMIN_BASE}/")
        page.wait_for_load_state("load")

        page.get_by_test_id("nav-operations").click()
        page.wait_for_load_state("load")
        assert "/admin/operations" in page.url

    def test_logout_nav_link_present(self, astro_server, clean_browser, page):
        """nav-logout link is present in the admin sidebar."""
        page.goto(f"{astro_server}{ADMIN_BASE}/")
        page.wait_for_load_state("load")

        expect(page.get_by_test_id("nav-logout")).to_be_visible(timeout=5000)


class TestAdminRouting:
    def test_unknown_admin_subpath_redirects_or_404(
        self, astro_server, clean_browser, page
    ):
        """GET /admin/nonexistent → 404 or auth redirect to /admin/login.

        Moved here from tests/browser/public/test_404.py because the Astro
        middleware calls /api/auth/verify on every /admin/* path, so this
        test needs FastAPI running (admin job has it, public job does not).
        """
        response = page.goto(
            f"{astro_server}{ADMIN_BASE}/nonexistent-page-m8-w7",
            wait_until="load",
        )
        final_url = page.url
        assert response is not None
        assert response.status in (200, 302, 404) and (
            response.status == 404
            or "/admin/login" in final_url
            or "/login" in final_url
        )
