# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Structural smoke tests for the canonical /login page and /admin/login 301 redirect.

Business rule: /login is the canonical login page (contains the login form and
OAuthButtons); /admin/login is a 301 permanent redirect to /login that exists
solely for backward-compatibility with existing bookmarks and old links.

These tests guard against accidental regression back to the old layout where
/admin/login was canonical and /login was the alias.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

LOGIN_CANONICAL = REPO_ROOT / "site" / "src" / "pages" / "login.astro"
ADMIN_LOGIN = REPO_ROOT / "site" / "src" / "pages" / "admin" / "login.astro"
MIDDLEWARE = REPO_ROOT / "site" / "src" / "middleware.ts"


def test_canonical_login_file_exists() -> None:
    """site/src/pages/login.astro must exist as the canonical login page."""
    assert LOGIN_CANONICAL.exists(), f"Missing file: {LOGIN_CANONICAL}"


def test_canonical_login_is_real_page_not_redirect() -> None:
    """login.astro must be the real login form, NOT a redirect to /admin/login.

    Business rule: /login is the canonical URL. The old shim called
    `Astro.redirect('/admin/login?return=...')` — that line must not exist.
    The file may contain `Astro.redirect(returnTarget)` (already-authed bounce
    to /admin) and may mention /admin/login in comments, but must not include
    the old shim redirect call.
    """
    content = LOGIN_CANONICAL.read_text()
    # The old shim pattern: Astro.redirect pointing directly to /admin/login path.
    # This was the exact string in the shim: Astro.redirect(`/admin/login?return=
    assert "Astro.redirect(`/admin/login" not in content, (
        "login.astro must not redirect to /admin/login — it is the canonical page"
    )
    assert 'Astro.redirect("/admin/login' not in content, (
        "login.astro must not redirect to /admin/login — it is the canonical page"
    )
    # Must contain the login form (confirms it is a real page, not a shim)
    assert "login-form" in content, (
        "login.astro must contain the login form (data-testid=login-form)"
    )


def test_canonical_login_has_oauth_buttons() -> None:
    """login.astro must import and render OAuthButtons (parity with WI-2 signup)."""
    content = LOGIN_CANONICAL.read_text()
    assert "OAuthButtons" in content, (
        "login.astro must include OAuthButtons component"
    )


def test_canonical_login_has_cwe601_guard() -> None:
    """login.astro must apply the open-redirect guard (CWE-601) on ?return=.

    The guard rejects any ?return= value that is not a safe same-origin path —
    preventing phishing via crafted redirect URLs. The guard is centralised in
    site/src/lib/auth-landing.ts (resolveAuthLanding → isSafeInternalPath) and
    must be applied in BOTH the SSR frontmatter (already-authed bounce) and the
    <script> block (post-login redirect). This test asserts login.astro routes
    its ?return= value through resolveAuthLanding rather than building an
    unchecked redirect target.
    """
    content = LOGIN_CANONICAL.read_text()
    # The centralised guard must be imported and used (covers both the SSR
    # already-authed branch and the client-side post-login redirect).
    assert "resolveAuthLanding" in content, (
        "login.astro must route ?return= through resolveAuthLanding "
        "(centralised CWE-601 guard in lib/auth-landing.ts)"
    )
    # resolveAuthLanding is fed the raw ?return= query value.
    assert "searchParams.get('return')" in content or 'searchParams.get("return")' in content, (
        "login.astro must read the ?return= query param to pass to resolveAuthLanding"
    )


def test_admin_login_is_301_redirect() -> None:
    """admin/login.astro must be a 301 permanent redirect to /login.

    Business rule: /admin/login is legacy. Permanent redirect (301) transfers
    SEO equity to /login and signals to search engines that the URL has moved.
    """
    assert ADMIN_LOGIN.exists(), f"Missing file: {ADMIN_LOGIN}"
    content = ADMIN_LOGIN.read_text()
    assert "Astro.redirect" in content, "admin/login.astro must call Astro.redirect"
    assert "/login" in content, "admin/login.astro must redirect to /login"
    assert "301" in content, "admin/login.astro redirect must specify 301 (permanent)"
    # Must NOT be a real page (no login-form)
    assert "login-form" not in content, (
        "admin/login.astro must not contain a login form — it is only a redirect"
    )


def test_admin_login_preserves_query_params() -> None:
    """admin/login.astro must forward ?return= / ?error= / ?info= to /login.

    OAuth callbacks and the middleware both append error codes or return paths
    to /admin/login. The redirect must pass these through so users see the
    correct error banner on /login and land on the right page post-login.
    """
    content = ADMIN_LOGIN.read_text()
    # The redirect must propagate query parameters (qs / searchParams)
    assert "searchParams" in content or "qs" in content, (
        "admin/login.astro must forward query parameters to /login"
    )


def test_middleware_has_login_in_public_paths() -> None:
    """middleware.ts must include '/login' in _PUBLIC_PATHS so it is never auth-gated."""
    assert MIDDLEWARE.exists(), f"Missing file: {MIDDLEWARE}"
    content = MIDDLEWARE.read_text()
    assert "'/login'" in content, "middleware.ts _PUBLIC_PATHS must contain '/login'"


def test_middleware_redirects_unauth_to_login_not_admin_login() -> None:
    """middleware.ts unauth bounce must point to /login, not /admin/login.

    Bounce redirects in middleware must use the canonical URL /login directly,
    not /admin/login (which would add a redundant 301 hop on every unauthenticated
    request). Fewer hops = faster UX and simpler redirect chains.
    """
    content = MIDDLEWARE.read_text()
    # _redirectWithHeaders('/admin/login') must NOT appear (bounce-redirect form)
    assert "_redirectWithHeaders('/admin/login')" not in content, (
        "middleware.ts bounce redirects must use /login, not /admin/login"
    )
