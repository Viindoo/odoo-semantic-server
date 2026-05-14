# tests/test_web_ui_delete_repo_browser.py
"""MIGRATED — see tests/browser/admin/test_repos.py for current browser tests.

All Delete Repo browser tests have been migrated to
tests/browser/admin/test_repos.py::TestDeleteRepo as part of M8 W7.

URL change: /repos → /admin/repos.
Selector change: button[title='Delete repo'] → data-testid^="delete-repo-button-".
"""
import pytest

pytest.skip(
    "Migrated to tests/browser/admin/test_repos.py::TestDeleteRepo in M8 W7 "
    "(Astro SSR + FastAPI pure JSON API refactor).",
    allow_module_level=True,
)
