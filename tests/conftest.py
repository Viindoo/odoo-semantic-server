# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/conftest.py
import asyncio
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from neo4j import GraphDatabase

# Worktree bootstrap: put THIS repo root at sys.path[0] before any `src.*` import.
# When `pytest` runs as the editable-installed console-script, sys.path is seeded
# from the editable `.pth` of whichever checkout was `pip install -e`d (the MAIN
# tree). From a sibling git worktree, a bare `pytest` would otherwise import
# `src.*` from the main tree instead of this worktree's code — silently testing
# the wrong source. Prepending the conftest's own repo root forces this worktree's
# `src/` to win. conftest.py is imported in full BEFORE pytest collects any test
# module, and no `src.*` import exists above this line (all such imports live
# inside fixtures), so this runs before the first `src.*` resolution.
_repo_root = str(Path(__file__).resolve().parent.parent)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

# C4: never COLLECT the parser fixture tree as live test modules. The files under
# tests/fixtures/test_src/**/ are REAL Odoo test sources used as parser INPUT
# (loaded by path in tests/test_parser_test.py via FIXTURE_DIR); they `from odoo...
# import` and would fail collection with ModuleNotFoundError, interrupting the whole
# unit suite (collection precedes marker deselection). collect_ignore_glob is the
# canonical pytest opt-out (relative to this conftest's dir).
collect_ignore_glob = ["fixtures/*"]

# Test-only bcrypt work factor. Production uses cost=12 (ADR-0011); the ~45
# password-hashing tests don't need that strength, and a cost-12 hash is ~0.4s
# each. Lower to 4 here so auth/MFA tests run fast. setdefault preserves an
# explicit shell/CI override (e.g. a fidelity test that wants cost=12). Must be
# set BEFORE src.web_ui.auth is first imported (it reads this at module load).
os.environ.setdefault("BCRYPT_ROUNDS", "4")

NEO4J_URI = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_TEST_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_TEST_PASSWORD", "password")
TEST_VERSION = "99.0"  # dedicated test version — avoids conflict with real data

# Production helpers (e.g. pipeline._neo4j_creds) read NEO4J_* env, not
# NEO4J_TEST_*. Mirror the test names → prod names so any prod helper used by
# tests resolves to the test Neo4j. setdefault preserves an explicit override
# from the surrounding shell / CI workflow.
os.environ.setdefault("NEO4J_URI", NEO4J_URI)
os.environ.setdefault("NEO4J_USER", NEO4J_USER)
os.environ.setdefault("NEO4J_PASSWORD", NEO4J_PASSWORD)

# Canonical version defined in .env.example (NEO4J_IMAGE=...).
# CI loads .env.example before running tests; local dev copies .env.example → .env.
_NEO4J_IMAGE = os.getenv("NEO4J_IMAGE", "neo4j:5.26.25")


def _priority2_guard_blocks_run() -> bool:
    """Return True when the Priority 2 bolt fallback should be blocked.

    Blocks when ALL three conditions hold simultaneously:
      - Not running in CI (CI env var is not "true")
      - NEO4J_TEST_URI is the default "bolt://localhost:7687"
      - NEO4J_TEST_PASSWORD is the default "password"

    Rationale: on a machine that already has a production (or non-test) Neo4j
    on :7687 with non-default credentials, attempting a connect with "password"
    triggers an auth-rate-limit burst. Incident: 2026-05-26 14:01 UTC.
    Override: set NEO4J_TEST_URI or NEO4J_TEST_PASSWORD to non-default values.
    """
    # CI detection tolerates common values across providers:
    # GitHub Actions sets "true"; Jenkins/GitLab/Travis often set "1";
    # some environments use "True" / "TRUE" / "yes".
    _is_ci = _is_truthy_env("CI")
    _default_uri = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687") == "bolt://localhost:7687"
    _default_pw = os.getenv("NEO4J_TEST_PASSWORD", "password") == "password"
    return _default_uri and _default_pw and not _is_ci


def _is_truthy_env(name: str) -> bool:
    """Return True if the named env var is set to a truthy value.

    Recognised truthy values (case-insensitive): "1", "true", "yes".
    Returns False when the env var is absent or set to any other value.
    Used by remote-host guard, db-name guard, and other opt-in escape hatches
    so every truthy check in conftest uses identical semantics.
    """
    return os.getenv(name, "").lower() in {"1", "true", "yes"}


# Loopback hosts that are always safe to target with destructive test fixtures.
# Anything else (a routable host) is treated as potentially-production.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]", ""})


def _host_from_target(target: str) -> str:
    """Best-effort host extraction from a bolt URI or libpq DSN.

    Handles ``bolt://host:7687``, ``neo4j+s://host``, ``postgresql://user:pw@host:5432/db``
    (URL form) and the ``host=... port=...`` keyword DSN form. Returns the lowercased
    host, or ``""`` when it cannot be determined (treated as loopback → safe-by-omission;
    we only HARD-block on a *positively-identified* remote host).
    """
    from urllib.parse import urlsplit

    target = (target or "").strip()
    if not target:
        return ""
    # Keyword libpq DSN: "host=db.prod port=5432 dbname=..."
    if "=" in target and "://" not in target:
        for tok in target.split():
            if tok.lower().startswith("host="):
                return tok.split("=", 1)[1].strip().lower()
        return ""
    try:
        return (urlsplit(target).hostname or "").lower()
    except Exception:
        return ""


def _assert_test_db_target_is_safe(env_var: str, default: str) -> None:
    """Fail-closed guard: refuse to run destructive DB fixtures against a REMOTE
    (non-loopback) store unless the operator explicitly opts in.

    Destructive test fixtures (``clean_neo4j`` runs ``DETACH DELETE``; the PG
    fixtures TRUNCATE/DELETE rows). If someone exports ``NEO4J_TEST_URI`` /
    ``PG_TEST_DSN`` pointing at a real/production store WITH valid (non-default)
    credentials, the ADR-0040 default-creds guard does NOT fire (creds aren't
    default) and the tests would happily wipe production data.

    This guard hard-skips when ALL of:
      - the resolved host is positively a NON-loopback host, AND
      - not running in CI (``CI`` env var unset/false), AND
      - the operator has NOT set ``OSM_ALLOW_REMOTE_TEST_DB=1``.

    CI-safe by construction: GitHub Actions sets ``CI=true`` AND points both
    targets at ``127.0.0.1`` (loopback) — either condition alone exempts CI.
    Local default (``localhost``) is loopback → never blocked. Only an explicit
    remote target on a dev box is refused. See ADR-0040.
    """
    if _is_truthy_env("CI"):
        return
    if _is_truthy_env("OSM_ALLOW_REMOTE_TEST_DB"):
        return
    target = os.getenv(env_var, default)
    host = _host_from_target(target)
    if host and host not in _LOOPBACK_HOSTS:
        pytest.skip(
            f"DESTRUCTIVE test DB guard: {env_var} resolves to a non-loopback host "
            f"({host!r}). The Neo4j/Postgres test fixtures DETACH DELETE / TRUNCATE "
            f"rows and must never run against a remote (production) store. "
            f"If this host really is a disposable test instance, set "
            f"OSM_ALLOW_REMOTE_TEST_DB=1 to override. See "
            f"docs/adr/0040-conftest-priority2-fallback-guard.md."
        )


