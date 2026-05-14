# tests/browser/admin/test_dashboard.py
"""Browser tests for /admin/ dashboard page (M8 W7).

Consolidated from TestDashboard + TestNavigation in tests/test_web_ui_browser.py.
URL prefix: /admin/ (was / in old Jinja2 UI).
Selectors: data-testid (was .stat .number, nav a:has-text, etc.).
"""
import pytest

pytestmark = [pytest.mark.browser, pytest.mark.postgres]

ADMIN_BASE = "/admin"


class TestDashboard:
    def test_dashboard_title_visible(self, astro_server, clean_browser, page):
        """GET /admin/ → Dashboard heading and stats grid are visible."""
        page.goto(f"{astro_server}{ADMIN_BASE}/")
        page.wait_for_load_state("load")

        # Stats cards (data-testid from StatsCard component)
        assert page.get_by_test_id("stat-profiles").is_visible()
        assert page.get_by_test_id("stat-repos").is_visible()
        assert page.get_by_test_id("stat-api-keys").is_visible()

    def test_empty_state_profiles_visible(self, astro_server, clean_browser, page):
        """Empty DB → profiles-empty-state element visible on dashboard."""
        page.goto(f"{astro_server}{ADMIN_BASE}/")
        page.wait_for_load_state("load")

        assert page.get_by_test_id("profiles-empty-state").is_visible()

    def test_empty_state_has_link_to_repos(self, astro_server, clean_browser, page):
        """Dashboard empty state includes a link to /admin/repos."""
        page.goto(f"{astro_server}{ADMIN_BASE}/")
        page.wait_for_load_state("load")

        # go-to-repos-link or profiles-empty-state contains a repos link
        assert page.get_by_test_id("go-to-repos-link").is_visible()


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

        assert page.get_by_test_id("quicklink-repositories").is_visible()
        assert page.get_by_test_id("quicklink-api-keys").is_visible()
        assert page.get_by_test_id("quicklink-ssh-keys").is_visible()
        assert page.get_by_test_id("quicklink-operations").is_visible()

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

        assert page.get_by_test_id("nav-logout").is_visible()
