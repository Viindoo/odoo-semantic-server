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
    """Kết nối Neo4j một lần cho toàn bộ test session. Skip nếu Neo4j không available."""
    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        driver.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j không sẵn sàng ({e})")
    yield driver
    driver.close()


@pytest.fixture
def clean_neo4j(neo4j_driver):
    """Xóa tất cả nodes có odoo_version=TEST_VERSION trước và sau mỗi test."""
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=TEST_VERSION)
    yield
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