# Safe db-name patterns: must start with "osm_test_" OR end with "_test".
# This guard is host-independent: it fires for both localhost and remote targets
# so a prod db-name is always rejected regardless of where Postgres runs.
# Escape hatch: OSM_ALLOW_NONTEST_DB=1 (explicit opt-in, documents intent).
# NOTE: CI=true does NOT bypass this guard — CI must use a compliant db-name.
_TEST_DB_NAME_PREFIXES = ("osm_test_",)
_TEST_DB_NAME_SUFFIXES = ("_test",)
_KNOWN_PROD_DB_NAMES = frozenset({"odoo_semantic"})


def _dbname_from_dsn(dsn: str) -> str:
    """Extract the database name from a postgresql:// URL or keyword DSN.

    Delegates to ``psycopg2.extensions.parse_dsn`` which is the same parser
    the driver itself uses, so keyword and URL forms are handled identically
    and this function cannot diverge from the driver's own interpretation.
    Returns empty string when the db-name cannot be determined.
    """
    dsn = (dsn or "").strip()
    if not dsn:
        return ""
    try:
        import psycopg2.extensions as _pext
        return _pext.parse_dsn(dsn).get("dbname", "")
    except Exception:
        return ""


def _assert_pg_db_name_is_safe(db_name: str) -> None:
    """Fail-closed name-based guard for PG destructive fixtures.

    Skips the test (via pytest.skip) when the target db-name is NEITHER:
      - prefixed with one of ``_TEST_DB_NAME_PREFIXES`` (e.g. ``osm_test_abc123``), NOR
      - suffixed with one of ``_TEST_DB_NAME_SUFFIXES`` (e.g. ``mydb_test``).

    Additionally hard-skips for known production db-names (``odoo_semantic``)
    regardless of any suffix/prefix match.

    Escape hatch: ``OSM_ALLOW_NONTEST_DB=1`` — set this only when you
    intentionally target a non-standard test DB (e.g. a manually-provisioned QA
    database with a legacy name). This bypass is intentional and documented;
    it is NOT triggered by ``CI=true`` (unlike the remote-host guard). CI must
    use a compliant db-name.

    This guard is orthogonal to the remote-host guard: it fires for localhost
    prod DBs that the host guard misses, closing the primary RCA-1 hole.
    """
    if _is_truthy_env("OSM_ALLOW_NONTEST_DB"):
        return
    if not db_name:
        # Unknown name — safe-by-omission: we cannot verify, so skip.
        pytest.skip(
            "DESTRUCTIVE PG guard: cannot determine db-name from DSN. "
            "Set a recognisable postgresql:// URL or OSM_ALLOW_NONTEST_DB=1."
        )
    # Hard-block known production names (host-independent).
    if db_name in _KNOWN_PROD_DB_NAMES:
        pytest.skip(
            f"DESTRUCTIVE PG guard: db-name {db_name!r} is a known production "
            f"database. PG test fixtures DROP/TRUNCATE tables and must never "
            f"run against a production store. Use a test database whose name "
            f"starts with 'osm_test_' or ends with '_test', or set "
            f"OSM_ALLOW_NONTEST_DB=1 to override explicitly."
        )
    # Require a test-marker pattern (prefix or suffix).
    is_safe = any(db_name.startswith(p) for p in _TEST_DB_NAME_PREFIXES) or any(
        db_name.endswith(s) for s in _TEST_DB_NAME_SUFFIXES
    )
    if not is_safe:
        pytest.skip(
            f"DESTRUCTIVE PG guard: db-name {db_name!r} does not match a safe "
            f"test-db pattern (must start with 'osm_test_' or end with '_test'). "
            f"PG test fixtures DROP/TRUNCATE tables. Use a disposable test DB or "
            f"set OSM_ALLOW_NONTEST_DB=1 to override explicitly."
        )


@pytest.fixture(autouse=True)
def _ensure_current_event_loop():
    """Py3.12 guard: restore a usable current event loop before each test.

    Python 3.12 asyncio.Runner.close() calls set_event_loop(None), leaving the
    policy state as {_set_called: True, _loop: None}.  A subsequent bare call to
    asyncio.get_event_loop() then raises RuntimeError instead of auto-creating a
    loop (the auto-create guard checks _set_called before creating).

    With asyncio_mode='auto' (pytest-asyncio), every async test is wrapped in a
    Runner; when the Runner is torn down the thread-level loop is cleared.
    Any synchronous code that then calls asyncio.get_event_loop() directly
    (rather than asyncio.run() or asyncio.new_event_loop()) will crash.

    This fixture detects the poisoned state and installs a fresh loop so each
    test starts from a known-good baseline, without touching a loop that is
    already valid (no leak, no interference with pytest-asyncio's own fixtures).
    """
    try:
        # get_running_loop() does NOT emit DeprecationWarning (unlike
        # get_event_loop() on Python 3.12 when no running loop is set).
        # It raises RuntimeError when no loop is running, which we catch
        # to install a fresh loop — preserving the anti-poisoning intent.
        asyncio.get_running_loop()
    except RuntimeError:
        # _set_called=True and _loop=None: install a fresh loop so the test
        # (and any helper that calls get_event_loop()) does not crash.
        asyncio.set_event_loop(asyncio.new_event_loop())
    yield


@pytest.fixture(scope="session", autouse=True)
def _close_server_driver_at_session_end():
    """Close the Neo4j driver singleton after all tests complete.

    src.mcp.server._driver is a module-level singleton that is never closed
    during test runs (there is no ASGI lifespan in tests).  When the process
    exits, Python's GC calls Driver.__del__(), which emits
    "Driver's destructor called while session still open" (neo4j >= 5.x).

    This session-scoped autouse fixture runs teardown once — after the last
    test — and calls driver.close() so the destructor is a no-op.
    """
    yield
    import sys
    srv = sys.modules.get("src.mcp.server")
    if srv is not None and getattr(srv, "_driver", None) is not None:
        try:
            srv._driver.close()
        except Exception:  # noqa: BLE001
            pass
        srv._driver = None


