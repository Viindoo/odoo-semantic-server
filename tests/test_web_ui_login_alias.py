# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Structural smoke tests for W1D-3: /login canonical alias → /admin/login.

Astro pages are not easily unit-tested from pytest (they require a running
Node/Vite dev server or a browser). These tests verify the presence and
content of the relevant source files to guard against accidental deletion
and to document the intended behaviour in the test suite.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

LOGIN_ALIAS = REPO_ROOT / "site" / "src" / "pages" / "login.astro"
MIDDLEWARE = REPO_ROOT / "site" / "src" / "middleware.ts"


def test_login_alias_file_exists() -> None:
    """site/src/pages/login.astro must exist."""
    assert LOGIN_ALIAS.exists(), f"Missing file: {LOGIN_ALIAS}"


def test_login_alias_contains_redirect() -> None:
    """login.astro must call Astro.redirect pointing at /admin/login."""
    content = LOGIN_ALIAS.read_text()
    assert "Astro.redirect" in content, "login.astro must call Astro.redirect"
    assert "/admin/login" in content, "login.astro must redirect to /admin/login"


def test_login_alias_is_302() -> None:
    """The redirect must be 302 (temporary) so search engines don't lock in the alias."""
    content = LOGIN_ALIAS.read_text()
    assert "302" in content, "login.astro redirect must specify 302 status"


def test_login_alias_preserves_return_param() -> None:
    """login.astro must forward ?return= / ?next= to the canonical login page."""
    content = LOGIN_ALIAS.read_text()
    assert "return" in content, "login.astro must handle ?return= param"
    assert "next" in content, "login.astro must handle ?next= param"


def test_middleware_has_login_in_public_paths() -> None:
    """middleware.ts must include '/login' in _PUBLIC_PATHS so it is never auth-gated."""
    assert MIDDLEWARE.exists(), f"Missing file: {MIDDLEWARE}"
    content = MIDDLEWARE.read_text()
    assert "'/login'" in content, "middleware.ts _PUBLIC_PATHS must contain '/login'"
