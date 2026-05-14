# tests/test_web_ui_index_options_browser.py
"""MIGRATED — see tests/browser/admin/test_repos.py for current browser tests.

All Index Options browser tests have been migrated to
tests/browser/admin/test_repos.py::TestIndexOptions as part of M8 W7.

URL change: /repos → /admin/repos.
Selector change: button:has-text('Index') → data-testid^="index-repo-button-".
"""
import pytest

pytest.skip(
    "Migrated to tests/browser/admin/test_repos.py::TestIndexOptions in M8 W7 "
    "(Astro SSR + FastAPI pure JSON API refactor).",
    allow_module_level=True,
)
