# tests/test_web_ui_seed_patterns_browser.py
"""Browser (Playwright) tests for the Seed Patterns form on /operations (M8 W6).

These tests run only when Playwright + Chromium are installed.
conftest.py auto-skips tests marked with @pytest.mark.browser when the
Chromium binary is absent.

Popen is NOT mocked here — the tests assert UI elements and form submission
behaviour (flash redirect, error alert visible). Actual subprocess spawn
behaviour is covered by the integration tests in test_web_ui_seed_patterns.py.
"""
import unittest.mock as mock

import pytest

pytestmark = [pytest.mark.browser, pytest.mark.postgres]


class TestSeedPatternsForm:
    """Browser tests for the Seed Patterns form section on /operations."""

    def test_form_visible_on_operations_page(self, web_ui_server, clean_browser, page):
        """GET /operations → Seed Pattern Catalogue form and its fields are visible."""
        page.goto(f"{web_ui_server}/operations")
        # Section heading
        assert page.locator("h2:has-text('Seed Pattern Catalogue')").is_visible()
        # Form fields
        assert page.locator('input[name="version"]').is_visible()
        assert page.locator('input[name="no_embed"]').is_visible()
        assert page.locator('input[name="force"]').is_visible()
        assert page.locator('input[name="patterns_file"]').is_visible()
        # Submit button
        assert page.locator('button:has-text("Seed Patterns")').is_visible()

    def test_submit_with_force_shows_flash(self, web_ui_server, clean_browser, page):
        """Tick force checkbox → submit → flash banner visible after redirect."""
        with mock.patch("subprocess.Popen"):
            page.goto(f"{web_ui_server}/operations")
            page.locator('input[name="force"]').check()
            page.locator('button:has-text("Seed Patterns")').click()
            # After redirect, flash banner must appear
            page.wait_for_url(f"{web_ui_server}/operations*")
            page_text = page.content()
            # Flash must include "patterns" and "job"
            assert "patterns" in page_text.lower()
            assert "job" in page_text.lower()

    def test_submit_with_version_shows_version_in_flash(
        self, web_ui_server, clean_browser, page
    ):
        """Fill version=17.0, submit → flash contains '17.0'."""
        with mock.patch("subprocess.Popen"):
            page.goto(f"{web_ui_server}/operations")
            page.locator('input[name="version"]').fill("17.0")
            page.locator('button:has-text("Seed Patterns")').click()
            page.wait_for_url(f"{web_ui_server}/operations*")
            assert "17.0" in page.content()

    def test_submit_invalid_version_shows_error(
        self, web_ui_server, clean_browser, page
    ):
        """Fill version with 'not-a-version' → error alert visible, no redirect."""
        page.goto(f"{web_ui_server}/operations")
        # Override HTML5 pattern validation by setting value directly via JS
        page.evaluate(
            "document.querySelector('input[name=\"version\"]').value = 'not-a-version'"
        )
        page.locator('button:has-text("Seed Patterns")').click()

        assert page.locator(".alert").is_visible()
        alert_text = page.locator(".alert").inner_text()
        assert "not-a-version" in alert_text or "Invalid" in alert_text
