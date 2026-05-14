# tests/test_web_ui_index_core_browser.py
"""MIGRATED — see tests/browser/admin/test_repos.py for current browser tests.

All Index Core browser tests have been migrated to
tests/browser/admin/test_repos.py::TestIndexCore as part of M8 W7.

URL change: /operations → /admin/operations.
Selector change: h2:has-text / button:has-text / .alert → data-testid attributes.
"""
import pytest

pytest.skip(
    "Migrated to tests/browser/admin/test_repos.py::TestIndexCore in M8 W7 "
    "(Astro SSR + FastAPI pure JSON API refactor).",
    allow_module_level=True,
)