def _playwright_chromium_available() -> bool:
    """True if Playwright chromium binary is installed at the expected location.

    pytest-playwright's session-scoped browser fixture raises a hard error
    (not a skip) when the binary is missing, which then cascades into other
    tests that share session-scoped DB fixtures. Detect missing binary
    upfront and convert to a clean skip via pytest_collection_modifyitems.
    """
    cache_root = Path.home() / ".cache" / "ms-playwright"
    if not cache_root.is_dir():
        return False
    return any(p.name.startswith("chromium") for p in cache_root.iterdir())


def pytest_collection_modifyitems(config, items):
    """Convert browser-marker tests to clean SKIPs when chromium binary missing.

    Without this, pytest-playwright's `browser` fixture raises
    "Executable doesn't exist at ~/.cache/ms-playwright/chromium..." during
    fixture setup, and the resulting ERROR cascades to corrupt the shared
    `pg_conn` session fixture — failing unrelated tests in the same suite.
    Local dev: run `playwright install chromium` to enable browser tests.
    """
    if _playwright_chromium_available():
        return
    skip_marker = pytest.mark.skip(
        reason="Playwright chromium not installed — run: playwright install chromium"
    )
    for item in items:
        if "browser" in item.keywords:
            item.add_marker(skip_marker)


@pytest.fixture(autouse=True)
def _bypass_webui_auth_for_legacy_tests(monkeypatch, request):
    """Disable the W16 auth middleware for tests that pre-date session auth.

    These tests exercise the real auth flow end-to-end and must NOT bypass.
    All other tests written before/during M8 assume an unauthenticated client
    can hit /admin/* — they rely on this bypass.

    M9 cross-test contamination guard: several M9 test modules (test_signup,
    test_operations_backup_route, test_restore_security) historically set
    WEBUI_AUTH_DISABLED directly via os.environ (not monkeypatch), leaking
    the bypass into subsequent tests. For tests that exercise real auth flow
    we explicitly delete the env var so middleware's is_test_bypass_active()
    correctly returns False regardless of what prior tests did.
    """
    fname = request.node.fspath.basename
    # These tests exercise real auth flow end-to-end.
    # Browser tests also exercise the real auth flow (W7 refactor).
    # Other M9 auth-flow tests (signup/oauth/totp/admin_users/restore) also
    # exercise the real auth flow and must not be silently bypassed.
    real_auth_flow_files = {
        "test_web_ui_auth.py",
        "test_web_ui_browser.py",
        # M9 auth-flow tests: each of these exercises real auth logic and must
        # not fall through to the WEBUI_AUTH_DISABLED bypass.  Each file is
        # responsible for managing its own bypass when bypass is incidental
        # (e.g. test_admin_users seeds WEBUI_AUTH_DISABLED at module level for
        # tests that check admin data, not the auth flow itself).
        "test_signup.py",
        "test_oauth.py",
        "test_totp.py",
        "test_admin_users.py",
        "test_restore_security.py",
        # W3 MFA step-up fix: exercises the real auth gate (require_admin_with_fresh_mfa)
        # and the new /api/auth/totp/step-up endpoint end-to-end.
        "test_mfa_step_up.py",
        # WS2a/WS2c: real login + verify + change-password flow with real DB.
        "test_change_password.py",
    }
    if fname in real_auth_flow_files:
        # Defensive: scrub any leaked bypass env from prior tests in the session.
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)
        return
    monkeypatch.setenv("WEBUI_AUTH_DISABLED", "1")


@pytest.fixture(autouse=True)
def _reset_mcp_middleware_state():
    """Clear in-process MCP middleware caches/buffers between tests.

    Wave 2 introduced plan-aware quota + rate limit globals in
    `src/mcp/middleware.py`. Cross-test contamination causes monthly quota
    buffer to accumulate across the 1469-test session, eventually tripping
    429 on tests that share the session-scoped api_key fixture.

    Globals reset (all module-level dicts in `src.mcp.middleware`):
      - `_PLAN_CACHE`         plan-info per api_key_id (LRU + TTL)
      - `_KEY_CACHE`          api_key_id lookup keyed by token hash
      - `_CACHE_TS`           timestamp companion to `_KEY_CACHE` for TTL eviction
      - `_TENANT_CACHE`       tenant_id lookup keyed by token hash
      - `_usage_buffer`       in-memory call_count delta awaiting DB flush
      - `_rate_buckets`       per-key rolling rpm window

    Both `_CACHE_TS` + `_TENANT_CACHE` were previously skipped, leaving a
    NEGATIVE cache entry alive across tests → reused api_key_id surfaces as
    "not found" → 401 in unrelated tests. Reset before each test for
    hermetic state.
    """
    try:
        from src.mcp import middleware as _mw
        for name in (
            "_PLAN_CACHE",
            "_KEY_CACHE",
            "_CACHE_TS",
            "_TENANT_CACHE",
            "_OWNER_CACHE",
            "_usage_buffer",
            "_rate_buckets",
        ):
            cache = getattr(_mw, name, None)
            if cache is not None and hasattr(cache, "clear"):
                cache.clear()
    except Exception:
        # If module not importable yet (early collection phase), skip.
        pass

    # WI-RV F-K: also clear the Admin Settings overlay LRU
    # (``src.settings._cache``) and the EE-modules cache
    # (``src.data.ee_modules._cache``).  Both are module-level dicts that
    # leak across tests when a setting is patched in one test and read in
    # the next — the next test sees the prior test's value until the
    # 60 s / 300 s TTL elapses (typically NEVER inside a single pytest
    # session).  This caused intermittent flakes on
    # tests/test_e2e_quota_hotreload.py and the wave0 admin-gate suite.
    try:
        from src.settings import invalidate_all as _invalidate_settings
        _invalidate_settings()
    except Exception:
        pass
    try:
        from src.data.ee_modules import invalidate_ee_modules_cache
        invalidate_ee_modules_cache()
    except Exception:
        pass

    yield


