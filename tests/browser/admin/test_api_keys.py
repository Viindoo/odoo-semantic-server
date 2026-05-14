# tests/browser/admin/test_api_keys.py
"""Browser tests for /admin/api-keys page (M8 W7).

Consolidated from TestApiKeysPage in tests/test_web_ui_browser.py.
URL: /admin/api-keys (was /api-keys).
Selectors: data-testid (was .badge-ok, .badge-error, text=...).
"""
import pytest
from playwright.sync_api import expect

pytestmark = [pytest.mark.browser, pytest.mark.postgres]

API_KEYS_URL = "/admin/api-keys"


class TestApiKeysPage:
    def test_empty_state_message_visible(self, astro_server, clean_browser, page):
        """GET /admin/api-keys → api-keys-empty-state visible."""
        page.goto(f"{astro_server}{API_KEYS_URL}")
        page.wait_for_load_state("load")

        expect(page.get_by_test_id("api-keys-empty-state")).to_be_visible(timeout=5000)

    def test_generate_key_button_visible(self, astro_server, clean_browser, page):
        """GET /admin/api-keys → generate-key-button visible."""
        page.goto(f"{astro_server}{API_KEYS_URL}")
        page.wait_for_load_state("load")

        expect(page.get_by_test_id("generate-key-button")).to_be_visible(timeout=5000)

    def test_create_key_shows_raw_key_once(self, astro_server, clean_browser, page):
        """Click generate → fill name → create → new-key-banner visible with osm_ prefix."""
        page.goto(f"{astro_server}{API_KEYS_URL}")
        page.wait_for_load_state("load")

        page.get_by_test_id("generate-key-button").click()
        page.wait_for_timeout(300)
        page.get_by_test_id("api-key-name-input").fill("browser-key-1")
        page.get_by_test_id("create-key-button").click()
        page.wait_for_timeout(800)

        # new-key-banner should appear with the raw key
        expect(page.get_by_test_id("new-key-banner")).to_be_visible(timeout=5000)
        key_text = page.get_by_test_id("new-key-value").inner_text()
        assert key_text.startswith("osm_")

    def test_created_key_appears_in_table(self, astro_server, clean_browser, page):
        """After creating a key, api-key-row is visible in the table."""
        page.goto(f"{astro_server}{API_KEYS_URL}")
        page.wait_for_load_state("load")

        page.get_by_test_id("generate-key-button").click()
        page.wait_for_timeout(300)
        page.get_by_test_id("api-key-name-input").fill("my-browser-key")
        page.get_by_test_id("create-key-button").click()
        page.wait_for_timeout(800)

        expect(page.get_by_test_id("api-key-row")).to_be_visible(timeout=5000)

    def test_deactivate_button_visible_for_active_key(
        self, astro_server, clean_browser, page
    ):
        """After creating a key, deactivate-key-button is visible."""
        page.goto(f"{astro_server}{API_KEYS_URL}")
        page.wait_for_load_state("load")

        page.get_by_test_id("generate-key-button").click()
        page.wait_for_timeout(300)
        page.get_by_test_id("api-key-name-input").fill("key-to-deact-br")
        page.get_by_test_id("create-key-button").click()
        page.wait_for_timeout(800)

        # deactivate-key-button-{id} — use first match
        expect(page.locator('[data-testid^="deactivate-key-button-"]').first).to_be_visible(timeout=5000)

    def test_deactivate_key_shows_inactive_row(self, astro_server, clean_browser, page):
        """Click deactivate → api-key-row-inactive visible."""
        page.goto(f"{astro_server}{API_KEYS_URL}")
        page.wait_for_load_state("load")

        page.get_by_test_id("generate-key-button").click()
        page.wait_for_timeout(300)
        page.get_by_test_id("api-key-name-input").fill("key-to-deact-check")
        page.get_by_test_id("create-key-button").click()
        page.wait_for_timeout(800)

        # Deactivate JS handler starts with `if (!confirm(...)) return;`. Playwright
        # auto-dismisses dialogs unless a handler accepts them.
        page.on("dialog", lambda d: d.accept())
        page.locator('[data-testid^="deactivate-key-button-"]').first.click()

        expect(page.get_by_test_id("api-key-row-inactive")).to_be_visible(timeout=8000)
        # The deactivate button should be gone for this key
        expect(page.locator('[data-testid^="deactivate-key-button-"]')).not_to_be_visible(timeout=5000)
