# tests/test_web_ui_reset_embed_browser.py
"""MIGRATED — see tests/browser/admin/test_repos.py for current browser tests.

All Reset Embed browser tests have been migrated to
tests/browser/admin/test_repos.py::TestResetEmbed as part of M8 W7.

URL change: /repos → /admin/repos.
Selector change: button[title='Reset embed state and re-index'] → data-testid^="reset-embed-button-".
"""
import pytest

pytest.skip(
    "Migrated to tests/browser/admin/test_repos.py::TestResetEmbed in M8 W7 "
    "(Astro SSR + FastAPI pure JSON API refactor).",
    allow_module_level=True,
)