@pytest.fixture(scope="session")
def neo4j_driver():
    """
    Neo4j driver for the whole test session.

    CI (CI=true): connect directly to NEO4J_TEST_URI — service container already running,
                  skip testcontainers import to avoid @wait_container_is_ready warning.
    Local dev:    Priority 1: testcontainers (spins up Docker container).
                  Priority 2: connect directly to NEO4J_TEST_URI.
                  Fallback:  skip with specific reason.
    """
    # Refuse a positively-remote NEO4J_TEST_URI on a dev box (destructive fixtures).
    _assert_test_db_target_is_safe("NEO4J_TEST_URI", "bolt://localhost:7687")
    # CI path — GitHub Actions sets CI=true; service container is already running.
    # Skip testcontainers import entirely to avoid import-time DeprecationWarning
    # from @wait_container_is_ready decorator (upstream issue in testcontainers 4.x).
    if os.getenv("CI"):
        driver = None
        try:
            driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
            driver.verify_connectivity()
        except Exception as e:
            if driver is not None:
                driver.close()
            pytest.skip(f"Neo4j service container not available in CI: {e}")
        yield driver
        driver.close()
        return

    # Local dev path — try testcontainers first.
    # Lazy import: keeps import-time DeprecationWarning out of unit-test runs
    # (-m "not neo4j" never reaches this fixture at all).
    from testcontainers.core.wait_strategies import LogMessageWaitStrategy
    from testcontainers.neo4j import Neo4jContainer

    class _Neo4jContainer(Neo4jContainer):
        """Override _connect() to prevent deprecated wait_for_logs runtime warning.

        Neo4jContainer._connect() calls wait_for_logs() (deprecated in testcontainers 4.x).
        LogMessageWaitStrategy set via .waiting_for() already handles readiness;
        this override just does a connectivity verify without the deprecated call.
        """
        def _connect(self) -> None:
            with self.get_driver() as driver:
                driver.verify_connectivity()

    container = None
    driver = None
    tc_error = None

    # --- Priority 1: testcontainers (requires Docker daemon running) ---
    try:
        container = _Neo4jContainer(_NEO4J_IMAGE).waiting_for(
            LogMessageWaitStrategy("Remote interface available at")
        )
        container.start()
        bolt_url = container.get_connection_url()
        driver = GraphDatabase.driver(bolt_url, auth=("neo4j", "password"))
        driver.verify_connectivity()
        # Expose for fixtures that create their own connections (writer, mcp_tools)
        os.environ["NEO4J_TEST_URI"] = bolt_url
        os.environ["NEO4J_TEST_USER"] = "neo4j"
        os.environ["NEO4J_TEST_PASSWORD"] = "password"
        # Also export NEO4J_* (prod names) so production helpers like
        # `pipeline._neo4j_creds()` resolve to the test container during pytest.
        # Production code intentionally does NOT consult NEO4J_TEST_* to prevent
        # test env from leaking into a production process (web_ui subprocess bug).
        os.environ["NEO4J_URI"] = bolt_url
        os.environ["NEO4J_USER"] = "neo4j"
        os.environ["NEO4J_PASSWORD"] = "password"
    except Exception as e:
        tc_error = e
        if container is not None:
            try:
                container.stop()
            except Exception:
                pass
        if driver is not None:
            driver.close()
        container = None
        driver = None

    # --- Priority 2: Neo4j already running (docker compose up -d neo4j) ---
    if driver is None:
        if _priority2_guard_blocks_run():
            pytest.skip(
                "Priority 2 fallback (localhost:7687 with default 'password') is "
                "only allowed in CI to prevent accidental hits on a non-test Neo4j "
                "running on the same host (e.g. production). "
                "Set NEO4J_TEST_PASSWORD or NEO4J_TEST_URI to a non-default value "
                "to enable. See docs/adr/0040-conftest-priority2-fallback-guard.md "
                "(forthcoming) + 2026-05-26 prod RCA."
            )
        bolt_driver = None
        try:
            bolt_driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
            bolt_driver.verify_connectivity()
            driver = bolt_driver
        except Exception as bolt_error:
            if bolt_driver is not None:
                bolt_driver.close()
            lines = ["[FIX] Install Docker + start daemon"
                     " → testcontainers will spin up Neo4j automatically"]
            tc_msg = (
                f"  testcontainers error: {tc_error}" if tc_error
                else "  testcontainers: not attempted"
            )
            lines.append(tc_msg)
            lines.append(f"  bolt ({NEO4J_URI}) error: {bolt_error}")
            lines.append("  Or run manually: make neo4j-up")
            pytest.skip("\n".join(lines))

    yield driver

    driver.close()
    if container is not None:
        container.stop()


@pytest.fixture
def clean_neo4j(neo4j_driver):
    """Delete all nodes with odoo_version=TEST_VERSION before and after each test."""
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=TEST_VERSION)
    yield neo4j_driver
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=TEST_VERSION)


@pytest.fixture
def tmp_git_repo(tmp_path):
    """Create a temporary git repo with branch 17.0 for scanner tests."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "checkout", "-b", "17.0"],
        check=True, capture_output=True,
    )
    return tmp_path


@pytest.fixture(scope="module")
def monkeypatch_module():
    """Module-scoped monkeypatch fixture (pytest built-in is function-scoped only).

    Required by fixtures with scope='module' that need env var isolation.
    Undo all patches after the module finishes.
    """
    from _pytest.monkeypatch import MonkeyPatch
    mp = MonkeyPatch()
    yield mp
    mp.undo()


def make_git_repo(path: Path, branch: str) -> Path:
    """Create a git repo at the given path with the given branch. Used in tests."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "checkout", "-b", branch],
        check=True, capture_output=True,
    )
    return path


def make_manifest(
    module_dir: Path,
    name: str,
    version: str,
    depends: list,
    installable: bool = True,
) -> None:
    """Create __manifest__.py in module_dir. Used in tests."""
    module_dir.mkdir(parents=True, exist_ok=True)
    (module_dir / "__manifest__.py").write_text(
        f"{{'name': {name!r}, 'version': {version!r}, "
        f"'depends': {depends!r}, 'installable': {installable!r}}}\n"
    )


# ---------------------------------------------------------------------------
# Wave 6 (ADR-0023) — seed helpers for OWLComp / JSPatch / QWebTmpl nodes.
# Used by `tests/test_mcp_server.py::test_list_owl_components_*`,
# `test_list_qweb_templates_*`, `test_list_js_patches_*`.
# Direct Cypher MERGE keeps the helper independent of writer_neo4j evolution.
# ---------------------------------------------------------------------------


