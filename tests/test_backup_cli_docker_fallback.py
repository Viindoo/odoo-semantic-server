# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_backup_cli_docker_fallback.py
"""Unit tests for _resolve_postgres_tool — docker exec fallback when pg_dump/psql absent."""
from unittest.mock import patch

from src.cli import _resolve_postgres_tool


def test_uses_local_pg_dump_when_available():
    """If pg_dump is on PATH, return bare command list without docker prefix."""
    with patch("src.cli.shutil.which", return_value="/usr/bin/pg_dump"):
        result = _resolve_postgres_tool("pg_dump")
    assert result == ["pg_dump"]


def test_falls_back_to_docker_exec_when_local_missing():
    """If pg_dump is not on PATH, fall back to docker exec with -e PGPASSWORD forwarding."""
    default_container = "odoo-semantic-mcp-postgres-1"
    with patch("src.cli.shutil.which", return_value=None):
        result = _resolve_postgres_tool("pg_dump")
    assert result == ["docker", "exec", "-i", "-e", "PGPASSWORD", default_container, "pg_dump"]


def test_falls_back_to_docker_exec_for_psql():
    """If psql is not on PATH, fall back to docker exec with -e PGPASSWORD forwarding."""
    default_container = "odoo-semantic-mcp-postgres-1"
    with patch("src.cli.shutil.which", return_value=None):
        result = _resolve_postgres_tool("psql")
    assert result == ["docker", "exec", "-i", "-e", "PGPASSWORD", default_container, "psql"]


def test_respects_postgres_container_env_override(monkeypatch):
    """POSTGRES_CONTAINER env var overrides the default container name."""
    monkeypatch.setenv("POSTGRES_CONTAINER", "my-custom-postgres")
    with patch("src.cli.shutil.which", return_value=None):
        result = _resolve_postgres_tool("pg_dump")
    assert result == ["docker", "exec", "-i", "-e", "PGPASSWORD", "my-custom-postgres", "pg_dump"]


def test_docker_exec_includes_pgpassword_env_forward():
    """Docker exec fallback must include -e PGPASSWORD so password is forwarded into container."""
    with patch("src.cli.shutil.which", return_value=None):
        result = _resolve_postgres_tool("pg_dump")
    # Verify -e PGPASSWORD appears before the container name
    assert "-e" in result
    e_idx = result.index("-e")
    assert result[e_idx + 1] == "PGPASSWORD", "PGPASSWORD must immediately follow -e"
    # Container name and tool follow
    assert result[-1] == "pg_dump"
    assert result[-2] == "odoo-semantic-mcp-postgres-1"
