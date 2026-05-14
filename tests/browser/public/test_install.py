# tests/browser/public/test_install.py
"""Browser tests for the install/snippet UX on the landing page (M8 W7).

The InstallSnippets component is embedded on the landing page (/). It provides
tab-based install instructions for Claude Code, Codex CLI, Gemini CLI, VS Code,
and Antigravity. Tests verify the tab UX works correctly.

Note: A dedicated /install/ page may be added in a later milestone. These tests
cover the InstallSnippets component on / until that page exists.
"""
import pytest

pytestmark = pytest.mark.browser


class TestInstallSnippets:
    def test_install_snippets_tab_buttons_present(self, astro_server, page):
        """Landing page / → all 5 install tab buttons are visible."""
        page.goto(f"{astro_server}/")
        page.wait_for_load_state("load")

        assert page.get_by_role("tab", name="Claude Code").is_visible()
        assert page.get_by_role("tab", name="Codex CLI").is_visible()
        assert page.get_by_role("tab", name="Gemini CLI").is_visible()
        assert page.get_by_role("tab", name="VS Code").is_visible()
        assert page.get_by_role("tab", name="Antigravity").is_visible()

    def test_claude_code_tab_active_by_default(self, astro_server, page):
        """Claude Code tab is active/selected by default."""
        page.goto(f"{astro_server}/")
        page.wait_for_load_state("load")

        # The active tab button has aria-selected="true"
        claude_tab = page.get_by_role("tab", name="Claude Code")
        assert claude_tab.get_attribute("aria-selected") == "true"

    def test_clicking_tab_shows_its_panel(self, astro_server, page):
        """Clicking Codex CLI tab shows that tab's panel content."""
        page.goto(f"{astro_server}/")
        page.wait_for_load_state("load")

        # Click Codex CLI tab
        page.get_by_role("tab", name="Codex CLI").click()
        page.wait_for_timeout(200)

        # Codex CLI panel should now be visible (contains codex-specific content)
        codex_panel = page.locator("#tab-codex")
        assert codex_panel.is_visible()

        # Claude panel should now be hidden
        claude_panel = page.locator("#tab-claude")
        assert not claude_panel.is_visible()
