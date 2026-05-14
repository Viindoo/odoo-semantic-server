# tests/test_web_ui_apply_preset_browser.py
"""MIGRATED — see tests/browser/admin/test_repos.py for current browser tests.

All Apply Preset browser tests have been migrated to
tests/browser/admin/test_repos.py::TestApplyPreset as part of M8 W7.

URL change: /operations → /admin/operations.
Selector change: button:has-text() / h2:has-text() → data-testid attributes.
"""
import pytest

pytest.skip(
    "Migrated to tests/browser/admin/test_repos.py::TestApplyPreset in M8 W7 "
    "(Astro SSR + FastAPI pure JSON API refactor).",
    allow_module_level=True,
)
