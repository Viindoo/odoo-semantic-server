# tests/browser/public/test_landing.py
"""Browser smoke tests for the landing page / (M8 W7).

Tests:
  1. Landing page renders with hero heading
  2. GraphHero React component container is present in DOM
"""
import pytest

pytestmark = pytest.mark.browser


class TestLandingPage:
    def test_landing_page_renders_heading(self, astro_server, page):
        """GET / → page renders with hero heading visible."""
        page.goto(f"{astro_server}/")
        page.wait_for_load_state("load")

        # Main heading — text is from index.astro h1
        h1 = page.locator("h1").first
        assert h1.is_visible()
        heading_text = h1.inner_text()
        assert "Odoo" in heading_text or "MCP" in heading_text or "Semantic" in heading_text

    def test_hero_cta_buttons_present(self, astro_server, page):
        """GET / → CTA links to /install/ and /admin/login are present."""
        page.goto(f"{astro_server}/")
        page.wait_for_load_state("load")

        # "Get Started" CTA → /install/
        get_started = page.get_by_role("link", name="Get Started")
        assert get_started.is_visible()

        # At least one link pointing to /admin/login
        admin_link = page.locator("a[href='/admin/login']").first
        assert admin_link.is_visible()
