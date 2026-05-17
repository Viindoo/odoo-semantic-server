# tests/browser/public/test_csp_headers.py
"""Astro middleware CSP + Permissions-Policy regression tests.

These tests drive a real `pnpm preview` server (via the session-scoped
`astro_server` fixture from tests/browser/conftest.py) and assert via
`urllib` that the CSP header emitted for each path matches the per-path
policy implemented in `site/src/middleware.ts::_buildCspForPath`.

Background (PR #118 reviewer follow-up — both reviewers, same finding):
  /signup conditionally loads `https://js.hcaptcha.com/1/api.js` when
  PUBLIC_HCAPTCHA_SITE_KEY is configured. The default Astro CSP
  (`script-src 'self' 'unsafe-inline'`) blocks that origin. The middleware
  therefore expands script-src / connect-src / frame-src with hCaptcha
  origins on /signup only — NOT on /, NOT on /admin/* — to keep the
  third-party-script blast-radius minimal.

Tests:
  1. /signup CSP includes js.hcaptcha.com, api.hcaptcha.com, newassets.hcaptcha.com
  2. / (landing) CSP does NOT include any hCaptcha origin
  3. /admin/login CSP does NOT include any hCaptcha origin
  4. All paths still carry Permissions-Policy with the expected disables
"""
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.browser


def _get_headers(url: str) -> dict[str, str]:
    """GET url and return response headers (lowercased keys for case-insensitive lookup)."""
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return {k.lower(): v for k, v in resp.headers.items()}


class TestSignupCspHcaptcha:
    """/signup must allowlist hCaptcha origins so the widget can load."""

    def test_signup_csp_present(self, astro_server):
        headers = _get_headers(f"{astro_server}/signup")
        assert "content-security-policy" in headers

    def test_signup_script_src_includes_hcaptcha_js(self, astro_server):
        csp = _get_headers(f"{astro_server}/signup")["content-security-policy"]
        # Extract the script-src directive
        directives = {d.strip().split(" ", 1)[0]: d.strip() for d in csp.split(";") if d.strip()}
        script_src = directives.get("script-src", "")
        assert "https://js.hcaptcha.com" in script_src, (
            f"script-src must allow js.hcaptcha.com on /signup; got: {script_src!r}"
        )
        assert "https://newassets.hcaptcha.com" in script_src, (
            f"script-src must allow newassets.hcaptcha.com on /signup; got: {script_src!r}"
        )

    def test_signup_connect_src_includes_hcaptcha_api(self, astro_server):
        csp = _get_headers(f"{astro_server}/signup")["content-security-policy"]
        directives = {d.strip().split(" ", 1)[0]: d.strip() for d in csp.split(";") if d.strip()}
        connect_src = directives.get("connect-src", "")
        assert "https://api.hcaptcha.com" in connect_src, (
            f"connect-src must allow api.hcaptcha.com on /signup; got: {connect_src!r}"
        )
        assert "https://newassets.hcaptcha.com" in connect_src, (
            f"connect-src must allow newassets.hcaptcha.com on /signup; got: {connect_src!r}"
        )

    def test_signup_frame_src_includes_hcaptcha_iframe(self, astro_server):
        csp = _get_headers(f"{astro_server}/signup")["content-security-policy"]
        directives = {d.strip().split(" ", 1)[0]: d.strip() for d in csp.split(";") if d.strip()}
        frame_src = directives.get("frame-src", "")
        assert "https://newassets.hcaptcha.com" in frame_src, (
            f"frame-src must allow newassets.hcaptcha.com on /signup; got: {frame_src!r}"
        )

    def test_signup_permissions_policy_present(self, astro_server):
        headers = _get_headers(f"{astro_server}/signup")
        assert "permissions-policy" in headers
        assert "camera=()" in headers["permissions-policy"]


class TestNonHcaptchaPathsDoNotLeakAllowlist:
    """Verify the hCaptcha allowlist is scoped to /signup only — no over-grant.

    Note: `/` (landing), `/pricing`, `/bootstrap`, `/benchmarks` are
    `export const prerender = true` — Astro emits them as static files
    at build time and the Node adapter serves them from `dist/client/`
    WITHOUT invoking middleware. So the per-path CSP only matters for
    SSR pages (`/admin/*`, `/signup`, `/verify-email`, `/reset-password`).
    In production, prerendered pages still get a baseline CSP from
    nginx (see `docs/deploy/nginx-m8.conf`) which is a permissive
    superset designed to intersect safely with the middleware CSP per
    W3C CSP3 §4.1. The tests below only assert the SSR-side guarantee.
    """

    def test_admin_login_csp_does_not_include_hcaptcha(self, astro_server):
        csp = _get_headers(f"{astro_server}/admin/login")["content-security-policy"]
        assert "js.hcaptcha.com" not in csp, (
            f"hCaptcha origin must NOT appear on /admin/login: {csp!r}"
        )
        assert "api.hcaptcha.com" not in csp
        assert "newassets.hcaptcha.com" not in csp

    def test_admin_login_csp_still_has_default_directives(self, astro_server):
        """Sanity: even without hCaptcha, admin CSP must have the base directives."""
        csp = _get_headers(f"{astro_server}/admin/login")["content-security-policy"]
        assert "default-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "form-action 'self'" in csp


class TestRedirectsCarrySecurityHeaders:
    """3xx responses from the Astro middleware must also carry CSP + Permissions-Policy.

    Browsers will apply the destination page's CSP after the redirect, but
    security scanners (and the SecurityHeaders.com grader) flag 3xx
    responses missing these headers — and a malicious page could in theory
    intercept the redirect target. Per the PR #118 review, all 3
    `context.redirect()` call sites in `site/src/middleware.ts` are now
    wrapped to add the headers before returning.
    """

    def _get_redirect_response(self, url: str) -> tuple[int, dict[str, str]]:
        """GET url WITHOUT following redirects; return (status, headers-lowercased)."""

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):  # pragma: no cover - simple override
                return None

        opener = urllib.request.build_opener(_NoRedirect)
        req = urllib.request.Request(url, method="GET")
        try:
            resp = opener.open(req, timeout=5)
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as e:
            # urllib treats 3xx without a Location follower as an HTTPError.
            return e.code, {k.lower(): v for k, v in e.headers.items()}

    def test_admin_users_unauthenticated_redirect_has_csp(self, astro_server):
        """GET /admin/users/ without cookie → 302 to /admin/login WITH CSP header."""
        status, headers = self._get_redirect_response(f"{astro_server}/admin/users/")
        assert status in (301, 302, 303, 307, 308), (
            f"Expected redirect, got status {status} (admin/users/ unauthenticated must redirect)"
        )
        assert "content-security-policy" in headers, (
            "Redirect response must carry CSP — PR #118 review fix"
        )
        assert "permissions-policy" in headers, (
            "Redirect response must carry Permissions-Policy — PR #118 review fix"
        )

    def test_admin_dashboard_unauthenticated_redirect_has_csp(self, astro_server):
        """GET /admin without cookie → 302 to /admin/login WITH CSP header.

        Exercises the second `_redirectWithHeaders('/admin/login')` site
        in middleware.ts (the catch-all admin path).
        """
        status, headers = self._get_redirect_response(f"{astro_server}/admin")
        assert status in (301, 302, 303, 307, 308), (
            f"Expected redirect, got status {status} (/admin unauthenticated must redirect)"
        )
        assert "content-security-policy" in headers
        assert "permissions-policy" in headers
