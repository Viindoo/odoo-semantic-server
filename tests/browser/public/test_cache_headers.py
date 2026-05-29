# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/browser/public/test_cache_headers.py
"""Cache-Control / Vary header regression tests (WI-9b — middleware noStore).

These tests drive a real `pnpm preview` server (via the session-scoped
`astro_server` fixture from tests/browser/conftest.py) and assert via
`urllib` that:

  1. SSR pages that the middleware processes with noStore=True carry the
     expected Cache-Control: no-store, must-revalidate and Vary: Cookie
     headers — preventing bfcache from storing authenticated page snapshots.

  2. Prerendered/public pages (/, /pricing if present) do NOT get
     Cache-Control: no-store — they remain cacheable because they contain no
     session-specific content and the middleware explicitly passes noStore=False
     for context.isPrerendered responses (see site/src/middleware.ts L197-204).

Background (WI-3 follow-up):
  Without Cache-Control: no-store on SSR responses, browsers (Chrome, Safari)
  store /admin/* page snapshots in bfcache memory. A user who switches Google
  accounts and presses Back instantly sees the previous session's admin
  dashboard without the server ever receiving a request — the session guard
  never runs. Pragma: no-cache and Vary: Cookie add HTTP/1.0 compat and CDN
  partition safety respectively.

Test structure:
  TestSsrResponsesCacheControl  — SSR auth pages must have no-store + Vary
  TestPrerenderedPagesAreCacheable — public prerendered pages must NOT have no-store
  TestRedirectsCacheControl     — 3xx responses (unauthenticated) must carry no-store

Note on prerendered detection:
  The Astro @astrojs/node adapter serves prerendered pages (export const
  prerender = true) directly from dist/client/ as static files, BEFORE the
  middleware runs. The test therefore cannot rely on middleware applying
  noStore=false — it simply verifies that static pages are not forced into
  no-store territory, matching the design intent described in the middleware
  comment at site/src/middleware.ts L87-109.

  Known prerendered pages (as of this branch): / (landing), /pricing,
  /bootstrap, /benchmarks. If the test for / or /pricing fails because the
  node adapter does NOT add Cache-Control headers at all (i.e., the header is
  absent rather than set to no-store), the assertion still passes — absent
  means not-no-store. We assert `no-store NOT IN header` rather than
  `max-age IN header` to stay resilient to different adapter defaults.
"""
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.browser


# ---------------------------------------------------------------------------
# Helpers (same pattern as test_csp_headers.py)
# ---------------------------------------------------------------------------

def _get_headers(url: str) -> dict[str, str]:
    """GET url (following redirects) and return response headers (lowercased keys)."""
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return {k.lower(): v for k, v in resp.headers.items()}


def _get_redirect_response(url: str) -> tuple[int, dict[str, str]]:
    """GET url WITHOUT following redirects; return (status, headers-lowercased).

    Mirrors the helper in test_csp_headers.py::TestRedirectsCarrySecurityHeaders.
    """

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):  # pragma: no cover
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, method="GET")
    try:
        resp = opener.open(req, timeout=5)
        return resp.status, {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}


# ---------------------------------------------------------------------------
# SSR auth pages — must carry Cache-Control: no-store + Vary: Cookie
# ---------------------------------------------------------------------------

