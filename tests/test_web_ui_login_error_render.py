# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structural tests for /login.astro (canonical login page) error/info banner rendering.

/login.astro is the canonical login page (WI-3 URL rename). /admin/login.astro
is now a 301 redirect; error banner logic lives exclusively in login.astro.

These tests verify that:
1. The canonical login page exists and contains all required whitelist keys.
2. The XSS guard is in place — user-supplied error/info values are NEVER
   rendered verbatim; only whitelisted strings pass through.
3. The aria-live attributes are present for screen-reader accessibility.
"""

from pathlib import Path

# Absolute path — avoids cwd dependency in pytest invocations.
_LOGIN_ASTRO = Path(__file__).parents[1] / "site" / "src" / "pages" / "login.astro"


def _content() -> str:
    return _LOGIN_ASTRO.read_text(encoding="utf-8")


def test_login_astro_exists() -> None:
    assert _LOGIN_ASTRO.exists(), f"Expected canonical login page at {_LOGIN_ASTRO}"


# ---------------------------------------------------------------------------
# Whitelist presence — all required error codes must be keys in the mapping
# ---------------------------------------------------------------------------

def test_whitelist_error_oauth_failed() -> None:
    assert "oauth_failed" in _content()


def test_whitelist_error_signup_disabled() -> None:
    assert "signup_disabled" in _content()


def test_whitelist_error_email_unverified() -> None:
    assert "email_unverified" in _content()


def test_whitelist_info_password_reset_sent() -> None:
    assert "password_reset_sent" in _content()


# ---------------------------------------------------------------------------
# Template variable presence — both rendering variables must be used
# ---------------------------------------------------------------------------

def test_template_uses_errorMsg() -> None:  # noqa: N802  (camelCase matches Astro convention)
    assert "errorMsg" in _content()


def test_template_uses_infoMsg() -> None:  # noqa: N802
    assert "infoMsg" in _content()


# ---------------------------------------------------------------------------
# Accessibility — aria-live attributes must be present
# ---------------------------------------------------------------------------

def test_aria_live_present() -> None:
    assert "aria-live" in _content()


# ---------------------------------------------------------------------------
# XSS guard — raw user input must NOT be rendered directly.
#
# The file must NOT contain template expressions that echo the raw URL
# parameter (e.g. `{error}` or `${error}` where `error` is the raw param).
# The whitelist variables are named `errorMsg` / `infoMsg` — those ARE fine.
# What we forbid is a pattern where `_rawError` / `_rawInfo` (the unfiltered
# param values) appear inside a JSX-style expression brace in the template
# section (after the closing `---`).
# ---------------------------------------------------------------------------

def test_xss_guard_raw_error_not_rendered() -> None:
    """_rawError must not appear inside a template expression {_rawError}."""
    content = _content()
    # Split at closing frontmatter delimiter — only check the HTML template part
    parts = content.split("---", 2)
    assert len(parts) >= 3, "Could not find closing frontmatter delimiter"
    template_section = parts[2]
    # The raw variable must not be rendered directly in the template
    assert "{_rawError}" not in template_section, (
        "_rawError is rendered verbatim in the template — XSS risk"
    )
    assert "{_rawInfo}" not in template_section, (
        "_rawInfo is rendered verbatim in the template — XSS risk"
    )


def test_xss_guard_no_set_html_on_user_input() -> None:
    """set:html must not be used on any error/info variable."""
    content = _content()
    # set:html={errorMsg} would be safe (it's whitelisted), but raw param must not appear
    assert "set:html={_rawError}" not in content
    assert "set:html={_rawInfo}" not in content
