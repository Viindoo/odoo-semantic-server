# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the backup pre-check that skips gracefully when PG is down.

Incident 2026-05-19: backup unit was failing loud with
`psycopg2.OperationalError: connection refused` because postgres container
was Exited (127), masking the true upstream cause and noisy-paging ops.
The new behaviour: exit 0 with a SKIPPED line on stderr, log a WARNING.
"""
import argparse

import pytest


@pytest.fixture
def _backup_args(tmp_path):
    """Minimal argparse Namespace acceptable to _cmd_backup."""
    return argparse.Namespace(
        output=str(tmp_path / "osm-test.tar.gz"),
        bundle_passphrase_env="",
    )


def _stub_pg_dsn(monkeypatch):
    """Make _get_pg_dsn() return a non-empty value so we get past the early check."""
    from src import cli as cli_mod

    monkeypatch.setattr(
        cli_mod, "_get_pg_dsn", lambda: "postgresql://odoo_semantic:pw@127.0.0.1:5432/odoo_semantic",
    )


def test_backup_skips_gracefully_when_container_not_running(
    monkeypatch, _backup_args, tmp_path, caplog,
):
    """Container reported as not running → exit 0, no psycopg2 connect attempted."""
    from src import cli as cli_mod

    _stub_pg_dsn(monkeypatch)

    # Ensure BACKUP_DIR validation passes (output must be under it).
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))

    # Simulate `docker inspect` returning State.Running=false.
    def _fake_run(cmd, *args, **kwargs):
        class _R:
            returncode = 0
            stdout = "false\n"
            stderr = ""
        assert cmd[0:2] == ["docker", "inspect"], f"unexpected subprocess call: {cmd}"
        return _R()

    monkeypatch.setattr(cli_mod.subprocess, "run", _fake_run)

    # If anything tries to open a real PG connection, the test fails — the
    # pre-check is supposed to short-circuit before this point.
    def _fail_connect(*a, **kw):
        raise AssertionError("psycopg2.connect must NOT be called when container not running")

    import psycopg2 as _psycopg2
    monkeypatch.setattr(_psycopg2, "connect", _fail_connect)

    with caplog.at_level("WARNING"):
        rc = cli_mod._cmd_backup(_backup_args)

    assert rc == 0
    assert any("skipped" in r.message.lower() for r in caplog.records)


def test_backup_short_circuits_on_psycopg_operational_error(
    monkeypatch, _backup_args, tmp_path, caplog,
):
    """Container check inconclusive (docker absent) but psycopg2 connect fails →
    still exit 0 with SKIPPED, NOT a Python traceback."""
    import psycopg2

    from src import cli as cli_mod

    _stub_pg_dsn(monkeypatch)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))

    # Simulate docker not in PATH so _is_pg_container_running returns None.
    def _no_docker(cmd, *args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(cli_mod.subprocess, "run", _no_docker)

    # psycopg2.connect raises OperationalError — exactly the failure mode the
    # incident produced 11k times.
    def _fail_connect(*a, **kw):
        raise psycopg2.OperationalError("connection to server failed: Connection refused")

    monkeypatch.setattr(psycopg2, "connect", _fail_connect)

    with caplog.at_level("WARNING"):
        rc = cli_mod._cmd_backup(_backup_args)

    assert rc == 0  # skip-gracefully, not exit 1
    assert any("connection failed" in r.message.lower() for r in caplog.records)


def test_is_pg_container_running_handles_missing_docker(monkeypatch):
    """_is_pg_container_running returns None (not False) when docker is absent."""
    from src import cli as cli_mod

    def _missing(*a, **kw):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(cli_mod.subprocess, "run", _missing)
    assert cli_mod._is_pg_container_running() is None


def test_is_pg_container_running_handles_unknown_container(monkeypatch):
    """`docker inspect` exits non-zero (container missing) → None, not False."""
    from src import cli as cli_mod

    def _unknown(cmd, *a, **kw):
        class _R:
            returncode = 1
            stdout = ""
            stderr = "Error: No such object: odoo-semantic-mcp-postgres-1\n"
        return _R()

    monkeypatch.setattr(cli_mod.subprocess, "run", _unknown)
    assert cli_mod._is_pg_container_running() is None


def test_is_pg_container_running_true_when_state_running_true(monkeypatch):
    from src import cli as cli_mod

    def _ok(cmd, *a, **kw):
        class _R:
            returncode = 0
            stdout = "true\n"
            stderr = ""
        return _R()

    monkeypatch.setattr(cli_mod.subprocess, "run", _ok)
    assert cli_mod._is_pg_container_running() is True