class TestSsrResponsesCacheControl:
    """SSR pages handled with noStore=True must emit the full no-store header set.

    /login, /signup, /verify-email, /forgot-password are public SSR paths
    processed by the PUBLIC_PATHS branch in middleware.ts which also calls
    _addSecurityHeaders(response, path) with the default noStore=True.

    /admin/* unauthenticated requests redirect to /login; the redirect itself
    is verified in TestRedirectsCacheControl below.  We use /login here
    because it responds 200 without credentials.
    """

    def test_login_has_no_store(self, astro_server: str) -> None:
        """GET /login → Cache-Control must contain no-store."""
        headers = _get_headers(f"{astro_server}/login")
        cache_control = headers.get("cache-control", "")
        assert "no-store" in cache_control, (
            f"SSR /login must set Cache-Control: no-store; got: {cache_control!r}"
        )

    def test_login_has_must_revalidate(self, astro_server: str) -> None:
        """GET /login → Cache-Control must contain must-revalidate."""
        headers = _get_headers(f"{astro_server}/login")
        cache_control = headers.get("cache-control", "")
        assert "must-revalidate" in cache_control, (
            f"SSR /login must set Cache-Control: must-revalidate; got: {cache_control!r}"
        )

    def test_login_has_vary_cookie(self, astro_server: str) -> None:
        """GET /login → Vary must contain Cookie (prevents CDN cross-user poisoning)."""
        headers = _get_headers(f"{astro_server}/login")
        vary = headers.get("vary", "")
        assert "Cookie" in vary or "cookie" in vary, (
            f"SSR /login must set Vary: Cookie; got: {vary!r}"
        )

    def test_login_has_pragma_no_cache(self, astro_server: str) -> None:
        """GET /login → Pragma: no-cache (HTTP/1.0 compat shim)."""
        headers = _get_headers(f"{astro_server}/login")
        pragma = headers.get("pragma", "")
        assert "no-cache" in pragma, (
            f"SSR /login must set Pragma: no-cache; got: {pragma!r}"
        )

    def test_signup_has_no_store(self, astro_server: str) -> None:
        """GET /signup → Cache-Control must contain no-store."""
        headers = _get_headers(f"{astro_server}/signup")
        cache_control = headers.get("cache-control", "")
        assert "no-store" in cache_control, (
            f"SSR /signup must set Cache-Control: no-store; got: {cache_control!r}"
        )

    def test_signup_has_vary_cookie(self, astro_server: str) -> None:
        """GET /signup → Vary must contain Cookie."""
        headers = _get_headers(f"{astro_server}/signup")
        vary = headers.get("vary", "")
        assert "Cookie" in vary or "cookie" in vary, (
            f"SSR /signup must set Vary: Cookie; got: {vary!r}"
        )


# ---------------------------------------------------------------------------
# Prerendered / public pages — must NOT be forced to no-store
# ---------------------------------------------------------------------------

class TestPrerenderedPagesAreCacheable:
    """Static / prerendered pages must NOT receive Cache-Control: no-store.

    The Astro middleware sets noStore=False for context.isPrerendered responses
    (site/src/middleware.ts L197-204). Prerendered pages are built to static
    HTML and contain no session-specific data — forcing them to no-store wastes
    CDN/browser caching for public marketing content.

    We assert `no-store NOT IN cache-control` — if the header is absent
    entirely (the node adapter may serve static files without setting
    Cache-Control at all), the assertion still passes because '' does not
    contain 'no-store'.

    Known prerendered pages (export const prerender = true): /
    """

    def test_landing_root_does_not_have_no_store(self, astro_server: str) -> None:
        """GET / (prerendered landing page) must NOT carry Cache-Control: no-store."""
        headers = _get_headers(f"{astro_server}/")
        cache_control = headers.get("cache-control", "")
        assert "no-store" not in cache_control, (
            f"Prerendered / must NOT set Cache-Control: no-store; got: {cache_control!r}. "
            "Check middleware.ts: isPrerendered branch must call "
            "_addSecurityHeaders with noStore=false."
        )


# ---------------------------------------------------------------------------
# 3xx redirect responses — must carry no-store headers
# ---------------------------------------------------------------------------