def seed_owl_components(
    driver, *, module: str, odoo_version: str, components: list[dict],
) -> None:
    """MERGE a batch of OWLComp nodes (and their Module) for tests.

    Each component dict: ``{"name": str, "bound_model": str | None,
    "template": str | None}``. Repo defaults to ``"test_repo"``.
    """
    with driver.session() as session:
        session.run(
            "MERGE (mod:Module {name: $m, odoo_version: $v}) "
            "SET mod.repo = 'test_repo', mod.edition = 'community'",
            m=module, v=odoo_version,
        )
        for comp in components:
            session.run(
                "MERGE (c:OWLComp {name: $name, module: $m, odoo_version: $v}) "
                "SET c.bound_model = $bound_model, c.template = $template, "
                "    c.file_path = '' "
                "WITH c "
                "MATCH (mod:Module {name: $m, odoo_version: $v}) "
                "MERGE (c)-[:DEFINED_IN]->(mod)",
                name=comp["name"], m=module, v=odoo_version,
                bound_model=comp.get("bound_model"),
                template=comp.get("template"),
            )


def seed_js_patches(
    driver, *, module: str, odoo_version: str, patches: list[dict],
) -> None:
    """MERGE a batch of JSPatch nodes (and their Module) for tests.

    Each patch dict: ``{"target": str, "patch_name": str,
    "era": "extend"|"include"|"patch"}``. Repo defaults to ``"test_repo"``.
    """
    with driver.session() as session:
        session.run(
            "MERGE (mod:Module {name: $m, odoo_version: $v}) "
            "SET mod.repo = 'test_repo', mod.edition = 'community'",
            m=module, v=odoo_version,
        )
        for p in patches:
            session.run(
                "MERGE (j:JSPatch {target: $target, patch_name: $pn, "
                "                  module: $m, odoo_version: $v}) "
                "SET j.era = $era, j.file_path = '' "
                "WITH j "
                "MATCH (mod:Module {name: $m, odoo_version: $v}) "
                "MERGE (j)-[:DEFINED_IN]->(mod)",
                target=p["target"], pn=p["patch_name"], m=module,
                v=odoo_version, era=p["era"],
            )


def seed_qweb_templates(
    driver, *, module: str, odoo_version: str, templates: list[dict],
) -> None:
    """MERGE a batch of QWebTmpl nodes (and their Module) for tests.

    Each template dict: ``{"xmlid": str, "inherit_xmlid": str | None}``.
    When ``inherit_xmlid`` is set the helper MERGE-creates the EXTENDS_TMPL
    edge (and the parent placeholder if needed).
    """
    with driver.session() as session:
        session.run(
            "MERGE (mod:Module {name: $m, odoo_version: $v}) "
            "SET mod.repo = 'test_repo', mod.edition = 'community'",
            m=module, v=odoo_version,
        )
        for t in templates:
            session.run(
                "MERGE (qt:QWebTmpl {xmlid: $xmlid, odoo_version: $v}) "
                "SET qt.module = $m "
                "WITH qt "
                "MATCH (mod:Module {name: $m, odoo_version: $v}) "
                "MERGE (qt)-[:DEFINED_IN]->(mod)",
                xmlid=t["xmlid"], m=module, v=odoo_version,
            )
            if t.get("inherit_xmlid"):
                session.run(
                    "MERGE (parent:QWebTmpl {xmlid: $pxmlid, odoo_version: $v}) "
                    "ON CREATE SET parent.module = '__seed_parent__' "
                    "WITH parent "
                    "MATCH (child:QWebTmpl {xmlid: $cxmlid, odoo_version: $v}) "
                    "MERGE (child)-[:EXTENDS_TMPL]->(parent)",
                    cxmlid=t["xmlid"], pxmlid=t["inherit_xmlid"], v=odoo_version,
                )


def seed_stylesheets(
    driver, *, module: str, odoo_version: str, stylesheets: list[dict],
    imports: list[tuple[str, str]] | None = None,
) -> None:
    """MERGE a batch of :Stylesheet nodes and optional :IMPORTS edges for tests.

    Each stylesheet dict: ``{"file_path": str, "language": str,
    "selector_count": int, "variable_count": int, "import_count": int,
    "mixin_count": int}``.  ``import_count`` should equal the number of
    :IMPORTS edges declared for that file.

    ``imports`` is a list of ``(src_file_path, tgt_file_path)`` pairs; both
    nodes must already exist in the ``stylesheets`` list.
    """
    with driver.session() as session:
        for ss in stylesheets:
            session.run(
                """
                MERGE (s:Stylesheet {file_path: $fp, module: $m, odoo_version: $v})
                ON CREATE SET s.language = $lang,
                              s.selector_count = $sel,
                              s.variable_count = $var,
                              s.import_count = $imp,
                              s.mixin_count = $mix
                ON MATCH  SET s.language = $lang,
                              s.selector_count = $sel,
                              s.variable_count = $var,
                              s.import_count = $imp,
                              s.mixin_count = $mix
                """,
                fp=ss["file_path"], m=module, v=odoo_version,
                lang=ss.get("language", "css"),
                sel=ss.get("selector_count", 0),
                var=ss.get("variable_count", 0),
                imp=ss.get("import_count", 0),
                mix=ss.get("mixin_count", 0),
            )
        for src_fp, tgt_fp in (imports or []):
            session.run(
                """
                MATCH (src:Stylesheet {file_path: $sfp, module: $m, odoo_version: $v})
                MATCH (tgt:Stylesheet {file_path: $tfp, odoo_version: $v})
                MERGE (src)-[:IMPORTS]->(tgt)
                """,
                sfp=src_fp, tfp=tgt_fp, m=module, v=odoo_version,
            )


# --- PostgreSQL fixtures (for src/db tests) ---

# PG_TEST_DSN has NO default. It is derived at session-start from the ephemeral
# DB created by _ephemeral_pg_db. This module-level variable is set by that
# fixture; it starts as None so that any direct import before the fixture runs
# is always None (never a prod DSN).
PG_TEST_DSN: str | None = os.getenv("PG_TEST_DSN")  # explicit override only; no default

# PG_ADMIN_DSN: superuser / CREATEDB connection to the maintenance database.
# Required for ephemeral-DB lifecycle (CREATE DATABASE / DROP DATABASE).
# Example: postgresql://postgres:password@localhost:5432/postgres
# If not set, all postgres-marked tests are skipped.
_PG_ADMIN_DSN: str | None = os.getenv("PG_ADMIN_DSN")


