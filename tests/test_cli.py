# tests/test_cli.py
"""Unit tests for src.cli — backup, restore, rotate-fernet commands."""
from unittest.mock import MagicMock, patch

from src.cli import (
    _build_parser,
    _cmd_backup,
    _cmd_restore,
    _cmd_rotate_fernet,
    _dsn_to_pg_args_and_env,
)


class TestDsnParsing:
    """Tests for _dsn_to_pg_args_and_env() — ensure password never leaks to argv."""

    def test_url_dsn_with_password(self):
        """PostgreSQL URL with password → password in env, not argv."""
        argv, env = _dsn_to_pg_args_and_env("postgresql://user:secret@localhost:5432/mydb")
        joined_cmd = " ".join(argv)

        # Password must NOT be in argv
        assert "secret" not in joined_cmd
        assert "secret" not in str(argv)

        # Password must be in env
        assert env.get("PGPASSWORD") == "secret"

        # All components must be parsed
        assert "--host" in argv and "localhost" in argv
        assert "--port" in argv and "5432" in argv
        assert "--username" in argv and "user" in argv
        assert "--dbname" in argv and "mydb" in argv

    def test_url_dsn_no_password(self):
        """PostgreSQL URL without password → no PGPASSWORD in env."""
        argv, env = _dsn_to_pg_args_and_env("postgresql://user@localhost/mydb")

        # No password in env
        assert "PGPASSWORD" not in env

        # User and dbname still present
        assert "--username" in argv and "user" in argv
        assert "--dbname" in argv and "mydb" in argv

    def test_url_dsn_special_chars_in_password(self):
        """Password with special characters (URL-encoded) → decoded correctly."""
        # @ symbol in password should be URL-encoded as %40
        argv, env = _dsn_to_pg_args_and_env("postgresql://user:p%40ss@localhost/db")
        assert env.get("PGPASSWORD") == "p@ss"
        assert "p@ss" not in " ".join(argv)

    def test_keyword_dsn_with_password(self):
        """Keyword form DSN with password → parsed correctly."""
        argv, env = _dsn_to_pg_args_and_env(
            "host=localhost port=5432 user=myuser password=mysecret dbname=mydb"
        )
        joined_cmd = " ".join(argv)

        # Password must NOT be in argv
        assert "mysecret" not in joined_cmd
        assert "mysecret" not in str(argv)

        # Password in env
        assert env.get("PGPASSWORD") == "mysecret"

        # All components parsed
        assert "--host" in argv and "localhost" in argv
        assert "--port" in argv and "5432" in argv
        assert "--username" in argv and "myuser" in argv
        assert "--dbname" in argv and "mydb" in argv

    def test_keyword_dsn_no_password(self):
        """Keyword DSN without password → no PGPASSWORD in env."""
        argv, env = _dsn_to_pg_args_and_env("host=localhost user=myuser dbname=mydb")
        assert "PGPASSWORD" not in env
        assert "--host" in argv and "--username" in argv and "--dbname" in argv

    def test_invalid_dsn_format(self):
        """Unrecognized DSN format → raises ValueError."""
        import pytest

        with pytest.raises(ValueError, match="Unrecognized PostgreSQL DSN format"):
            _dsn_to_pg_args_and_env("invalid-dsn-format")

    def test_empty_dsn(self):
        """Empty DSN → raises ValueError."""
        import pytest

        with pytest.raises(ValueError):
            _dsn_to_pg_args_and_env("")


