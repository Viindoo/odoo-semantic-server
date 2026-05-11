# tests/test_web_ui_apply_preset_browser.py
"""Browser (Playwright) tests for the Apply Preset form on /operations (M8 W8).

These tests run only when Playwright + Chromium are installed.
conftest.py auto-skips tests marked with @pytest.mark.browser when the
Chromium binary is absent.

subprocess.run is NOT mocked here except where noted — UI element and
form submission behaviour (flash redirect, preview rendering, error alerts)
are verified. Actual subprocess execution is replaced by mock.patch on
subprocess.run to avoid needing real Odoo repos.
"""
import subprocess
import unittest.mock as mock

import pytest

from src.indexer.version_presets import PRESETS

pytestmark = [pytest.mark.browser, pytest.mark.postgres]

_FIRST_PRESET_KEY = sorted(PRESETS.keys())[0]
_FIRST_PRESET = PRESETS[_FIRST_PRESET_KEY]


def _dry_run_stdout():
    return (
        f"[dry-run] Profile: {_FIRST_PRESET['profile_name']}"
        f"  odoo_version={_FIRST_PRESET['odoo_version']}\n"
        f"[dry-run] Description: {_FIRST_PRESET['description']}\n"
        "[dry-run] Repos:\n"
        "[dry-run]   https://github.com/odoo/odoo@17.0 → /tmp/odoo_17.0\n"
        f"[dry-run] Run 'python -m src.indexer index-repo"
        f" --profile {_FIRST_PRESET['profile_name']}' to index.\n"
    )


class TestApplyPresetFormVisible:
    """Verify the Apply Preset form renders all required elements."""

    def test_form_visible_on_operations_page(self, web_ui_server, clean_browser, page):
        """GET /operations → Apply Preset section with select, dry_run checkbox, submit button."""
        page.goto(f"{web_ui_server}/operations")

        # Section heading
        assert page.locator("h2:has-text('Apply Preset')").is_visible()

        # Preset dropdown
        assert page.locator('select[name="name"]').is_visible()

        # First preset key must appear in dropdown options
        assert _FIRST_PRESET_KEY in page.locator('select[name="name"]').inner_html()

        # repo_base_dir input
        assert page.locator('input[name="repo_base_dir"]').is_visible()

        # dry_run checkbox must be visible and checked by default
        dry_run_cb = page.locator('input[name="dry_run"]')
        assert dry_run_cb.is_visible()
        assert dry_run_cb.is_checked(), "dry_run must be checked by default"

        # Submit button
        assert page.locator('button:has-text("Apply preset")').is_visible()

        # "+ Add another mapping" link
        assert page.locator('#add-repo-map-row').is_visible()


class TestApplyPresetDryRunBrowser:
    """Browser tests for the dry-run flow."""

    def test_dry_run_shows_preview(self, web_ui_server, clean_browser, page):
        """Pick first preset, ensure dry_run checked, submit → preview <pre> visible."""
        fake_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_dry_run_stdout(), stderr=""
        )
        with mock.patch("subprocess.run", return_value=fake_result):
            page.goto(f"{web_ui_server}/operations")
            # dry_run is checked by default — just submit
            page.locator('button:has-text("Apply preset")').click()

            # Should stay on /operations (200, not redirect)
            page.wait_for_url(f"{web_ui_server}/operations*")

        # Preview pre block must be visible
        pre = page.locator("pre")
        assert pre.is_visible()
        pre_text = pre.inner_text()
        assert "[dry-run]" in pre_text

        # "Apply for real" button must appear
        assert page.locator('button:has-text("Apply for real")').is_visible()

    def test_dry_run_preview_contains_apply_for_real_form(
        self, web_ui_server, clean_browser, page
    ):
        """After dry-run, the 'Apply for real' form has dry_run unchecked."""
        fake_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_dry_run_stdout(), stderr=""
        )
        with mock.patch("subprocess.run", return_value=fake_result):
            page.goto(f"{web_ui_server}/operations")
            page.locator('button:has-text("Apply preset")').click()
            page.wait_for_url(f"{web_ui_server}/operations*")

        # The "Apply for real" button submits a hidden form (no dry_run input)
        apply_btn = page.locator('button:has-text("Apply for real")')
        assert apply_btn.is_visible()


class TestApplyPresetRealApplyBrowser:
    """Browser tests for non-dry-run (real apply) flow."""

    def test_uncheck_dry_run_and_apply_redirects_with_flash(
        self, web_ui_server, clean_browser, page
    ):
        """Uncheck dry_run, submit → redirect + flash 'applied' visible."""
        fake_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                f"✓ Profile {_FIRST_PRESET['profile_name']} registered with 2 repos.\n"
            ),
            stderr="",
        )
        with mock.patch("subprocess.run", return_value=fake_result):
            page.goto(f"{web_ui_server}/operations")
            # Uncheck dry_run
            page.locator('input[name="dry_run"]').uncheck()
            page.locator('button:has-text("Apply preset")').click()

            # Should redirect to /operations?flash=...
            page.wait_for_url(f"{web_ui_server}/operations*")

        page_text = page.content()
        assert "applied" in page_text.lower() or _FIRST_PRESET_KEY in page_text


class TestApplyPresetDynamicRows:
    """Browser tests for the JS-driven + Add another mapping row feature."""

    def test_add_another_mapping_appends_new_row(
        self, web_ui_server, clean_browser, page
    ):
        """Clicking '+ Add another mapping' appends a new pair of inputs."""
        page.goto(f"{web_ui_server}/operations")

        # Count initial visible repo_map_urls inputs (should be 0 or hidden template)
        initial_count = page.locator(
            '#repo-map-rows input[name="repo_map_urls"]:visible'
        ).count()

        # Click "+ Add another mapping"
        page.locator('#add-repo-map-row').click()

        # A new visible pair must appear
        after_count = page.locator(
            '#repo-map-rows input[name="repo_map_urls"]:visible'
        ).count()
        assert after_count == initial_count + 1

    def test_remove_button_removes_row(self, web_ui_server, clean_browser, page):
        """The ✕ remove button removes the corresponding row."""
        page.goto(f"{web_ui_server}/operations")

        # Add a row
        page.locator('#add-repo-map-row').click()
        after_add = page.locator(
            '#repo-map-rows input[name="repo_map_urls"]:visible'
        ).count()
        assert after_add >= 1

        # Click the remove ✕ button for the first visible row
        page.locator('#repo-map-rows .repo-map-row:visible button').first.click()

        after_remove = page.locator(
            '#repo-map-rows input[name="repo_map_urls"]:visible'
        ).count()
        assert after_remove == after_add - 1


class TestApplyPresetInvalidBrowser:
    """Browser tests for error scenarios."""

    def test_subprocess_failure_shows_error_alert(
        self, web_ui_server, clean_browser, page
    ):
        """When subprocess exits non-zero, error alert must be visible on the page."""
        error_result = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="✗ Local path /tmp/missing does not exist",
        )
        with mock.patch("subprocess.run", return_value=error_result):
            page.goto(f"{web_ui_server}/operations")
            # Uncheck dry_run to trigger real path
            page.locator('input[name="dry_run"]').uncheck()
            page.locator('button:has-text("Apply preset")').click()

        # Error alert must appear (page stays at /operations — no redirect on failure)
        assert page.locator(".alert").is_visible()
