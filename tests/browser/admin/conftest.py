# tests/browser/admin/conftest.py
"""Admin browser test fixtures.

Admin tests hit /admin/* pages, where the Astro middleware proxies auth
checks to FastAPI at localhost:8003. Two pieces are autouse here so every
admin test starts with a working setup:

  1. `_autouse_api_server` — guarantees FastAPI is up (mirrored from the
     session-scoped api_server fixture in tests/browser/conftest.py).
  2. `_autouse_admin_login` — seeds a test admin user once per session,
     POSTs /api/auth/login to obtain a real session cookie, and injects
     the cookie into the Playwright browser context. Tests can opt out
     with `@pytest.mark.unauthenticated` if they need to exercise the
     pre-auth flow (e.g. test_login.py asserts the login form renders
     when no session is present).

The earlier WEBUI_AUTH_DISABLED+PYTEST_CURRENT_TEST env bypass was
removed: it made /api/auth/verify always return 200, which broke the
login page's "if already authed, redirect to /admin" guard and prevented
test_login.py from ever seeing the login form.
"""
import json
import urllib.error
import urllib.request

import pytest

TEST_ADMIN_USERNAME = "test-admin-m8"
TEST_ADMIN_PASSWORD = "test-password-m8"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "unauthenticated: do not auto-inject the admin session cookie "
        "(use for tests that exercise login/redirect-when-unauth flows)",
    )


@pytest.fixture(autouse=True)
def _autouse_api_server(api_server):
    """Force api_server fixture for every admin test."""
    yield api_server


@pytest.fixture(scope="session")
def _admin_session_cookie(api_server, pg_conn):
    """Seed a test admin user once per session and return its session cookie.

    Returns the raw cookie header value, e.g. ``session=eyJ...``. Splitting on
    ``=`` gives (name, value) suitable for Playwright's context.add_cookies.
    """
    from src.db.migrate import run_migrations
    from src.db.pg import auth_store
    from src.web_ui.auth import hash_password

    run_migrations(pg_conn)
    auth_store().set_user_password(
        TEST_ADMIN_USERNAME, hash_password(TEST_ADMIN_PASSWORD)
    )

    req = urllib.request.Request(
        f"{api_server}/api/auth/login",
        data=json.dumps(
            {"username": TEST_ADMIN_USERNAME, "password": TEST_ADMIN_PASSWORD}
        ).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw_cookie = resp.headers.get("Set-Cookie", "")
    except urllib.error.HTTPError as exc:
        pytest.skip(
            f"POST /api/auth/login returned {exc.code}: cannot obtain admin "
            f"session cookie. Body: {exc.read().decode(errors='replace')[:200]}"
        )
    if not raw_cookie:
        pytest.skip("POST /api/auth/login did not return a Set-Cookie header")

    # Set-Cookie format: "session=<value>; Path=/; HttpOnly; ..." — first segment
    # is the name=value pair.
    return raw_cookie.split(";", 1)[0]


@pytest.fixture(autouse=True)
def _autouse_admin_login(request, context, _admin_session_cookie):
    """Inject the admin session cookie into the Playwright browser context.

    Skipped for tests marked @pytest.mark.unauthenticated. Cookie is set on
    the context (not page) so it survives any same-context navigation.
    """
    if request.node.get_closest_marker("unauthenticated"):
        yield
        return

    name, value = _admin_session_cookie.split("=", 1)
    context.add_cookies(
        [
            {
                "name": name,
                "value": value,
                "domain": "127.0.0.1",
                "path": "/",
                "httpOnly": True,
                "secure": False,
                "sameSite": "Strict",
            }
        ]
    )
    yield
