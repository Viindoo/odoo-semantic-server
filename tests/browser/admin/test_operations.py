# tests/browser/admin/test_operations.py
"""Browser tests for /admin/operations page — migrations display + FERNET placeholder (M9 W-UO).

Tests:
  - TestMigrationsSection: migrations table renders on operations page.
  - TestFernetPlaceholder: FERNET section shows CLI hint, no trigger button.
"""
import pytest
from playwright.sync_api import expect

pytestmark = [pytest.mark.browser, pytest.mark.postgres]

OPS_URL = "/admin/operations"


class TestMigrationsSection:
    def test_migrations_heading_visible(self, astro_server, clean_browser, page):
        """Operations page renders 'Database Migrations' heading."""
        page.goto(f"{astro_server}{OPS_URL}")
        page.wait_for_load_state("load")

        heading = page.get_by_text("Database Migrations")
        expect(heading).to_be_visible(timeout=5000)

    def test_migrations_list_container_present(self, astro_server, clean_browser, page):
        """data-testid=migrations-list container is present on operations page."""
        page.goto(f"{astro_server}{OPS_URL}")
        page.wait_for_load_state("load")

        expect(page.get_by_test_id("migrations-list")).to_be_visible(timeout=5000)

    def test_migrations_readonly_description_visible(self, astro_server, clean_browser, page):
        """Read-only description text is visible under Migrations heading."""
        page.goto(f"{astro_server}{OPS_URL}")
        page.wait_for_load_state("load")

        description = page.get_by_text("read-only")
        expect(description).to_be_visible(timeout=5000)

    def test_migrations_no_trigger_button(self, astro_server, clean_browser, page):
        """Migrations section must NOT have a trigger/run button (read-only)."""
        page.goto(f"{astro_server}{OPS_URL}")
        page.wait_for_load_state("load")

        # Confirm there is no button inside the migrations-list container
        migrations_container = page.get_by_test_id("migrations-list")
        expect(migrations_container).to_be_visible(timeout=5000)
        # No <button> element inside the migrations-list
        buttons_in_section = migrations_container.locator("button")
        assert buttons_in_section.count() == 0, (
            "Migrations section must be read-only — no trigger button allowed"
        )


class TestFernetPlaceholder:
    def test_fernet_heading_visible(self, astro_server, clean_browser, page):
        """Operations page renders 'FERNET Key Rotation' heading."""
        page.goto(f"{astro_server}{OPS_URL}")
        page.wait_for_load_state("load")

        heading = page.get_by_text("FERNET Key Rotation")
        expect(heading).to_be_visible(timeout=5000)

    def test_fernet_cli_hint_container_present(self, astro_server, clean_browser, page):
        """data-testid=fernet-rotation-cli-hint container is present."""
        page.goto(f"{astro_server}{OPS_URL}")
        page.wait_for_load_state("load")

        expect(page.get_by_test_id("fernet-rotation-cli-hint")).to_be_visible(timeout=5000)

    def test_fernet_cli_command_text_visible(self, astro_server, clean_browser, page):
        """CLI hint shows the rotate-fernet command text."""
        page.goto(f"{astro_server}{OPS_URL}")
        page.wait_for_load_state("load")

        hint = page.get_by_test_id("fernet-rotation-cli-hint")
        expect(hint).to_be_visible(timeout=5000)
        # Command text should contain the CLI subcommand
        expect(hint).to_contain_text("rotate-fernet")

    def test_fernet_no_trigger_button(self, astro_server, clean_browser, page):
        """FERNET section must NOT have a trigger button (deferred to M10)."""
        page.goto(f"{astro_server}{OPS_URL}")
        page.wait_for_load_state("load")

        hint = page.get_by_test_id("fernet-rotation-cli-hint")
        expect(hint).to_be_visible(timeout=5000)

        # No <button> inside the CLI hint block
        buttons_in_hint = hint.locator("button")
        assert buttons_in_hint.count() == 0, (
            "FERNET UI trigger is deferred to M10 — no button should be present"
        )

    def test_fernet_deferred_message_visible(self, astro_server, clean_browser, page):
        """FERNET section shows 'deferred to M10' or similar UI-deferral text."""
        page.goto(f"{astro_server}{OPS_URL}")
        page.wait_for_load_state("load")

        # The page text should indicate M10 deferral
        page_text = page.locator("body").inner_text()
        assert "M10" in page_text or "deferred" in page_text.lower(), (
            "FERNET section should communicate that UI trigger is deferred"
        )
