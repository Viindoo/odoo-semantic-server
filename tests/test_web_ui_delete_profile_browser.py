# tests/test_web_ui_delete_profile_browser.py
"""MIGRATED — see tests/browser/admin/test_repos.py for current browser tests.

All Delete Profile browser tests have been migrated to
tests/browser/admin/test_repos.py::TestDeleteProfile as part of M8 W7.

URL change: /repos → /admin/repos.
Selector change: button[title='Delete profile'] → data-testid^="delete-profile-button-".
"""
import pytest

pytest.skip(
    "Migrated to tests/browser/admin/test_repos.py::TestDeleteProfile in M8 W7 "
    "(Astro SSR + FastAPI pure JSON API refactor).",
    allow_module_level=True,
)