def get_test_dsn() -> str | None:
    """Return the live ``PG_TEST_DSN`` module attribute, or None when not yet set.

    Callers (typically other test modules) should skip the test when this
    returns None — it means the ephemeral DB fixture has not run yet (or
    PG_ADMIN_DSN was not supplied) and no PostgreSQL connection is available.

    Usage::

        dsn = get_test_dsn()
        if dsn is None:
            pytest.skip("PostgreSQL not available (PG_ADMIN_DSN not set)")

    This function reads the live module-level ``PG_TEST_DSN`` each time it is
    called so it correctly reflects the value written by ``_ephemeral_pg_db``
    during session setup. Never cache the return value before the test session
    starts — it will be None until the fixture has run.
    """
    return PG_TEST_DSN


def _build_ephemeral_db_name() -> str:
    """Return a unique, per-run test database name.

    Priority:
      1. ``PG_TEST_DB`` env — explicit operator override (analogous to odoo-bin -d).
      2. Auto: ``osm_test_<uuid4-hex-8>`` + optional xdist worker suffix for
         parallel safety. Multiple concurrent agents or pytest-xdist workers each
         get their own database.
    """
    import uuid

    explicit = os.getenv("PG_TEST_DB", "").strip()
    if explicit:
        return explicit
    worker = os.getenv("PYTEST_XDIST_WORKER", "").strip()
    uid = uuid.uuid4().hex[:8]
    suffix = f"_{worker}" if worker else ""
    return f"osm_test_{uid}{suffix}"


def _drop_ephemeral_db(admin_conn, db_name: str) -> None:
    """Best-effort terminate-connections + DROP DATABASE for an ephemeral test DB.

    Shared by the migrate-failure cleanup path inside ``_ephemeral_pg_db`` and
    the normal teardown path so the SQL is written exactly once (SSOT).
    Silently swallows all errors — caller logs / pytest.skips independently.

    ``admin_conn`` must have ``autocommit = True`` and be connected to the
    maintenance database (NOT to ``db_name`` itself).
    """
    import psycopg2.extensions as _pext

    try:
        with admin_conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (db_name,),
            )
    except Exception:
        pass
    try:
        with admin_conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {_pext.quote_ident(db_name, admin_conn)}")
    except Exception:
        pass


@pytest.fixture(scope="session")
def _ephemeral_pg_db():
    """Session-scoped: create an ephemeral test DB, yield its DSN, then drop it.

    Lifecycle (mirrors odoo-bin -d but for testing):
      1. Connect to the maintenance database via PG_ADMIN_DSN (needs CREATEDB).
      2. Choose a unique db-name (osm_test_<uuid8>[_worker]) or PG_TEST_DB.
      3. Validate the db-name via _assert_pg_db_name_is_safe (hard guard).
      4. CREATE DATABASE <name>.
      5. Apply schema/migrations via run_migrations (once; web_ui_server reuses this).
      6. Yield the test DSN (postgresql://.../<name>).
      7. Teardown: terminate connections then DROP DATABASE.

    Skips all postgres tests when PG_ADMIN_DSN is absent — never falls back to
    a hardcoded production DSN.
    """
    from urllib.parse import urlsplit, urlunsplit

    import psycopg2

    global PG_TEST_DSN  # noqa: PLW0603

    # If PG_TEST_DSN is explicitly set by the operator (env override), use it
    # directly WITHOUT ephemeral CREATE/DROP — operator owns the lifecycle.
    if PG_TEST_DSN is not None:
        db_name = _dbname_from_dsn(PG_TEST_DSN)
        _assert_pg_db_name_is_safe(db_name)
        _assert_test_db_target_is_safe("PG_TEST_DSN", "")
        try:
            conn = psycopg2.connect(PG_TEST_DSN)
        except Exception as e:
            pytest.skip(f"PostgreSQL not reachable at {PG_TEST_DSN}: {e}")
        conn.autocommit = True
        try:
            from src.db.migrate import run_migrations
            run_migrations(conn)
        except Exception as e:
            conn.close()
            pytest.skip(f"Could not apply migrations to {PG_TEST_DSN!r}: {e}")
        conn.close()
        yield PG_TEST_DSN
        return  # operator manages lifecycle; no DROP

    # Require PG_ADMIN_DSN for ephemeral lifecycle.
    if _PG_ADMIN_DSN is None:
        pytest.skip(
            "PG_ADMIN_DSN not set — cannot create ephemeral test database. "
            "Set PG_ADMIN_DSN=postgresql://postgres:password@localhost:5432/postgres "
            "(a user with CREATEDB) or set PG_TEST_DSN to an existing '*_test' DB."
        )

    db_name = _build_ephemeral_db_name()
    _assert_pg_db_name_is_safe(db_name)

    # Build the test DSN by replacing the db-name in PG_ADMIN_DSN.
    parsed = urlsplit(_PG_ADMIN_DSN)
    test_dsn = urlunsplit(parsed._replace(path=f"/{db_name}"))

    # Connect to maintenance DB to CREATE the ephemeral database.
    try:
        admin_conn = psycopg2.connect(_PG_ADMIN_DSN)
    except Exception as e:
        pytest.skip(f"PG_ADMIN_DSN not reachable ({_PG_ADMIN_DSN!r}): {e}")
    admin_conn.autocommit = True
    import psycopg2.extensions as _pext
    try:
        with admin_conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE {_pext.quote_ident(db_name, admin_conn)}")
    except Exception as e:
        admin_conn.close()
        pytest.skip(f"Could not CREATE DATABASE {db_name!r}: {e}")

    # Apply schema/migrations to the fresh ephemeral DB.
    try:
        setup_conn = psycopg2.connect(test_dsn)
        setup_conn.autocommit = True
        from src.db.migrate import run_migrations
        run_migrations(setup_conn)
        setup_conn.close()
    except Exception as e:
        # Best-effort cleanup: terminate connections + drop then skip.
        _drop_ephemeral_db(admin_conn, db_name)
        admin_conn.close()
        pytest.skip(f"Could not apply migrations to ephemeral DB {db_name!r}: {e}")

    # Publish DSN to the module-level variable so pg_conn and direct importers see it.
    PG_TEST_DSN = test_dsn  # noqa: PLW0603

    yield test_dsn

    # --- Teardown: terminate connections + drop DB ---
    _drop_ephemeral_db(admin_conn, db_name)
    admin_conn.close()