class TestRedirectsCacheControl:
    """Redirect responses emitted by _redirectWithHeaders() must carry no-store.

    _redirectWithHeaders() calls _addSecurityHeaders(r, path) with the default
    noStore=True, so every 3xx redirect produced by middleware also prevents
    bfcache from storing that in-flight navigation.
    """

    def test_admin_unauthenticated_redirect_has_no_store(self, astro_server: str) -> None:
        """GET /admin without auth cookie → 3xx redirect must carry Cache-Control: no-store."""
        status, headers = _get_redirect_response(f"{astro_server}/admin")
        assert status in (301, 302, 303, 307, 308), (
            f"Expected redirect from /admin (unauthenticated), got status {status}"
        )
        cache_control = headers.get("cache-control", "")
        assert "no-store" in cache_control, (
            f"Redirect from /admin must carry Cache-Control: no-store; got: {cache_control!r}"
        )

    def test_admin_unauthenticated_redirect_has_vary_cookie(self, astro_server: str) -> None:
        """GET /admin without auth cookie → 3xx redirect must carry Vary: Cookie."""
        status, headers = _get_redirect_response(f"{astro_server}/admin")
        assert status in (301, 302, 303, 307, 308), (
            f"Expected redirect from /admin (unauthenticated), got status {status}"
        )
        vary = headers.get("vary", "")
        assert "Cookie" in vary or "cookie" in vary, (
            f"Redirect from /admin must carry Vary: Cookie; got: {vary!r}"
        )

    def test_admin_users_unauthenticated_redirect_has_no_store(self, astro_server: str) -> None:
        """GET /admin/users without auth cookie → 3xx redirect carries no-store."""
        status, headers = _get_redirect_response(f"{astro_server}/admin/users")
        assert status in (301, 302, 303, 307, 308), (
            f"Expected redirect from /admin/users (unauthenticated), got status {status}"
        )
        cache_control = headers.get("cache-control", "")
        assert "no-store" in cache_control, (
            f"Redirect from /admin/users must carry Cache-Control: no-store; got: {cache_control!r}"
        )


# ---------------------------------------------------------------------------
# Role-landing redirect assertions (unauthenticated redirects to /login)
# ---------------------------------------------------------------------------

class TestRoleLandingRedirects:
    """Assert the unauthenticated → /login redirect path (observable without login).

    Full role-aware landing (admin → /admin/, customer → /account/api-keys) is
    implemented in resolveAuthLanding() (client-side lib) and in the OAuth
    callbacks / login.astro (SSR), all of which require a live authenticated
    session and a real OAuth flow that this harness cannot drive headlessly.

    The unit tests in site/src/lib/__tests__/auth-landing.test.ts provide
    complete deterministic coverage of resolveAuthLanding() and isSafeInternalPath().

    What we CAN assert here without auth: unauthenticated /account/* and
    /admin/* hits are redirected to /login (not to /admin/login which was
    the prior canonical URL), confirming the WI-3 middleware change is live.
    """

    def test_unauthenticated_account_api_keys_redirects_to_login(
        self, astro_server: str
    ) -> None:
        """GET /account/api-keys (no auth cookie) → 3xx redirect to /login."""
        status, headers = _get_redirect_response(f"{astro_server}/account/api-keys")
        assert status in (301, 302, 303, 307, 308), (
            f"Expected redirect from /account/api-keys (unauthenticated), got {status}"
        )
        location = headers.get("location", "")
        assert "/login" in location, (
            f"Unauthenticated /account/api-keys must redirect to /login; got Location: {location!r}"
        )

    def test_unauthenticated_admin_redirects_to_login(self, astro_server: str) -> None:
        """GET /admin (no auth cookie) → 3xx redirect to /login (not /admin/login)."""
        status, headers = _get_redirect_response(f"{astro_server}/admin")
        assert status in (301, 302, 303, 307, 308), (
            f"Expected redirect from /admin (unauthenticated), got {status}"
        )
        location = headers.get("location", "")
        assert "/login" in location, (
            f"Unauthenticated /admin must redirect to /login; got Location: {location!r}"
        )
        # Confirm it is NOT the old /admin/login path — /login is now canonical (WI-3).
        assert location.rstrip("/") != "/admin/login", (
            f"Redirect must go to /login (canonical), not /admin/login; got: {location!r}"
        )

    # TODO: Full role-aware landing assertions (admin login → /admin/,
    # customer login → /account/api-keys) require a live FastAPI backend
    # with test credentials + session cookie injection, which the current
    # harness only provides for admin logins via tests/browser/admin/conftest.py
    # (admin_session_cookie fixture).  To add these:
    #   1. Create a non-admin test user via POST /api/admin/users in a fixture.
    #   2. POST /api/auth/login → capture Set-Cookie.
    #   3. GET /admin/logout → assert redirect destination is /account/api-keys.
    # See tests/browser/admin/conftest.py for the pattern.
