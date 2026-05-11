# tests/test_web_ui_delete_repo_browser.py
"""Browser E2E tests for the delete-repo feature (M8 W2).

Uses Playwright headless Chromium via web_ui_server + clean_browser fixtures.
Auth is bypassed via WEBUI_AUTH_DISABLED=1.

Skipped automatically when chromium is not installed (conftest.py hook).
"""
import pytest

pytestmark = [pytest.mark.browser, pytest.mark.postgres]


class TestDeleteRepoBrowser:
    def test_delete_repo_button_present_after_repo_added(
        self, web_ui_server, clean_browser, page
    ):
        """After adding a profile + repo, the 🗑 delete-repo button is visible."""
        page.goto(f"{web_ui_server}/repos")

        # Add profile
        page.fill("input[name='name']", "del_repo_test_profile")
        page.fill("input[name='version']", "99.0")
        page.click("button:has-text('Add Profile')")
        page.wait_for_load_state("load")

        # Add repo
        page.fill("input[name='branch']", "99.0")
        page.fill("input[name='local_path']", "/tmp/del_repo_browser_test")
        page.click("button:has-text('Add Repo')")
        page.wait_for_load_state("load")

        # Delete-repo button should be visible on the row
        assert page.locator("button[title='Delete repo']").is_visible()

    def test_delete_repo_removes_row_and_shows_flash(
        self, web_ui_server, clean_browser, page
    ):
        """Click 🗑 on repo row → accept dialog → row disappears + flash 'deleted' shown."""
        page.goto(f"{web_ui_server}/repos")

        # Add profile
        page.fill("input[name='name']", "victim_repo_browser_profile")
        page.fill("input[name='version']", "99.0")
        page.click("button:has-text('Add Profile')")
        page.wait_for_load_state("load")

        # Add repo (two so we can verify the sibling survives)
        page.fill("input[name='branch']", "99.0")
        page.fill("input[name='local_path']", "/tmp/victim_repo_browser_to_delete")
        page.click("button:has-text('Add Repo')")
        page.wait_for_load_state("load")

        # Accept the confirm() dialog before clicking
        page.on("dialog", lambda d: d.accept())
        page.click("button[title='Delete repo']")
        page.wait_for_load_state("load")

        content = page.content()
        # The repo local path should be gone
        assert "victim_repo_browser_to_delete" not in content
        # Flash message should mention "deleted"
        assert "deleted" in content.lower()

    def test_delete_one_repo_leaves_sibling_intact(
        self, web_ui_server, clean_browser, page
    ):
        """Delete the first repo; the second repo's row must remain visible."""
        page.goto(f"{web_ui_server}/repos")

        # Add profile
        page.fill("input[name='name']", "two_repos_profile_browser")
        page.fill("input[name='version']", "99.0")
        page.click("button:has-text('Add Profile')")
        page.wait_for_load_state("load")

        # Add first repo
        page.fill("input[name='branch']", "99.0")
        page.fill("input[name='local_path']", "/tmp/browser_repo_to_delete_first")
        page.click("button:has-text('Add Repo')")
        page.wait_for_load_state("load")

        # Add second repo
        page.fill("input[name='branch']", "99.0")
        page.fill("input[name='local_path']", "/tmp/browser_repo_sibling_keep")
        page.click("button:has-text('Add Repo')")
        page.wait_for_load_state("load")

        # Verify both repos present
        assert "browser_repo_to_delete_first" in page.content()
        assert "browser_repo_sibling_keep" in page.content()

        # Delete only the first repo (click first 🗑 repo button)
        page.on("dialog", lambda d: d.accept())
        page.locator("button[title='Delete repo']").first.click()
        page.wait_for_load_state("load")

        content = page.content()
        assert "browser_repo_to_delete_first" not in content
        assert "browser_repo_sibling_keep" in content
        assert "deleted" in content.lower()
