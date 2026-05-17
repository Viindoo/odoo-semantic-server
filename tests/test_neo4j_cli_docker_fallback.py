# tests/test_neo4j_cli_docker_fallback.py
"""Unit tests for _resolve_neo4j_tool — docker exec fallback when neo4j-admin absent.

Parallel to test_backup_cli_docker_fallback.py which covers _resolve_postgres_tool.
In typical Docker-Compose deployments neo4j-admin is only available inside the
Neo4j container; this helper lets the backup CLI fall back to docker exec transparently.
"""
from unittest.mock import patch

from src.cli import _resolve_neo4j_tool


def test_uses_local_neo4j_admin_when_available():
    """If neo4j-admin is on PATH, return bare command list without docker prefix."""
    with patch("src.cli.shutil.which", return_value="/usr/bin/neo4j-admin"):
        result = _resolve_neo4j_tool("neo4j-admin")
    assert result == ["neo4j-admin"]


def test_falls_back_to_docker_exec_when_local_missing():
    """If neo4j-admin is not on PATH, fall back to docker exec with default container."""
    default_container = "odoo-semantic-mcp-neo4j-1"
    with patch("src.cli.shutil.which", return_value=None):
        result = _resolve_neo4j_tool("neo4j-admin")
    assert result == ["docker", "exec", "-i", default_container, "neo4j-admin"]


def test_respects_neo4j_container_env_override(monkeypatch):
    """NEO4J_CONTAINER env var overrides the default container name."""
    monkeypatch.setenv("NEO4J_CONTAINER", "my-custom-neo4j")
    with patch("src.cli.shutil.which", return_value=None):
        result = _resolve_neo4j_tool("neo4j-admin")
    assert result == ["docker", "exec", "-i", "my-custom-neo4j", "neo4j-admin"]


def test_docker_exec_does_not_include_env_forward():
    """Neo4j docker exec must NOT include -e PGPASSWORD (no Postgres credential needed)."""
    with patch("src.cli.shutil.which", return_value=None):
        result = _resolve_neo4j_tool("neo4j-admin")
    # -e PGPASSWORD is Postgres-specific; must not bleed into neo4j exec
    assert "PGPASSWORD" not in result
    assert result[-1] == "neo4j-admin"
    assert result[-2] == "odoo-semantic-mcp-neo4j-1"
