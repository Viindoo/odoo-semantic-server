# tests/test_web_ui_seed_patterns_browser.py
"""MIGRATED — see tests/browser/admin/test_repos.py for current browser tests.

All Seed Patterns browser tests have been migrated to
tests/browser/admin/test_repos.py::TestSeedPatterns as part of M8 W7.

URL change: /operations → /admin/operations.
Selector change: button:has-text / h2:has-text / .alert → data-testid attributes.
"""
import pytest

pytest.skip(
    "Migrated to tests/browser/admin/test_repos.py::TestSeedPatterns in M8 W7 "
    "(Astro SSR + FastAPI pure JSON API refactor).",
    allow_module_level=True,
)
