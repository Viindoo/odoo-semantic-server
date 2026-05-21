# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_web_ui_operations.py
"""Browser-level E2E tests for the /operations page (M8 W0).

Fixtures: web_ui_server, clean_browser, page — from tests/conftest.py.
Tests are skipped automatically when Playwright chromium binary is missing
(handled by pytest_collection_modifyitems in conftest.py).

Note: These browser tests test the Astro frontend, not the FastAPI JSON API directly.
"""
import pytest

pytestmark = [pytest.mark.browser, pytest.mark.postgres]


class TestOperationsNavLink:
    def test_operations_nav_link_visible(self, web_ui_server, clean_browser, page):
        """Navigating to /repos should show an 'Operations' nav link."""
        page.goto(f"{web_ui_server}/repos")
        assert page.locator("nav a:has-text('Operations')").is_visible()

    def test_active_class_on_operations(self, web_ui_server, clean_browser, page):
        """Navigating to /operations sets the nav active class on 'Operations'."""
        page.goto(f"{web_ui_server}/operations")
        active_text = page.locator("nav a.active").inner_text()
        assert "Operations" in active_text


class TestOperationsPageContent:
    def test_operations_page_loads(self, web_ui_server, clean_browser, page):
        """GET /operations renders all 3 section headers."""
        page.goto(f"{web_ui_server}/operations")
        assert page.locator("h2:has-text('Index Odoo Core Specs')").is_visible()
        assert page.locator("h2:has-text('Seed Pattern Catalogue')").is_visible()
        assert page.locator("h2:has-text('Apply Preset')").is_visible()
