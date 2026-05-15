# tests/browser/public/test_landing.py
"""Browser smoke tests for the landing page /.

Tests:
  1. Landing page renders with hero heading
  2. Both hero CTAs are present (targeted by data-testid, decoupled from copy)
"""
import pytest

pytestmark = pytest.mark.browser


class TestLandingPage:
    def test_landing_page_renders_heading(self, astro_server, page):
        """GET / → page renders with hero heading visible."""
        page.goto(f"{astro_server}/")
        page.wait_for_load_state("load")

        h1 = page.get_by_test_id("hero-heading")
        assert h1.is_visible()
        heading_text = h1.inner_text()
        # Outcome-first headline must mention verified knowledge or hallucinations
        assert "verified" in heading_text.lower() or "hallucination" in heading_text.lower()

    def test_hero_cta_buttons_present(self, astro_server, page):
        """GET / → primary + ghost CTAs are present (targeted by testid)."""
        page.goto(f"{astro_server}/")
        page.wait_for_load_state("load")

        primary_cta = page.get_by_test_id("cta-try-demo")
        assert primary_cta.is_visible()

        ghost_cta = page.get_by_test_id("cta-connect-ai")
        assert ghost_cta.is_visible()
