# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drift guard: nginx edge-baseline CSP must union every middleware CSP origin.

Prerendered Astro pages (landing `/`, `/benchmark`) are served by nginx, NOT the
Astro SSR middleware. So when the middleware grants an origin (e.g. the GA4
loader/beacon in `site/src/middleware.ts::_defaultCspDirectives`) but the nginx
baseline in `docs/deploy/nginx-m8.conf` does not, that origin is silently blocked
on exactly the high-traffic prerendered pages — GA would collect nothing on the
landing page. nginx-m8.conf itself documents the rule: "the edge baseline must be
the UNION of every origin/keyword the middleware ever grants."

This is a plain file-read unit test (no server / browser deps) so it runs in the
normal suite and catches the nginx↔middleware drift the browser CSP tests cannot
(those only exercise the SSR middleware path).
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NGINX_CONF = REPO_ROOT / "docs" / "deploy" / "nginx-m8.conf"
MIDDLEWARE_TS = REPO_ROOT / "site" / "src" / "middleware.ts"

# Origins the middleware grants in its DEFAULT (non-path-scoped) directives and
# that must therefore also appear in the nginx edge baseline.
GA_SCRIPT_SRC = "https://www.googletagmanager.com"
GA_CONNECT_SRC = ("https://www.google-analytics.com", "https://www.googletagmanager.com")


def _nginx_csp_line() -> str:
    text = NGINX_CONF.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "Content-Security-Policy" in line and "add_header" in line:
            return line
    raise AssertionError("No Content-Security-Policy add_header found in nginx-m8.conf")


def test_nginx_script_src_includes_ga_loader():
    assert GA_SCRIPT_SRC in _nginx_csp_line(), (
        "nginx-m8.conf CSP script-src must allow googletagmanager.com (GA4 loader); "
        "the edge baseline serves prerendered pages and must union every middleware grant."
    )


def test_nginx_connect_src_includes_ga_origins():
    csp = _nginx_csp_line()
    for origin in GA_CONNECT_SRC:
        assert origin in csp, (
            f"nginx-m8.conf CSP connect-src must allow {origin} (GA4 beacon/config) on "
            "prerendered pages."
        )


def test_nginx_still_unions_hcaptcha_origins():
    # Regression: the GA edit must not drop the pre-existing hCaptcha union.
    csp = _nginx_csp_line()
    assert "https://js.hcaptcha.com" in csp and "https://api.hcaptcha.com" in csp, (
        "nginx-m8.conf CSP must still carry the hCaptcha origins."
    )
    # hCaptcha asset subdomains rotate; the wildcard is required per their CSP docs.
    assert "https://hcaptcha.com" in csp, (
        "nginx-m8.conf CSP must include https://hcaptcha.com (hCaptcha wildcard)."
    )
    assert "https://*.hcaptcha.com" in csp, (
        "nginx-m8.conf CSP must include https://*.hcaptcha.com (hCaptcha wildcard)."
    )


def test_middleware_default_csp_grants_ga_origins():
    # Confirm the middleware side of the contract the nginx baseline mirrors.
    ts = MIDDLEWARE_TS.read_text(encoding="utf-8")
    assert GA_SCRIPT_SRC in ts and "https://www.google-analytics.com" in ts, (
        "middleware.ts must grant the GA origins the nginx baseline mirrors — if this "
        "fails the two sources have drifted."
    )


def test_middleware_grants_hcaptcha_wildcards():
    # Confirm middleware.ts contains the new hCaptcha wildcards added in PR #281.
    ts = MIDDLEWARE_TS.read_text(encoding="utf-8")
    assert "https://hcaptcha.com" in ts, (
        "middleware.ts must contain https://hcaptcha.com (hCaptcha apex wildcard)."
    )
    assert "https://*.hcaptcha.com" in ts, (
        "middleware.ts must contain https://*.hcaptcha.com (hCaptcha subdomain wildcard)."
    )


CADDYFILE = REPO_ROOT / "docs" / "deploy" / "Caddyfile.example"


def _caddyfile_csp_line() -> str:
    text = CADDYFILE.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "Content-Security-Policy" in line:
            return line
    raise AssertionError("No Content-Security-Policy found in Caddyfile.example")


def test_caddyfile_unions_ga_origins():
    csp = _caddyfile_csp_line()
    assert "https://www.google-analytics.com" in csp, (
        "Caddyfile.example CSP must include https://www.google-analytics.com (GA4 beacon) — "
        "must stay in parity with nginx-m8.conf."
    )
    assert "https://www.googletagmanager.com" in csp, (
        "Caddyfile.example CSP must include https://www.googletagmanager.com (GA4 loader) — "
        "must stay in parity with nginx-m8.conf."
    )


def test_caddyfile_unions_hcaptcha_origins():
    csp = _caddyfile_csp_line()
    assert "https://hcaptcha.com" in csp, (
        "Caddyfile.example CSP must include https://hcaptcha.com (hCaptcha apex wildcard)."
    )
    assert "https://*.hcaptcha.com" in csp, (
        "Caddyfile.example CSP must include https://*.hcaptcha.com (hCaptcha subdomain wildcard)."
    )
