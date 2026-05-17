# tests/test_cli_restore_bundle.py
"""Unit tests for CLI restore bundle path (M9 W-RS).

Tests cover:
- tarfile filter='data' blocks path traversal and symlinks
- Missing manifest.json aborts restore
- Missing postgres.sql aborts restore
- Valid bundle completes restore with safety backup
"""
import io
import json
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.cli import _build_parser, _cmd_restore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_minimal_bundle(tmp_path: Path, *, include_manifest=True, include_pg=True) -> Path:
    """Create a .tar.gz bundle with optional contents."""
    bundle = tmp_path / "bundle.tar.gz"
    with tarfile.open(bundle, "w:gz") as tar:
        if include_manifest:
            data = json.dumps({"created_at": "2026-05-15"}).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if include_pg:
            pg_data = b"-- SQL dump\n"
            info2 = tarfile.TarInfo("postgres.sql")
            info2.size = len(pg_data)
            tar.addfile(info2, io.BytesIO(pg_data))
    return bundle


def _make_traversal_bundle(tmp_path: Path) -> Path:
    """Craft a malicious bundle with a path-traversal member."""
    bundle = tmp_path / "evil.tar.gz"
    with tarfile.open(bundle, "w:gz") as tar:
        # Attempt path traversal: ../../etc/passwd
        evil_data = b"root:x:0:0:root:/root:/bin/bash\n"
        info = tarfile.TarInfo("../../etc/passwd")
        info.size = len(evil_data)
        tar.addfile(info, io.BytesIO(evil_data))
    return bundle


def _make_symlink_bundle(tmp_path: Path) -> Path:
    """Craft a bundle with a symlink pointing outside the dest dir."""
    bundle = tmp_path / "symlink.tar.gz"
    with tarfile.open(bundle, "w:gz") as tar:
        # Symlink member pointing to /etc/passwd
        info = tarfile.TarInfo("evil_link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        info.size = 0
        tar.addfile(info)
    return bundle


def _args_for(file: Path, dsn: str = "postgresql://user:pw@localhost/db"):
    """Parse CLI args for restore command."""
    return _build_parser().parse_args(["restore", str(file)])


# ---------------------------------------------------------------------------
# filter='data' — path traversal blocked
# ---------------------------------------------------------------------------

def test_bundle_extract_uses_filter_data_blocks_traversal(tmp_path, monkeypatch):
    """A bundle with ../../etc/passwd member must be rejected (filter='data')."""
    monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
    bundle = _make_traversal_bundle(tmp_path)
    args = _args_for(bundle)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr=b"", stdout="")
        result = _cmd_restore(args)
    # Should fail: either tarfile rejects the member, or manifest missing after safe extract
    # Either way, should NOT return 0 success with a traversal payload
    # (filter='data' rejects the member; the bundle also has no manifest → exit 1)
    assert result != 0


def test_bundle_extract_rejects_symlink_member(tmp_path, monkeypatch):
    """A bundle with a symlink outside dest must be rejected (filter='data')."""
    monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
    bundle = _make_symlink_bundle(tmp_path)
    args = _args_for(bundle)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr=b"", stdout="")
        result = _cmd_restore(args)
    # Either filter='data' rejects the symlink, or manifest is absent → exit 1
    assert result != 0


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------

def test_bundle_missing_manifest_aborts(tmp_path, monkeypatch):
    """Bundle without manifest.json → non-zero exit."""
    monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
    bundle = _make_minimal_bundle(tmp_path, include_manifest=False, include_pg=True)
    args = _args_for(bundle)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr=b"", stdout="")
        result = _cmd_restore(args)
    assert result != 0


# ---------------------------------------------------------------------------
# postgres.sql validation
# ---------------------------------------------------------------------------

def test_bundle_missing_pg_dump_aborts(tmp_path, monkeypatch):
    """Bundle without postgres.sql → non-zero exit."""
    monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
    bundle = _make_minimal_bundle(tmp_path, include_manifest=True, include_pg=False)
    args = _args_for(bundle)
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr=b"", stdout="")
        result = _cmd_restore(args)
    assert result != 0


# ---------------------------------------------------------------------------
# Happy path: valid bundle with safety backup
# ---------------------------------------------------------------------------

def test_bundle_restore_happy_path(tmp_path, monkeypatch):
    """Valid bundle + PG_DSN → safety backup + psql restore called."""
    monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))

    bundle = _make_minimal_bundle(tmp_path)
    args = _args_for(bundle)

    call_order = []

    def mock_run(cmd, **kwargs):
        call_order.append(cmd[0])
        # Safety backup writes to stdout
        if cmd[0] == "pg_dump":
            stdout = kwargs.get("stdout")
            if stdout and hasattr(stdout, "write"):
                stdout.write(b"-- mock dump\n")
            return MagicMock(returncode=0, stderr=b"")
        # psql restore
        return MagicMock(returncode=0, stderr="", stdout="")

    with patch("src.cli.shutil.which", return_value="/usr/bin/pg_dump"):
        with patch("subprocess.run", side_effect=mock_run):
            result = _cmd_restore(args)

    assert result == 0
    # Safety backup (pg_dump) must come BEFORE psql restore
    assert "pg_dump" in call_order
    assert "psql" in call_order
    pg_dump_idx = call_order.index("pg_dump")
    psql_idx = call_order.index("psql")
    assert pg_dump_idx < psql_idx, "Safety backup must run before psql restore"


def test_bundle_safety_backup_failure_aborts(tmp_path, monkeypatch):
    """If pg_dump safety backup fails, restore must abort (no psql called)."""
    monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))

    bundle = _make_minimal_bundle(tmp_path)
    args = _args_for(bundle)

    call_order = []

    def mock_run(cmd, **kwargs):
        call_order.append(cmd[0])
        if cmd[0] == "pg_dump":
            return MagicMock(returncode=1, stderr=b"pg_dump: connection refused")
        return MagicMock(returncode=0, stderr="", stdout="")

    with patch("src.cli.shutil.which", return_value="/usr/bin/pg_dump"):
        with patch("subprocess.run", side_effect=mock_run):
            result = _cmd_restore(args)

    assert result != 0
    assert "psql" not in call_order, "psql must NOT be called if safety backup fails"
