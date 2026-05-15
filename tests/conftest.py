# tests/conftest.py
import os
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from neo4j import GraphDatabase

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
    }
    if fname in real_auth_flow_files:
        # Defensive: scrub any leaked bypass env from prior tests in the session.
        monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)
        return
    monkeypatch.setenv("WEBUI_AUTH_DISABLED", "1")


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


# --- PostgreSQL fixtures (for src/db tests) ---

PG_TEST_DSN = os.getenv(
    "PG_TEST_DSN",
    "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic",
)


@pytest.fixture(scope="session")
def pg_conn():
    """Session-scoped PostgreSQL connection. Skips if not reachable."""
    import psycopg2
    try:
        conn = psycopg2.connect(PG_TEST_DSN)
    except Exception as e:
        pytest.skip(f"PostgreSQL not reachable at {PG_TEST_DSN}: {e}")
    conn.autocommit = True
    # Initialize centralized pool so store accessors (auth_store, repo_store, etc.) work in tests
    from src.db.pg import init_pool
    init_pool(PG_TEST_DSN, min_conn=1, max_conn=3)
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
    conn.close()


@pytest.fixture
def clean_pg(pg_conn):
    """Drop test schema tables + yoyo state before and after each test (idempotent).

    yoyo tracks applied migrations in _yoyo_migration.  Leaving this table
    intact between tests causes run_migrations() to report '0 pending' while
    the schema tables (dropped earlier) are absent — producing confusing test
    failures.  Both the schema tables and yoyo internal tables are therefore
    dropped together to guarantee a truly clean starting state.

    Drop order respects FK constraints: dependent tables before referenced ones.
    """
    _all_tables = [
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
        # schema tables in FK-safe order
        "pattern_feedback",
        "indexer_jobs",
        "usage_log",
        "repos",
        "api_keys",
        "ssh_key_pairs",
        "embeddings",
        "profiles",
        "webui_users",
    ]

    def _wipe(conn):
        for tbl in _all_tables:
            with conn.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")

    _wipe(pg_conn)
    yield pg_conn
    _wipe(pg_conn)


PG_EMBED_VERSION = "99.0"  # dedicated test version for embeddings tests


@pytest.fixture
def clean_pg_embeddings(pg_conn):
    """Bootstrap embeddings schema and clean test rows before/after each test.

    Skips automatically if the pgvector extension is not installed in the database.
    Admin setup (once): run  CREATE EXTENSION vector;  as PostgreSQL superuser.
    """
    from pgvector.psycopg2 import register_vector

    from src.db.migrate import _vector_extension_available, run_migrations
    run_migrations(pg_conn)
    if not _vector_extension_available(pg_conn):
        pytest.skip("pgvector extension not installed — run as superuser: CREATE EXTENSION vector;")
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
    """
    from cryptography.fernet import Fernet

    from src.db.migrate import run_migrations
    from src.web_ui.app import create_app

    # Bootstrap schema once at session start
    run_migrations(pg_conn)

    # PG_DSN read by _get_conn() via os.getenv() at each request — set before first request
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
