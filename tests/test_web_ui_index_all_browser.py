# tests/test_web_ui_index_all_browser.py
"""Browser E2E tests for the index-all bulk operation (M8 W7).

Uses Playwright headless Chromium via web_ui_server + clean_browser fixtures.
Auth is bypassed via WEBUI_AUTH_DISABLED=1.

Skipped automatically when chromium is not installed (conftest.py hook).
"""
import pytest

pytestmark = [pytest.mark.browser, pytest.mark.postgres]


class TestIndexAllBrowser:
    def test_bulk_operations_card_not_visible_when_no_profiles(
        self, web_ui_server, clean_browser, page
    ):
        """When no profiles exist, the Bulk Operations card must NOT be visible."""
        page.goto(f"{web_ui_server}/repos")
        page.wait_for_load_state("load")

        # The card is guarded by {% if profiles %} — it must not render at all
        assert not page.locator("h2:has-text('Bulk Operations')").is_visible()

    def test_bulk_operations_card_visible_after_profile_added(
        self, web_ui_server, clean_browser, page
    ):
        """After adding a profile the Bulk Operations card and all 4 form fields appear."""
        page.goto(f"{web_ui_server}/repos")

        # Add a profile
        page.fill("input[name='name']", "ia_browser_profile_visible")
        page.fill("input[name='version']", "99.0")
        page.click("button:has-text('Add Profile')")
        page.wait_for_load_state("load")

        # Bulk Operations card must now be visible
        assert page.locator("h2:has-text('Bulk Operations')").is_visible()

        # All 4 form controls exist
        assert page.locator("input[name='no_embed']").is_visible()
        assert page.locator("input[name='full']").is_visible()
        assert page.locator("input[name='max_workers']").is_visible()
        assert page.locator("input[name='profile_workers']").is_visible()

        # Submit button exists
        assert page.locator("button:has-text('Index All Profiles')").is_visible()

    def test_index_all_submit_shows_flash(
        self, web_ui_server, clean_browser, page
    ):
        """Tick full + no_embed, set workers, submit → flash 'Index all started' visible."""
        page.goto(f"{web_ui_server}/repos")

        # Add a profile so the bulk card appears
        page.fill("input[name='name']", "ia_browser_profile_flash")
        page.fill("input[name='version']", "99.0")
        page.click("button:has-text('Add Profile')")
        page.wait_for_load_state("load")

        # Fill in the bulk form
        page.check("input[name='full']")
        page.check("input[name='no_embed']")
        page.fill("input[name='max_workers']", "2")
        page.fill("input[name='profile_workers']", "2")

        # Submit — the form POSTs to /repos/index-all and redirects back to /repos
        page.click("button:has-text('Index All Profiles')")
        page.wait_for_load_state("load")

        # Flash message must contain "Index all started"
        content = page.content()
        assert "Index all started" in content or "index+all+started" in content.lower()