@pytest.fixture(scope="session")
def pg_conn(_ephemeral_pg_db):
    """Session-scoped PostgreSQL connection against the ephemeral test database.

    Depends on _ephemeral_pg_db (session-scoped) which creates the DB,
    applies migrations, and ensures teardown (DROP DATABASE) after the session.
    Skips automatically when PG_ADMIN_DSN is absent (no ephemeral DB available).
    """
    import psycopg2

    dsn = _ephemeral_pg_db  # DSN to the ephemeral (or explicit) test DB
    try:
        conn = psycopg2.connect(dsn)
    except Exception as e:
        pytest.skip(f"PostgreSQL not reachable at {dsn}: {e}")
    conn.autocommit = True
    # Initialize centralized pool so store accessors (auth_store, repo_store, etc.) work in tests
    from src.db.pg import init_pool
    init_pool(dsn, min_conn=1, max_conn=3)
    yield conn
    import src.db.pg as _pg_mod
    try:
        _pg_mod.get_pool().close()
    except Exception:
        pass
    _pg_mod._pool = None
    _pg_mod._auth_store = None
    _pg_mod._repo_store = None
    _pg_mod._job_store = None
    # Reset the billing store singleton too, else a later test gets a
    # SubscriptionStore wrapping the now-closed pool (M10B P1).
    _pg_mod._subscription_store = None
    conn.close()


# FK-safe drop order for the full test schema + yoyo internal tables.
# Hoisted to module scope (was local to clean_pg) so module-scoped performance
# fixtures — e.g. a per-file "migrate once" fixture — can reuse the EXACT same
# ordered list via wipe_pg_tables() instead of duplicating it (SSOT).
_PG_TEST_TABLES = [
        # yoyo internal (must go first — no FKs referencing schema tables)
        "_yoyo_log",
        "_yoyo_migration",
        "_yoyo_version",
        # M9 tables (FK-leaf tables first, then referenced tables)
        "totp_secrets",
        "email_verifications",
        "active_sessions",
        "login_attempts",
        "admin_audit_log",
        # m9_008 — operational audit, no FK referencing it
        "key_rotation_log",
        # schema tables in FK-safe order
        "pattern_feedback",
        # patterns: curated catalogue seeded by admin-patterns CRUD + seed_patterns.
        # Must appear before webui_users (updated_by FK) and after pattern_feedback
        # (pattern_feedback.pattern_node_id is TEXT, not a FK to patterns).
        # Omitting this caused test-isolation failures: rows from test_admin_patterns_endpoints
        # leaked into test_pipeline_seed_integration (DB-primary loader read stale rows).
        "patterns",
        "indexer_jobs",
        "usage_log",
        # M10B / m13_006 — usage_counter FK→api_keys, must drop BEFORE api_keys.
        # Even though we use DROP TABLE ... CASCADE (so FK order is technically
        # irrelevant), keeping topological order documents the dependency for
        # future maintainers and is safe if a future migration replaces CASCADE
        # with explicit cleanup.
        "usage_counter",
        # 0005 — api_key_session_state FK→api_keys (ON DELETE CASCADE at row level);
        # DROP TABLE api_keys CASCADE only drops the CONSTRAINT, NOT the data,
        # so we must explicitly DROP this table to avoid stale-row leakage.
        "api_key_session_state",
        "repos",
        # api_keys.plan_id FK→plans → api_keys must drop BEFORE plans (m13_006).
        "api_keys",
        "ssh_key_pairs",
        "embeddings",
        # m13_008 — waitlist_emails has no FK; standalone leakable table.
        "waitlist_emails",
        # plans (m13_006) — referenced by api_keys.plan_id; drop AFTER api_keys.
        "plans",
        "profiles",
        # M13 — tenant_members FK→webui_users AND FK→tenants (both ON DELETE
        # CASCADE at row level). DROP TABLE … CASCADE on the referenced tables
        # only drops the CONSTRAINT, NOT the rows, so stale tenant_members
        # rows survive across test sessions. When run_migrations recreates
        # webui_users/tenants, the SERIAL sequences restart at 1 and new IDs
        # collide with stale (user_id, tenant_id) pairs — e.g. the W1 RBAC
        # tests would observe scope={1,2,5} where {1} was expected.
        # Must drop BEFORE webui_users + tenants to respect topological order.
        "tenant_members",
        "webui_users",
        # M13 — must come after all tables that FK-reference it
        "tenants",
]


# ---------------------------------------------------------------------------
# osm_reader role helpers — shared by migration tests that assert GRANT coverage
# (issue #254, WI-10).  Canonical pattern from test_billing_rls.py extracted
# here to avoid duplication across test_migration_m13_010/011/012.
# ---------------------------------------------------------------------------

def ensure_osm_reader_or_skip(conn) -> None:
    """Create the osm_reader NOLOGIN role if absent, or pytest.skip on no CREATEROLE.

    Mirrors the production deploy order: ops/rls_create_osm_reader.sql runs before
    src.db.migrate so the GRANT inside the migration fires and is assertable.

    Callers must commit() on success before run_migrations() (so the role is
    visible inside the migration's own transaction).

    Raises:
        Calls pytest.skip() when the DB user lacks CREATE ROLE privilege —
        not a hard failure because the cause is infra, not code (ADR-0040
        precedent: guard skips under infra conditions, not cryptic errors).
    """
    import psycopg2.errors  # noqa: PLC0415 (local import keeps conftest light)

    try:
        with conn.cursor() as cur:
            cur.execute(
                "DO $$ BEGIN "
                "IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'osm_reader') "
                "THEN CREATE ROLE osm_reader NOLOGIN; END IF; "
                "END $$;"
            )
        conn.commit()
    except psycopg2.errors.InsufficientPrivilege:
        conn.rollback()
        pytest.skip(
            "DB user lacks CREATE ROLE privilege — "
            "run tests as a superuser to enable osm_reader grant coverage."
        )


def drop_osm_reader(conn) -> None:
    """Best-effort teardown: revoke owned objects then drop the osm_reader role.

    Non-fatal — silently swallows errors so teardown never fails a test that
    already passed.  Mirrors _drop_osm_reader in test_billing_rls.py.
    """
    for stmt in (
        "DO $$ BEGIN IF EXISTS (SELECT FROM pg_roles WHERE rolname='osm_reader') "
        "THEN DROP OWNED BY osm_reader; END IF; END $$;",
        "DROP ROLE IF EXISTS osm_reader",
    ):
        try:
            with conn.cursor() as cur:
                cur.execute(stmt)
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass


def wipe_pg_tables(conn):
    """DROP all test-schema + yoyo internal tables in FK-safe order (idempotent).

    Shared by the function-scoped clean_pg fixture and any module-scoped
    "migrate once" performance fixture so both use the identical drop order.
    """
    for tbl in _PG_TEST_TABLES:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")


