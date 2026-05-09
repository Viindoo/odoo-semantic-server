# tests/test_cli.py
"""Unit tests for src.cli — backup, restore, rotate-fernet commands."""
import os
from unittest.mock import MagicMock, patch

import pytest

from src.cli import _build_parser, _cmd_backup, _cmd_restore, _cmd_rotate_fernet


def test_backup_calls_pg_dump(monkeypatch, tmp_path):
    monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
    out = tmp_path / "dump.sql"
    args = _build_parser().parse_args(["backup", "--output", str(out)])
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        result = _cmd_backup(args)
    assert result == 0
    called_cmd = mock_run.call_args[0][0]
    assert "pg_dump" in called_cmd[0]
    assert str(out) in called_cmd


def test_backup_missing_dsn(monkeypatch, tmp_path):
    monkeypatch.delenv("PG_DSN", raising=False)
    # Patch config.get to return None
    with patch("src.cli._get_pg_dsn", return_value=""):
        args = _build_parser().parse_args(["backup", "--output", str(tmp_path / "dump.sql")])
        result = _cmd_backup(args)
    assert result == 1


def test_backup_pg_dump_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
    out = tmp_path / "dump.sql"
    args = _build_parser().parse_args(["backup", "--output", str(out)])
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stderr="connection refused")
        result = _cmd_backup(args)
    assert result == 1


def test_restore_file_not_found(tmp_path):
    args = _build_parser().parse_args(["restore", str(tmp_path / "nonexistent.sql")])
    result = _cmd_restore(args)
    assert result == 1


def test_restore_calls_psql(monkeypatch, tmp_path):
    monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
    dump = tmp_path / "dump.sql"
    dump.write_text("-- SQL dump")
    args = _build_parser().parse_args(["restore", str(dump)])
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        result = _cmd_restore(args)
    assert result == 0
    called_cmd = mock_run.call_args[0][0]
    assert "psql" in called_cmd[0]


def test_restore_psql_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
    dump = tmp_path / "dump.sql"
    dump.write_text("-- bad SQL")
    args = _build_parser().parse_args(["restore", str(dump)])
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stderr="syntax error")
        result = _cmd_restore(args)
    assert result == 1


def test_rotate_fernet_re_encrypts_rows():
    from cryptography.fernet import Fernet

    old_key = Fernet.generate_key()
    new_key = Fernet.generate_key()
    old_f = Fernet(old_key)
    plaintext = b"my-private-key"
    encrypted = old_f.encrypt(plaintext).decode()

    mock_cursor = MagicMock()
    mock_cursor.__enter__ = lambda s: s
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.fetchall.return_value = [(1, encrypted)]

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    args = _build_parser().parse_args(
        ["rotate-fernet", "--old-key", old_key.decode(), "--new-key", new_key.decode()]
    )
    with patch("psycopg2.connect", return_value=mock_conn):
        with patch("src.cli._get_pg_dsn", return_value="postgresql://user:pw@localhost/db"):
            result = _cmd_rotate_fernet(args)
    assert result == 0
    # Verify UPDATE was called
    assert mock_cursor.execute.called


def test_rotate_fernet_invalid_key():
    args = _build_parser().parse_args(
        ["rotate-fernet", "--old-key", "not-a-key", "--new-key", "also-bad"]
    )
    result = _cmd_rotate_fernet(args)
    assert result == 1


def test_rotate_fernet_skips_undecryptable_row():
    from cryptography.fernet import Fernet

    old_key = Fernet.generate_key()
    new_key = Fernet.generate_key()
    # Encrypt with a DIFFERENT key so old_key cannot decrypt it
    other_key = Fernet.generate_key()
    other_f = Fernet(other_key)
    bad_encrypted = other_f.encrypt(b"secret").decode()

    mock_cursor = MagicMock()
    mock_cursor.__enter__ = lambda s: s
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.fetchall.return_value = [(1, bad_encrypted)]

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    args = _build_parser().parse_args(
        ["rotate-fernet", "--old-key", old_key.decode(), "--new-key", new_key.decode()]
    )
    with patch("psycopg2.connect", return_value=mock_conn):
        with patch("src.cli._get_pg_dsn", return_value="postgresql://user:pw@localhost/db"):
            result = _cmd_rotate_fernet(args)
    # Should still return 0 (warnings printed, but not fatal)
    assert result == 0
