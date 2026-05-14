# tests/test_web_ui_index_all_browser.py
"""MIGRATED — see tests/browser/admin/test_repos.py for current browser tests.

All Index All browser tests have been migrated to
tests/browser/admin/test_repos.py::TestIndexAll as part of M8 W7.

URL change: /repos → /admin/repos.
Selector change: h2:has-text('Bulk Operations') / button:has-text → data-testid="index-all-button".
"""
import pytest

pytest.skip(
    "Migrated to tests/browser/admin/test_repos.py::TestIndexAll in M8 W7 "
    "(Astro SSR + FastAPI pure JSON API refactor).",
    allow_module_level=True,
)