def test_backup_calls_pg_dump(monkeypatch, tmp_path):
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
    monkeypatch.setenv("BACKUP_DIR", str(backup_dir))
    out = backup_dir / "dump.tar.gz"
    args = _build_parser().parse_args(["backup", "--output", str(out)])

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = lambda s: s
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.fetchone.return_value = (True,)
    mock_conn.cursor.return_value = mock_cursor

    def _fake_run(cmd, **kwargs):
        if "pg_dump" in cmd[0]:
            out_idx = cmd.index("-f") + 1
            from pathlib import Path
            Path(cmd[out_idx]).write_text("-- stub\n")
            return MagicMock(returncode=0, stderr="")
        return MagicMock(returncode=0, stderr="")

    with patch("psycopg2.connect", return_value=mock_conn):
        with patch("subprocess.run", side_effect=_fake_run) as mock_run:
            result = _cmd_backup(args)
    assert result == 0
    # Verify pg_dump was called with password in env, not argv
    pg_calls = [c for c in mock_run.call_args_list if "pg_dump" in c[0][0][0]]
    assert pg_calls, "pg_dump was not called"
    called_cmd = pg_calls[0][0][0]
    assert "pg_dump" in called_cmd[0]
    # Password must NOT be in argv
    assert "pw" not in " ".join(called_cmd)
    # Password must be in env
    called_env = pg_calls[0][1].get("env", {})
    assert called_env.get("PGPASSWORD") == "pw"


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
    # Password must NOT be in argv
    assert "pw" not in " ".join(called_cmd)
    # Password must be in env
    called_env = mock_run.call_args[1].get("env", {})
    assert called_env.get("PGPASSWORD") == "pw"


def test_restore_psql_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("PG_DSN", "postgresql://user:pw@localhost/db")
    dump = tmp_path / "dump.sql"
    dump.write_text("-- bad SQL")
    args = _build_parser().parse_args(["restore", str(dump)])
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stderr="syntax error")
        result = _cmd_restore(args)
    assert result == 1


def test_rotate_fernet_re_encrypts_rows(monkeypatch):
    from cryptography.fernet import Fernet

    old_key = Fernet.generate_key()
    new_key = Fernet.generate_key()
    old_f = Fernet(old_key)
    plaintext = b"my-private-key"
    encrypted = old_f.encrypt(plaintext).decode()

    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [(1, encrypted)]

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    monkeypatch.setenv("OLD_FERNET_KEY", old_key.decode())
    monkeypatch.setenv("NEW_FERNET_KEY", new_key.decode())

    args = _build_parser().parse_args(["rotate-fernet"])
    with patch("psycopg2.connect", return_value=mock_conn):
        with patch("src.cli._get_pg_dsn", return_value="postgresql://user:pw@localhost/db"):
            result = _cmd_rotate_fernet(args)
    assert result == 0
    # Verify UPDATE was called (among the cursor execute calls)
    update_calls = [c for c in mock_cursor.execute.call_args_list if "UPDATE" in str(c)]
    assert update_calls, "Expected at least one UPDATE call on successful rotation"


def test_rotate_fernet_invalid_key(monkeypatch):
    monkeypatch.setenv("OLD_FERNET_KEY", "not-a-key")
    monkeypatch.setenv("NEW_FERNET_KEY", "also-bad")
    args = _build_parser().parse_args(["rotate-fernet"])
    with patch("src.cli._get_pg_dsn", return_value="postgresql://user:pw@localhost/db"):
        result = _cmd_rotate_fernet(args)
    assert result == 1


def test_rotate_fernet_aborts_on_undecryptable_row(monkeypatch):
    """Since M9 W-FE atomic rotation: undecryptable row causes SystemExit(2), not skip."""
    import pytest
    from cryptography.fernet import Fernet

    old_key = Fernet.generate_key()
    new_key = Fernet.generate_key()
    # Encrypt with a DIFFERENT key so old_key cannot decrypt it.
    other_key = Fernet.generate_key()
    other_f = Fernet(other_key)
    bad_encrypted = other_f.encrypt(b"secret").decode()

    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [(1, bad_encrypted)]

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    monkeypatch.setenv("OLD_FERNET_KEY", old_key.decode())
    monkeypatch.setenv("NEW_FERNET_KEY", new_key.decode())

    args = _build_parser().parse_args(["rotate-fernet"])
    with patch("psycopg2.connect", return_value=mock_conn):
        with patch("src.cli._get_pg_dsn", return_value="postgresql://user:pw@localhost/db"):
            with pytest.raises(SystemExit) as exc_info:
                _cmd_rotate_fernet(args)

    assert exc_info.value.code == 2
    # Ensure rollback was called (not commit).
    mock_conn.rollback.assert_called_once()
    mock_conn.commit.assert_not_called()
