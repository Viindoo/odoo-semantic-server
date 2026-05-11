# tests/test_web_ui_reset_embed_browser.py
"""Browser E2E tests for the reset-embed feature (M8 W4).

Uses Playwright headless Chromium via web_ui_server + clean_browser fixtures.
Auth is bypassed via WEBUI_AUTH_DISABLED=1.

Skipped automatically when chromium is not installed (conftest.py hook).
"""
import pytest

pytestmark = [pytest.mark.browser, pytest.mark.postgres]


class TestResetEmbedBrowser:
    def test_reset_embed_button_visible_when_head_sha_set(
        self, web_ui_server, clean_browser, page
    ):
        """After setting head_sha on a repo, the 🔄 button is visible in the table."""
        pg_conn = clean_browser
        page.goto(f"{web_ui_server}/repos")

        # Add profile
        page.fill("input[name='name']", "re_browser_profile_visible")
        page.fill("input[name='version']", "99.0")
        page.click("button:has-text('Add Profile')")
        page.wait_for_load_state("load")

        # Add repo
        page.fill("input[name='branch']", "99.0")
        page.fill("input[name='local_path']", "/tmp/re_browser_test_visible")
        page.click("button:has-text('Add Repo')")
        page.wait_for_load_state("load")

        # Set head_sha directly in DB (simulates a prior indexed run)
        with pg_conn.cursor() as cur:
            cur.execute(
                "UPDATE repos SET head_sha = 'abc123testsha' WHERE local_path = %s",
                ("/tmp/re_browser_test_visible",),
            )

        # Reload page — button should now be visible
        page.reload()
        page.wait_for_load_state("load")

        assert page.locator("button[title='Reset embed state and re-index']").is_visible()

    def test_reset_embed_button_not_visible_when_head_sha_null(
        self, web_ui_server, clean_browser, page
    ):
        """When head_sha IS NULL (default after add-repo), the 🔄 button is NOT shown."""
        page.goto(f"{web_ui_server}/repos")

        # Add profile
        page.fill("input[name='name']", "re_browser_profile_hidden")
        page.fill("input[name='version']", "99.0")
        page.click("button:has-text('Add Profile')")
        page.wait_for_load_state("load")

        # Add repo — head_sha defaults to NULL
        page.fill("input[name='branch']", "99.0")
        page.fill("input[name='local_path']", "/tmp/re_browser_test_hidden")
        page.click("button:has-text('Add Repo')")
        page.wait_for_load_state("load")

        # The 🔄 button must NOT be present for this repo
        assert not page.locator("button[title='Reset embed state and re-index']").is_visible()

    def test_reset_embed_shows_flash_after_click(
        self, web_ui_server, clean_browser, page
    ):
        """Click 🔄 → accept confirm dialog → flash 'Re-embedding started' appears."""
        pg_conn = clean_browser
        page.goto(f"{web_ui_server}/repos")

        # Add profile
        page.fill("input[name='name']", "re_browser_profile_flash")
        page.fill("input[name='version']", "99.0")
        page.click("button:has-text('Add Profile')")
        page.wait_for_load_state("load")

        # Add repo
        page.fill("input[name='branch']", "99.0")
        page.fill("input[name='local_path']", "/tmp/re_browser_test_flash")
        page.click("button:has-text('Add Repo')")
        page.wait_for_load_state("load")

        # Seed head_sha so the button appears
        with pg_conn.cursor() as cur:
            cur.execute(
                "UPDATE repos SET head_sha = 'deadbeef' WHERE local_path = %s",
                ("/tmp/re_browser_test_flash",),
            )

        page.reload()
        page.wait_for_load_state("load")

        # Accept the confirm() dialog and click 🔄
        page.on("dialog", lambda d: d.accept())
        page.click("button[title='Reset embed state and re-index']")
        page.wait_for_load_state("load")

        content = page.content()
        assert "re-embedding" in content.lower() or "Re-embedding" in content