@pytest.fixture
def clean_pg(pg_conn):
    """Drop test schema tables + yoyo state before and after each test (idempotent).

    yoyo tracks applied migrations in _yoyo_migration.  Leaving this table
    intact between tests causes run_migrations() to report '0 pending' while
    the schema tables (dropped earlier) are absent — producing confusing test
    failures.  Both the schema tables and yoyo internal tables are therefore
    dropped together to guarantee a truly clean starting state.

    Drop order respects FK constraints (see _PG_TEST_TABLES).
    """
    wipe_pg_tables(pg_conn)
    yield pg_conn
    wipe_pg_tables(pg_conn)


@pytest.fixture(scope="module")
def migrated_pg_module(pg_conn):
    """Module-scoped wipe + migrate ONCE (vs clean_pg's per-test wipe+migrate).

    Performance fixture for files whose tests assert only relative/filtered
    state (HTTP status codes, ``ORDER BY id DESC LIMIT 1``, before/after deltas)
    and never an absolute ``count(*)`` or empty-table expectation. Sharing the
    migrated schema across a module collapses N per-test ``run_migrations``
    calls (~1s each) into one.

    INVARIANT (enforce per ADR / red-team m2): a test using this fixture MUST
    NOT assert an absolute row count or expect an empty table — rows accumulate
    across the module. Per-test seeds must be idempotent (``ON CONFLICT``).
    Use the function-scoped ``clean_pg`` if you need a pristine DB per test.
    """
    from src.db.migrate import run_migrations

    wipe_pg_tables(pg_conn)
    run_migrations(pg_conn)
    yield pg_conn
    wipe_pg_tables(pg_conn)


PG_EMBED_VERSION = "99.0"  # dedicated test version for embeddings tests


@pytest.fixture
def clean_pg_embeddings(pg_conn):
    """Bootstrap embeddings schema and clean test rows before/after each test.

    Skips automatically if the pgvector extension is not installed in the database.
    Admin setup (once): run  CREATE EXTENSION vector;  as PostgreSQL superuser.

    When pgvector is absent, the fixture probes whether the current DB role is a
    superuser to produce a precise diagnostic skip message instead of a generic
    "extension not installed" message.
    """
    from pgvector.psycopg2 import register_vector

    from src.db.migrate import _vector_extension_available, run_migrations
    run_migrations(pg_conn)
    if not _vector_extension_available(pg_conn):
        # Probe whether the current role is a superuser so we can explain WHY
        # the extension is missing — lack of superuser is the most common cause.
        try:
            with pg_conn.cursor() as _cur:
                _cur.execute("SELECT pg_catalog.current_setting('is_superuser')")
                _is_super = (_cur.fetchone() or ("off",))[0].lower() in ("on", "true", "1")
        except Exception:
            _is_super = None  # cannot determine — give generic message
        if _is_super is False:
            pytest.skip(
                "pgvector extension not installed and the current DB role is NOT a "
                "superuser — CREATE EXTENSION vector requires superuser. "
                "Fix: connect as superuser and run: CREATE EXTENSION IF NOT EXISTS vector; "
                "then re-run the tests. "
                "Alternatively set PG_ADMIN_DSN to a superuser DSN so the ephemeral-DB "
                "fixture can install the extension during setup."
            )
        pytest.skip(
            "pgvector extension not installed — run as superuser: CREATE EXTENSION vector; "
            "then re-run migrations. "
            "(Could not determine current role privilege level.)"
            if _is_super is None
            else "pgvector extension not installed — run as superuser: CREATE EXTENSION vector;"
        )
    register_vector(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (PG_EMBED_VERSION,))
    yield pg_conn
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (PG_EMBED_VERSION,))


# ---------------------------------------------------------------------------
# Browser test infrastructure (Playwright + uvicorn in-process server)
# ---------------------------------------------------------------------------

WEBUI_TEST_PORT = 8099  # Separate from production port 8003


class _UvicornThread(threading.Thread):
    """Run uvicorn in a daemon thread so the main pytest thread keeps control."""

    def __init__(self, app, port: int):
        super().__init__(daemon=True)
        import uvicorn
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical")
        )

    def run(self):
        self.server.run()

    def stop(self):
        self.server.should_exit = True


def _wipe_web_ui_tables(conn) -> None:
    """DELETE all rows from Web UI tables in FK-safe order.

    Each DELETE runs in its own cursor + rollback on failure so a missing table
    doesn't poison the connection state for subsequent DELETEs (which would
    otherwise raise InFailedSqlTransaction).
    """
    for tbl in ("usage_log", "repos", "api_keys", "ssh_key_pairs", "profiles"):
        try:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {tbl}")
            conn.commit()
        except Exception:
            conn.rollback()  # table absent — clear failed-tx state, continue


@pytest.fixture(scope="session")
def web_ui_server(pg_conn):
    """Start Web UI on 127.0.0.1:{WEBUI_TEST_PORT} pointing to test DB.

    Session-scoped: one server instance shared across all browser tests.
    Sets PG_DSN + FERNET_KEY env vars (read at request-time by _get_conn/_get_fernet).

    Depends on ``pg_conn`` (which already depends on ``_ephemeral_pg_db``).
    The schema is applied by ``_ephemeral_pg_db`` at DB creation time; no
    second ``run_migrations`` call is needed here.
    """
    from cryptography.fernet import Fernet

    from src.web_ui.app import create_app

    # PG_DSN read by _get_conn() via os.getenv() at each request — set before first request.
    # PG_TEST_DSN is written by _ephemeral_pg_db before pg_conn is yielded, so it
    # is non-None by the time this fixture runs.
    os.environ["PG_DSN"] = PG_TEST_DSN
    # FERNET_KEY required for SSH key routes
    if not os.environ.get("FERNET_KEY"):
        os.environ["FERNET_KEY"] = Fernet.generate_key().decode()

    app = create_app()
    srv = _UvicornThread(app, port=WEBUI_TEST_PORT)
    srv.start()

    base_url = f"http://127.0.0.1:{WEBUI_TEST_PORT}"
    for _ in range(30):
        try:
            urllib.request.urlopen(f"{base_url}/", timeout=0.5)
            break
        except Exception:
            time.sleep(0.1)

    yield base_url

    srv.stop()
    srv.join(timeout=3)


@pytest.fixture
def clean_browser(pg_conn):
    """Ensure migrated schema + empty tables before/after each browser test.

    Calls run_migrations() so tables exist even if a previous test dropped them
    via clean_pg. Yields pg_conn for direct DB assertions in browser tests.
    """
    from src.db.migrate import run_migrations

    run_migrations(pg_conn)
    _wipe_web_ui_tables(pg_conn)
    yield pg_conn
    _wipe_web_ui_tables(pg_conn)
