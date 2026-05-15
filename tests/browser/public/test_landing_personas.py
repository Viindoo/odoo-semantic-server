# tests/browser/public/test_landing_personas.py
"""Browser smoke tests for landing-v2 multi-persona expansion + interactive demos.

Tests:
  1. All 5 persona cards render (3 tier-1 + 2 tier-2)
  2. Graph showcase slot is present (React island, hydrates client-side)
  3. Prompt simulator slot is present (React island)
  4. Token benchmark slot is present (React island)
  5. /benchmarks/ methodology page loads
"""
import pytest

pytestmark = pytest.mark.browser


class TestLandingPersonas:
    def test_five_persona_cards_visible(self, astro_server, page):
        """GET / → all 5 persona cards (developer, consultant, pm, sales, marketer) visible."""
        page.goto(f"{astro_server}/")
        page.wait_for_load_state("load")

        for slug in ("developer", "consultant", "pm", "sales", "marketer"):
            card = page.get_by_test_id(f"persona-card-{slug}")
            assert card.is_visible(), f"persona-card-{slug} not visible"


class TestInteractiveDemos:
    def test_graph_showcase_slot_present(self, astro_server, page):
        """GET / → graph-showcase React island is mounted (data-testid in DOM)."""
        page.goto(f"{astro_server}/")
        page.wait_for_load_state("load")

        showcase = page.get_by_test_id("graph-showcase")
        # The element is in DOM even before client:visible hydration (Astro renders the wrapper)
        showcase.scroll_into_view_if_needed()
        assert showcase.count() >= 1

    def test_prompt_simulator_slot_present(self, astro_server, page):
        """GET / → prompt-simulator React island is mounted."""
        page.goto(f"{astro_server}/")
        page.wait_for_load_state("load")

        sim = page.get_by_test_id("prompt-simulator")
        sim.scroll_into_view_if_needed()
        assert sim.count() >= 1

    def test_token_benchmark_slot_present(self, astro_server, page):
        """GET / → token-benchmark React island is mounted."""
        page.goto(f"{astro_server}/")
        page.wait_for_load_state("load")

        bench = page.get_by_test_id("token-benchmark")
        bench.scroll_into_view_if_needed()
        assert bench.count() >= 1


class TestBenchmarksPage:
    def test_benchmarks_page_loads(self, astro_server, page):
        """GET /benchmarks/ → methodology page renders with title."""
        response = page.goto(f"{astro_server}/benchmarks/")
        assert response is not None
        assert response.status == 200
        page.wait_for_load_state("load")

        h1 = page.locator("h1").first
        assert h1.is_visible()
        assert "measured" in h1.inner_text().lower() or "methodology" in h1.inner_text().lower()
