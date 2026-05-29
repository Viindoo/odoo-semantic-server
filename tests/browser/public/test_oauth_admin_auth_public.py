# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/browser/public/test_oauth_admin_auth_public.py
"""Anonymous accessibility of /admin/auth/* OAuth routes.

Business invariant:
  Anonymous users MUST be able to initiate the OAuth flow at
  /admin/auth/{provider} and return through /admin/auth/callback/{provider}
  WITHOUT being intercepted by the Astro admin middleware. An intercept that
  redirects the user to the login page before the OAuth handler runs makes the
  entire signup/sign-in flow unreachable.

Implementation-agnostic:
  This test does NOT care how the middleware expresses the carve-out (Set
  membership, prefix check, regex, route metadata, dedicated mount, …).
  It only asserts the externally observable behaviour: an anonymous request
  to an /admin/auth/* path is not bounced to the login page
  (either /login or /admin/login — both are login surfaces).

What it tolerates:
  - 200 (handler returned a page)
  - 302 to the upstream provider (accounts.google.com, github.com)
  - 4xx (handler ran but parameters missing or state cookie absent)
  - 5xx (handler ran but provider creds unconfigured at build time, e.g. CI)

What it rejects:
  - Any 3xx whose Location contains /login or /admin/login
    (WI-3: /admin/login is now a 301 redirect to /login; both are login surfaces)

History:
  PR #208 fixed a regression where the middleware sent /admin/auth/* through
  the session-required path. Anonymous OAuth users were 302'd to /admin/login,
  silently breaking OAuth signup. The fix (and any future refactor) must
  preserve the externally observable behaviour pinned by this test.
  WI-3 renamed canonical login to /login; /admin/login now 301-redirects there.
"""

import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.browser


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Block urllib's auto-follow so we can inspect the FIRST response."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _get_first_response(url: str) -> tuple[int, str]:
    """GET url, return (status, Location header) — no redirect following."""
    opener = urllib.request.build_opener(_NoRedirectHandler)
    try:
        resp = opener.open(url, timeout=5)
        return resp.status, resp.headers.get("Location", "") or ""
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location", "") or ""


_OAUTH_INIT_PATHS = ["/admin/auth/google", "/admin/auth/github"]
_OAUTH_CALLBACK_PATHS = [
    "/admin/auth/callback/google",
    "/admin/auth/callback/github",
]


def _is_login_bounce(location: str) -> bool:
    """Return True if Location looks like a middleware login-bounce redirect.

    Both /login and /admin/login are login surfaces. A redirect to either
    (with no OAuth-related query params) signals middleware interception.
    We intentionally exclude error-redirect cases from OAuth callbacks
    (e.g. /login?error=no_email) because those are legitimate handler
    responses — not middleware interceptions. The key signal is that the
    redirect happens at all to a login path when the middleware should
    have passed through to the handler.
    """
    # A bare login path (no ?error=) would be a middleware bounce.
    # /login?error=... is an OAuth handler response (legitimate).
    return (location.rstrip("/") == "/login" or
            location.rstrip("/") == "/admin/login" or
            location == "/login" or
            location == "/admin/login")


class TestOAuthAnonymousAccess:
    """Anonymous GET to /admin/auth/* must not be bounced to the login page by middleware."""

    @pytest.mark.parametrize("path", _OAUTH_INIT_PATHS)
    def test_init_route_not_intercepted_by_admin_middleware(
        self, astro_server, path
    ):
        """Initiation route reaches the OAuth handler as anonymous user."""
        code, location = _get_first_response(f"{astro_server}{path}")
        if 300 <= code < 400:
            assert not _is_login_bounce(location), (
                f"Anon GET {path} was redirected to {location!r}. "
                "Admin middleware is intercepting an OAuth initiation route; "
                "OAuth flow is unreachable."
            )

    @pytest.mark.parametrize("path", _OAUTH_CALLBACK_PATHS)
    def test_callback_route_not_intercepted_by_admin_middleware(
        self, astro_server, path
    ):
        """Callback route reaches the OAuth handler as anonymous user.

        Without a state cookie + code param the callback will surface some
        validation error (typically 4xx). What matters is that the middleware
        does NOT bounce the user back to the login page before the handler runs.
        """
        code, location = _get_first_response(f"{astro_server}{path}")
        if 300 <= code < 400:
            assert not _is_login_bounce(location), (
                f"Anon GET {path} was redirected to {location!r}. "
                "Admin middleware is intercepting an OAuth callback route; "
                "users returning from the provider would be sent to login "
                "instead of having their state + PKCE checked."
            )
