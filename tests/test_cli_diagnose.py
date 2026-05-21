# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for `python -m src.cli diagnose`.

Each check is exercised independently by stubbing the underlying subprocess /
urllib call. The diagnose command must:
- Exit 0 when every check is ok OR skipped.
- Exit 1 when any check is fail.
- Emit JSON when --json is passed.
- Never raise — failures are surfaced as `status: fail` entries.
"""
import argparse
import json

import pytest


@pytest.fixture
def _isolated_cwd(tmp_path, monkeypatch):
    """Redirect the diagnose initdb resolver at a tmpdir so the bind-mount-type
    check is deterministic and isolated from the real repo layout.

    Patch `_diagnose_initdb_dir` (function returning Path) rather than relying
    on cwd — that was the bug Issue #2 fixed. Tests still use a per-test
    tmp_path to set up the file/dir scenario.
    """
    from src import cli as _cli

    target = tmp_path / "docker" / "initdb.d"
    monkeypatch.setattr(_cli, "_diagnose_initdb_dir", lambda: target)
    return tmp_path


def _stub_unreachable_mcp(monkeypatch):
    """Patch urllib.request.urlopen to raise URLError — MCP /health unreachable."""
    import urllib.error
    import urllib.request

    def _no_server(url, *a, **kw):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _no_server)


def _stub_healthy_mcp(monkeypatch, status: str = "ok", http: int = 200):
    """Patch urlopen with a context manager returning a JSON body."""
    import urllib.request

    class _Resp:
        def __init__(self):
            self.status = http
            self._body = json.dumps({"status": status}).encode()
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())


def test_diagnose_all_ok_returns_zero(_isolated_cwd, monkeypatch):
    """PG running + Neo4j healthy + MCP ok + initdb.d directory present."""
    from src import cli as cli_mod

    # initdb.d as a directory
    (_isolated_cwd / "docker" / "initdb.d").mkdir(parents=True)

    # docker inspect: both PG (running=true) and Neo4j (Health=healthy)
    def _fake_run(cmd, *a, **kw):
        class _R:
            returncode = 0
            stderr = ""
        r = _R()
        if "postgres" in " ".join(cmd):
            r.stdout = "true\n"
        else:
            r.stdout = "healthy\n"
        return r

    monkeypatch.setattr(cli_mod.subprocess, "run", _fake_run)
    _stub_healthy_mcp(monkeypatch)

    rc = cli_mod._cmd_diagnose(argparse.Namespace(json=False))
    assert rc == 0


def test_diagnose_fails_when_pg_not_running(_isolated_cwd, monkeypatch, capsys):
    from src import cli as cli_mod

    (_isolated_cwd / "docker" / "initdb.d").mkdir(parents=True)

    def _fake_run(cmd, *a, **kw):
        class _R:
            returncode = 0
            stderr = ""
        r = _R()
        if "postgres" in " ".join(cmd):
            r.stdout = "false\n"
        else:
            r.stdout = "healthy\n"
        return r

    monkeypatch.setattr(cli_mod.subprocess, "run", _fake_run)
    _stub_healthy_mcp(monkeypatch)

    rc = cli_mod._cmd_diagnose(argparse.Namespace(json=False))
    out = capsys.readouterr().out
    assert rc == 1
    assert "pg_container_running" in out
    assert "✗" in out  # fail symbol present


def test_diagnose_fails_when_initdb_is_a_file_not_dir(_isolated_cwd, monkeypatch):
    """Regression guard for the 2026-05-19 root cause — bind-mount source got
    auto-created as the WRONG type (or, originally, a stray empty dir replaced
    a file). The check must catch this."""
    from src import cli as cli_mod

    # Create a FILE where the directory should be.
    (_isolated_cwd / "docker").mkdir()
    (_isolated_cwd / "docker" / "initdb.d").write_text("oops not a dir")

    def _docker_absent(*a, **kw):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(cli_mod.subprocess, "run", _docker_absent)
    _stub_unreachable_mcp(monkeypatch)

    rc = cli_mod._cmd_diagnose(argparse.Namespace(json=False))
    assert rc == 1  # at least the mount-type check failed


def test_diagnose_skips_when_docker_absent(_isolated_cwd, monkeypatch):
    """Without docker we cannot inspect containers — that is `skipped`, not `fail`."""
    from src import cli as cli_mod

    (_isolated_cwd / "docker" / "initdb.d").mkdir(parents=True)

    def _docker_absent(*a, **kw):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(cli_mod.subprocess, "run", _docker_absent)
    _stub_healthy_mcp(monkeypatch)

    rc = cli_mod._cmd_diagnose(argparse.Namespace(json=False))
    # Only MCP /health + initdb mount-type can pass. PG + Neo4j → skipped.
    # No fail entries → rc=0.
    assert rc == 0


def test_diagnose_json_output_is_parseable(_isolated_cwd, monkeypatch, capsys):
    from src import cli as cli_mod

    (_isolated_cwd / "docker" / "initdb.d").mkdir(parents=True)

    def _fake_run(cmd, *a, **kw):
        class _R:
            returncode = 0
            stdout = "true\n" if "postgres" in " ".join(cmd) else "healthy\n"
            stderr = ""
        return _R()

    monkeypatch.setattr(cli_mod.subprocess, "run", _fake_run)
    _stub_healthy_mcp(monkeypatch)

    rc = cli_mod._cmd_diagnose(argparse.Namespace(json=True))
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert "checks" in payload
    assert payload["failures"] == 0
    assert all("check" in c and "status" in c for c in payload["checks"])


def test_diagnose_initdb_dir_anchored_to_file_not_cwd(monkeypatch, tmp_path):
    """Regression guard for Issue #2 (PR #134 review): the initdb path must
    resolve via __file__, NOT against runtime cwd, so the check works when
    diagnose is invoked from systemd (WorkingDirectory=/), cron, etc."""
    from pathlib import Path

    from src import cli as _cli

    # Move to a tmpdir that does NOT have a docker/initdb.d under it.
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / "docker" / "initdb.d").exists()

    resolved = _cli._diagnose_initdb_dir()
    # Must end with the canonical sub-path …
    assert resolved.parts[-2:] == ("docker", "initdb.d")
    # … and must NOT live under the tmp cwd (proves cwd-independence).
    try:
        resolved.relative_to(tmp_path)
        cwd_relative = True
    except ValueError:
        cwd_relative = False
    assert cwd_relative is False, (
        f"initdb dir resolved relative to cwd ({tmp_path}) — fix Issue #2 again. "
        f"resolved={resolved}"
    )
    # Bonus: the resolved path must be under the repo (src/cli.py's grandparent).
    expected_root = Path(_cli.__file__).resolve().parent.parent
    assert str(resolved).startswith(str(expected_root)), (
        f"resolved={resolved} not under {expected_root}"
    )
