# tests/conftest.py
import os
import subprocess
from pathlib import Path

import pytest
from neo4j import GraphDatabase

NEO4J_URI = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_TEST_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_TEST_PASSWORD", "password")
TEST_VERSION = "99.0"  # version đặc biệt chỉ dùng cho tests, tránh conflict với data thật

# Canonical version defined in .env.example (NEO4J_IMAGE=...).
# CI loads .env.example before running tests; local dev copies .env.example → .env.
_NEO4J_IMAGE = os.getenv("NEO4J_IMAGE", "neo4j:5.26.25")


@pytest.fixture(scope="session")
def neo4j_driver():
    """
    Kết nối Neo4j cho toàn bộ test session.

    CI (CI=true): kết nối trực tiếp tới NEO4J_TEST_URI — service container đã sẵn sàng,
                  không import testcontainers để tránh @wait_container_is_ready warning.
    Local dev:    Ưu tiên 1: testcontainers (tự spin up Docker container).
                  Ưu tiên 2: kết nối trực tiếp tới NEO4J_TEST_URI.
                  Fallback:  skip với lý do cụ thể.
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

    # --- Ưu tiên 1: testcontainers (yêu cầu Docker daemon đang chạy) ---
    try:
        container = _Neo4jContainer(_NEO4J_IMAGE).waiting_for(
            LogMessageWaitStrategy("Remote interface available at")
        )
        container.start()
        bolt_url = container.get_connection_url()
        driver = GraphDatabase.driver(bolt_url, auth=("neo4j", "password"))
        driver.verify_connectivity()
        # Expose cho các fixture tạo connection riêng (writer, mcp_tools)
        os.environ["NEO4J_TEST_URI"] = bolt_url
        os.environ["NEO4J_TEST_USER"] = "neo4j"
        os.environ["NEO4J_TEST_PASSWORD"] = "password"
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

    # --- Ưu tiên 2: Neo4j đang chạy sẵn (docker compose up -d neo4j) ---
    if driver is None:
        bolt_driver = None
        try:
            bolt_driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
            bolt_driver.verify_connectivity()
            driver = bolt_driver
        except Exception as bolt_error:
            if bolt_driver is not None:
                bolt_driver.close()
            lines = ["[FIX] Cài Docker + start daemon"
                     " → testcontainers tự spin up Neo4j khi test chạy"]
            tc_msg = (
                f"  testcontainers lỗi: {tc_error}" if tc_error
                else "  testcontainers: không thử được"
            )
            lines.append(tc_msg)
            lines.append(f"  bolt ({NEO4J_URI}) lỗi: {bolt_error}")
            lines.append("  Hoặc chạy thủ công: make neo4j-up")
            pytest.skip("\n".join(lines))

    yield driver

    driver.close()
    if container is not None:
        container.stop()


@pytest.fixture
def clean_neo4j(neo4j_driver):
    """Xóa tất cả nodes có odoo_version=TEST_VERSION trước và sau mỗi test."""
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=TEST_VERSION)
    yield neo4j_driver
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=TEST_VERSION)


@pytest.fixture
def tmp_git_repo(tmp_path):
    """Tạo một git repo tạm thời với branch 17.0 để test scanner."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "checkout", "-b", "17.0"],
        check=True, capture_output=True,
    )
    return tmp_path


def make_git_repo(path: Path, branch: str) -> Path:
    """Tạo git repo tại path với branch đã cho. Dùng trong tests."""
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
    """Tạo __manifest__.py trong module_dir. Dùng trong tests."""
    module_dir.mkdir(parents=True, exist_ok=True)
    (module_dir / "__manifest__.py").write_text(
        f"{{'name': {name!r}, 'version': {version!r}, "
        f"'depends': {depends!r}, 'installable': {installable!r}}}\n"
    )
