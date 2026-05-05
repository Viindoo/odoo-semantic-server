# tests/conftest.py
import os
import subprocess
import pytest
from pathlib import Path
from neo4j import GraphDatabase

NEO4J_URI = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_TEST_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_TEST_PASSWORD", "password")
TEST_VERSION = "99.0"  # version đặc biệt chỉ dùng cho tests, tránh conflict với data thật


@pytest.fixture(scope="session")
def neo4j_driver():
    """
    Kết nối Neo4j cho toàn bộ test session.

    Ưu tiên 1: testcontainers — tự spin up Docker container, không cần setup thủ công.
    Ưu tiên 2: kết nối trực tiếp tới NEO4J_TEST_URI (Neo4j đang chạy sẵn).
    Fallback:  skip toàn bộ neo4j tests với lý do cụ thể từng tầng.
    """
    container = None
    driver = None
    tc_error = None

    # --- Ưu tiên 1: testcontainers (yêu cầu Docker daemon đang chạy) ---
    try:
        from testcontainers.neo4j import Neo4jContainer
        container = Neo4jContainer("neo4j:5")
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
            lines = ["Neo4j không sẵn sàng. Lý do:"]
            if tc_error is not None:
                lines.append(f"  • testcontainers: {tc_error}")
            lines.append(f"  • bolt trực tiếp ({NEO4J_URI}): {bolt_error}")
            lines.append("Để chạy integration tests:")
            lines.append("  1. Cài Docker và đảm bảo Docker daemon đang chạy")
            lines.append("     (testcontainers sẽ tự spin up Neo4j)")
            lines.append("  2. Hoặc: make neo4j-up  (giữ Neo4j chạy thủ công)")
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
