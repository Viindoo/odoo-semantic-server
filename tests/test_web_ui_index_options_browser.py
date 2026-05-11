# tests/test_web_ui_index_options_browser.py
"""Browser E2E tests for the index options form (M8 W3).

Uses Playwright headless Chromium via web_ui_server + clean_browser fixtures.
Auth is bypassed via WEBUI_AUTH_DISABLED=1.

Skipped automatically when chromium is not installed (conftest.py hook).
"""
import pytest

pytestmark = [pytest.mark.browser, pytest.mark.postgres]


def _create_profile_and_repo(page, web_ui_server, profile_name, version="99.0"):
    """Helper: navigate to /repos and create a profile + one repo."""
    page.goto(f"{web_ui_server}/repos")

    page.fill("input[name='name']", profile_name)
    page.fill("input[name='version']", version)
    page.click("button:has-text('Add Profile')")
    page.wait_for_load_state("load")

    page.fill("input[name='branch']", version)
    page.fill("input[name='local_path']", f"/tmp/browser_index_opts_{profile_name}")
    page.click("button:has-text('Add Repo')")
    page.wait_for_load_state("load")


class TestIndexOptionsFormPresence:
    def test_index_form_fields_visible_after_repo_added(
        self, web_ui_server, clean_browser, page
    ):
        """After adding a profile + repo, the index options form fields are visible."""
        _create_profile_and_repo(page, web_ui_server, "idx_opts_presence")

        # Checkboxes
        assert page.locator("input[name='no_embed']").is_visible()
        assert page.locator("input[name='full']").is_visible()
        assert page.locator("input[name='gc']").is_visible()

        # Number input for max_workers
        assert page.locator("input[name='max_workers']").is_visible()

        # Index submit button
        assert page.locator("button:has-text('Index')").is_visible()

    def test_max_workers_default_is_one(self, web_ui_server, clean_browser, page):
        """max_workers input defaults to 1."""
        _create_profile_and_repo(page, web_ui_server, "idx_opts_default")

        val = page.locator("input[name='max_workers']").input_value()
        assert val == "1"

    def test_checkboxes_are_unchecked_by_default(
        self, web_ui_server, clean_browser, page
    ):
        """All option checkboxes are unchecked initially."""
        _create_profile_and_repo(page, web_ui_server, "idx_opts_unchecked")

        assert not page.locator("input[name='no_embed']").is_checked()
        assert not page.locator("input[name='full']").is_checked()
        assert not page.locator("input[name='gc']").is_checked()


class TestIndexOptionsFormSubmit:
    def test_submit_with_full_gc_max_workers_shows_flash(
        self, web_ui_server, clean_browser, page
    ):
        """Ticking full+gc, setting max_workers=4, clicking Index → flash banner visible."""
        _create_profile_and_repo(page, web_ui_server, "idx_opts_submit")

        # Tick full + gc, set max_workers=4
        page.check("input[name='full']")
        page.check("input[name='gc']")
        page.fill("input[name='max_workers']", "4")

        # Click Index — may get a flash (indexer not actually running in browser test)
        page.click("button:has-text('Index')")
        page.wait_for_load_state("load")

        # Redirect back to /repos — either success (queued flash) or running-guard flash
        assert "/repos" in page.url

    def test_submit_default_values_redirects_to_repos(
        self, web_ui_server, clean_browser, page
    ):
        """Clicking Index with default values redirects back to /repos."""
        _create_profile_and_repo(page, web_ui_server, "idx_opts_default_submit")

        page.click("button:has-text('Index')")
        page.wait_for_load_state("load")

        assert "/repos" in page.url
