# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_web_ui_browser.py
"""MIGRATED — see tests/browser/admin/ for the current browser test suite.

This file is retained as a reference stub. All tests have been migrated to
tests/browser/admin/test_dashboard.py, tests/browser/admin/test_repos.py,
tests/browser/admin/test_api_keys.py, and tests/browser/admin/test_ssh_keys.py
as part of M8 W7 (Astro + FastAPI refactor).

URL changes: /repos → /admin/repos, / → /admin/, /api-keys → /admin/api-keys, etc.
Selector changes: .badge-ok/.stat .number → data-testid attributes.
"""
import pytest

pytest.skip(
    "Migrated to tests/browser/admin/test_*.py in M8 W7 "
    "(Astro SSR + FastAPI pure JSON API refactor). "
    "See tests/browser/admin/test_dashboard.py, test_repos.py, "
    "test_api_keys.py, test_ssh_keys.py.",
    allow_module_level=True,
)
