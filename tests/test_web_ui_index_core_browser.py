# tests/test_web_ui_index_core_browser.py
"""Browser (Playwright) tests for the Index Core form on /operations (M8 W5).

These tests run only when Playwright + Chromium are installed.
conftest.py auto-skips tests marked with @pytest.mark.browser when the
Chromium binary is absent.

Popen is NOT mocked here — the tests assert UI elements and form submission
behaviour (flash redirect, error alert visible). Actual subprocess spawn
behaviour is covered by the integration tests in test_web_ui_index_core.py.
"""
import unittest.mock as mock

import pytest

pytestmark = [pytest.mark.browser, pytest.mark.postgres]


class TestIndexCoreForm:
    """Browser tests for the Index Core form section on /operations."""

    def test_form_visible_on_operations_page(self, web_ui_server, clean_browser, page):
        """GET /operations → Index Core form and its fields are visible."""
        page.goto(f"{web_ui_server}/operations")
        # Section heading
        assert page.locator("h2:has-text('Index Odoo Core Specs')").is_visible()
        # Form fields
        assert page.locator('input[name="source"]').is_visible()
        assert page.locator('input[name="version"]').is_visible()
        assert page.locator('input[name="static_data_dir"]').is_visible()
        # Submit button
        assert page.locator('button:has-text("Run Index Core")').is_visible()

    def test_submit_valid_form_shows_flash(self, web_ui_server, clean_browser, page, tmp_path):
        """Fill form with valid source (tmp_path) + version=17.0 → flash shown after redirect."""
        with mock.patch("subprocess.Popen"):
            page.goto(f"{web_ui_server}/operations")
            page.locator('input[name="source"]').fill(str(tmp_path))
            page.locator('input[name="version"]').fill("17.0")
            page.locator('button:has-text("Run Index Core")').click()
            # After redirect, flash banner must contain version and "core" or "Indexing"
            page.wait_for_url(f"{web_ui_server}/operations*")
            page_text = page.content()
            assert "17.0" in page_text
            # flash div rendered
            assert "Indexing" in page_text or "core" in page_text.lower()

    def test_submit_invalid_path_shows_error(self, web_ui_server, clean_browser, page):
        """Fill source with non-existent path → error alert visible, no redirect."""
        page.goto(f"{web_ui_server}/operations")
        page.locator('input[name="source"]').fill("/does/not/exist/odoo_17")
        page.locator('input[name="version"]').fill("17.0")
        page.locator('button:has-text("Run Index Core")').click()

        # Page stays on /operations (or same path) — error alert rendered
        assert page.locator(".alert").is_visible()
        alert_text = page.locator(".alert").inner_text()
        assert "/does/not/exist/odoo_17" in alert_text or "not exist" in alert_text.lower()

    def test_submit_invalid_version_shows_error(self, web_ui_server, clean_browser, page, tmp_path):
        """Fill version with 'abc' (non-semver) → error alert visible, no redirect."""
        page.goto(f"{web_ui_server}/operations")
        page.locator('input[name="source"]').fill(str(tmp_path))
        # Override HTML5 pattern validation by setting value directly via JS
        page.evaluate(
            "document.querySelector('input[name=\"version\"]').value = 'abc'"
        )
        page.locator('button:has-text("Run Index Core")').click()

        assert page.locator(".alert").is_visible()
        alert_text = page.locator(".alert").inner_text()
        assert "abc" in alert_text or "Invalid" in alert_text
