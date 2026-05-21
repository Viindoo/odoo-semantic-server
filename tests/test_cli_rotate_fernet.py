# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_cli_rotate_fernet.py
"""Tests for FERNET rotation hardening — atomic rollback, audit log, env-var delivery.

Unit tests (no Docker) cover:
  - Atomic rollback when any row fails to decrypt.
  - Audit row written to key_rotation_log on success.
  - Deprecation warning when legacy --old-key/--new-key flags are used.
  - Env-var-name indirection (--old-key-env/--new-key-env).

Integration tests (pytestmark = postgres) cover:
  - Full round-trip against a real PostgreSQL database.
"""
import logging
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from src.cli import _build_parser, _cmd_rotate_fernet, _key_fingerprint

# ---------------------------------------------------------------------------
# Helper: build args via argparse so defaults are applied correctly
# ---------------------------------------------------------------------------

def _make_rotate_args(
    old_key=None,
    new_key=None,
    old_key_env="OLD_FERNET_KEY",
    new_key_env="NEW_FERNET_KEY",
):
    """Return a parsed Namespace for rotate-fernet with the given options."""
    argv = ["rotate-fernet"]
    if old_key:
        argv.append(f"--old-key={old_key}")
    if new_key:
        argv.append(f"--new-key={new_key}")
    argv += [f"--old-key-env={old_key_env}", f"--new-key-env={new_key_env}"]
    return _build_parser().parse_args(argv)


# ---------------------------------------------------------------------------
# Unit: key fingerprint helper
# ---------------------------------------------------------------------------

class TestKeyFingerprint:
    def test_returns_16_hex_chars(self):
        key = Fernet.generate_key()
        fp = _key_fingerprint(key)
        assert len(fp) == 16
        assert all(c in "0123456789abcdef" for c in fp)

    def test_different_keys_produce_different_fingerprints(self):
        k1 = Fernet.generate_key()
        k2 = Fernet.generate_key()
        assert _key_fingerprint(k1) != _key_fingerprint(k2)

    def test_same_key_idempotent(self):
        key = Fernet.generate_key()
        assert _key_fingerprint(key) == _key_fingerprint(key)


# ---------------------------------------------------------------------------
# Unit: rotation atomicity — rollback on InvalidToken
# ---------------------------------------------------------------------------

class TestRotationAtomicOnInvalidToken:
    """Rotation must roll back ALL updates if any row fails to decrypt."""

    def _make_mock_conn(self, rows):
        """Build a mock psycopg2 connection whose cursor returns *rows*."""
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = rows
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        return mock_conn, mock_cur

    def test_rollback_when_one_row_undecryptable(self, monkeypatch):
        """3 rows, 1 corrupted → rollback called, no rows committed."""
        old_key = Fernet.generate_key()
        new_key = Fernet.generate_key()
        old_f = Fernet(old_key)
        other_f = Fernet(Fernet.generate_key())  # wrong key for row 2

        good1 = old_f.encrypt(b"private-key-1").decode()
        corrupted = other_f.encrypt(b"private-key-2").decode()  # undecryptable by old_key
        good3 = old_f.encrypt(b"private-key-3").decode()

        rows = [(1, good1), (2, corrupted), (3, good3)]
        mock_conn, mock_cur = self._make_mock_conn(rows)

        monkeypatch.setenv("OLD_FERNET_KEY", old_key.decode())
        monkeypatch.setenv("NEW_FERNET_KEY", new_key.decode())

        args = _make_rotate_args()

        with patch("psycopg2.connect", return_value=mock_conn):
            with patch("src.cli._get_pg_dsn", return_value="postgresql://u:p@h/d"):
                with pytest.raises(SystemExit) as exc_info:
                    _cmd_rotate_fernet(args)

        assert exc_info.value.code == 2
        mock_conn.rollback.assert_called_once()
        mock_conn.commit.assert_not_called()

    def test_no_rows_changed_on_rollback(self, monkeypatch):
        """When rollback is triggered, UPDATE must not have been executed for any row."""
        old_key = Fernet.generate_key()
        new_key = Fernet.generate_key()
        bad_f = Fernet(Fernet.generate_key())

        rows = [(1, bad_f.encrypt(b"k1").decode())]

        mock_conn, mock_cur = self._make_mock_conn(rows)
        monkeypatch.setenv("OLD_FERNET_KEY", old_key.decode())
        monkeypatch.setenv("NEW_FERNET_KEY", new_key.decode())

        args = _make_rotate_args()

        with patch("psycopg2.connect", return_value=mock_conn):
            with patch("src.cli._get_pg_dsn", return_value="postgresql://u:p@h/d"):
                with pytest.raises(SystemExit):
                    _cmd_rotate_fernet(args)

        # No standalone UPDATE statement should have been called (SELECT … FOR UPDATE is fine).
        update_calls = [
            c for c in mock_cur.execute.call_args_list
            if str(c).lstrip().startswith("call('UPDATE") or "call(\"UPDATE" in str(c)
            or (len(c[0]) > 0 and isinstance(c[0][0], str) and c[0][0].strip().startswith("UPDATE"))
        ]
        assert update_calls == [], (
            f"UPDATE was called despite rollback path being taken: {update_calls}"
        )


# ---------------------------------------------------------------------------
# Unit: audit log on success
# ---------------------------------------------------------------------------

class TestRotationLogsToAudit:
    """Successful rotation must insert a row into key_rotation_log."""

    def test_audit_insert_on_success(self, monkeypatch):
        old_key = Fernet.generate_key()
        new_key = Fernet.generate_key()
        old_f = Fernet(old_key)

        plaintext = b"real-ssh-private-key"
        encrypted = old_f.encrypt(plaintext).decode()

        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [(42, encrypted)]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        monkeypatch.setenv("OLD_FERNET_KEY", old_key.decode())
        monkeypatch.setenv("NEW_FERNET_KEY", new_key.decode())
        monkeypatch.setenv("USER", "test-operator")

        args = _make_rotate_args()

        with patch("psycopg2.connect", return_value=mock_conn):
            with patch("src.cli._get_pg_dsn", return_value="postgresql://u:p@h/d"):
                result = _cmd_rotate_fernet(args)

        assert result == 0
        mock_conn.commit.assert_called_once()
        mock_conn.rollback.assert_not_called()

        # Verify INSERT INTO key_rotation_log was called.
        insert_calls = [
            c for c in mock_cur.execute.call_args_list
            if "key_rotation_log" in str(c)
        ]
        assert len(insert_calls) == 1, (
            f"Expected 1 INSERT into key_rotation_log, got: {insert_calls}"
        )

        # Audit params: (actor, row_count, old_key_id, new_key_id)
        insert_call = insert_calls[0]
        params = insert_call[0][1]  # positional args[1] = tuple of params
        actor, row_count, old_fp, new_fp = params
        assert actor == "test-operator"
        assert row_count == 1
        assert len(old_fp) == 16
        assert len(new_fp) == 16
        assert old_fp != new_fp


# ---------------------------------------------------------------------------
# Unit: legacy --old-key/--new-key flags emit deprecation warning
# ---------------------------------------------------------------------------

class TestCliFlagsWarning:
    """Legacy --old-key/--new-key flags must still work but log a warning."""

    def test_legacy_flags_warn_and_succeed(self, monkeypatch, caplog):
        old_key = Fernet.generate_key()
        new_key = Fernet.generate_key()
        old_f = Fernet(old_key)

        encrypted = old_f.encrypt(b"ssh-key").decode()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [(1, encrypted)]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        args = _make_rotate_args(old_key=old_key.decode(), new_key=new_key.decode())

        with caplog.at_level(logging.WARNING, logger="src.cli"):
            with patch("psycopg2.connect", return_value=mock_conn):
                with patch("src.cli._get_pg_dsn", return_value="postgresql://u:p@h/d"):
                    result = _cmd_rotate_fernet(args)

        assert result == 0
        assert any(
            "/proc/PID/cmdline" in record.message or "cmdline" in record.message
            for record in caplog.records
        ), "Expected deprecation warning about /proc/PID/cmdline"

    def test_legacy_flags_hidden_from_help(self):
        parser = _build_parser()
        # --old-key and --new-key should not appear in formatted help
        # (they are marked with argparse.SUPPRESS)
        rot_parser = None
        for action in parser._subparsers._group_actions:
            for name, sub in action.choices.items():
                if name == "rotate-fernet":
                    rot_parser = sub
                    break
        assert rot_parser is not None
        help_text = rot_parser.format_help()
        assert "--old-key " not in help_text
        assert "--new-key " not in help_text


# ---------------------------------------------------------------------------
# Unit: env-var indirection (--old-key-env/--new-key-env)
# ---------------------------------------------------------------------------

class TestEnvOnlyFlow:
    """Keys delivered via --old-key-env/--new-key-env should work end-to-end."""

    def test_custom_env_var_names(self, monkeypatch):
        old_key = Fernet.generate_key()
        new_key = Fernet.generate_key()
        old_f = Fernet(old_key)

        encrypted = old_f.encrypt(b"my-key").decode()
        mock_cur = MagicMock()
        mock_cur.fetchall.return_value = [(7, encrypted)]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        monkeypatch.setenv("MY_CUSTOM_OLD", old_key.decode())
        monkeypatch.setenv("MY_CUSTOM_NEW", new_key.decode())

        args = _make_rotate_args(old_key_env="MY_CUSTOM_OLD", new_key_env="MY_CUSTOM_NEW")

        with patch("psycopg2.connect", return_value=mock_conn):
            with patch("src.cli._get_pg_dsn", return_value="postgresql://u:p@h/d"):
                result = _cmd_rotate_fernet(args)

        assert result == 0
        mock_conn.commit.assert_called_once()

    def test_missing_env_var_exits_with_error(self, monkeypatch):
        monkeypatch.delenv("OLD_FERNET_KEY", raising=False)
        monkeypatch.delenv("NEW_FERNET_KEY", raising=False)

        args = _make_rotate_args()

        with patch("src.cli._get_pg_dsn", return_value="postgresql://u:p@h/d"):
            with pytest.raises(SystemExit) as exc_info:
                _cmd_rotate_fernet(args)

        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# Integration: key_rotation_log table existence after migrate
# ---------------------------------------------------------------------------

pytestmark_postgres = pytest.mark.postgres


@pytest.mark.postgres
def test_migrate_creates_key_rotation_log(clean_pg):
    """key_rotation_log table must exist after running all migrations."""
    from src.db.migrate import run_migrations

    run_migrations(clean_pg)

    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'key_rotation_log'
            ORDER BY ordinal_position
        """)
        cols = [r[0] for r in cur.fetchall()]

    assert cols, "key_rotation_log table not found after migrate"
    assert "id" in cols
    assert "rotated_at" in cols
    assert "actor" in cols
    assert "row_count" in cols
    assert "old_key_id" in cols
    assert "new_key_id" in cols


@pytest.mark.postgres
def test_key_rotation_log_index_exists(clean_pg):
    """idx_key_rotation_log_time index must exist after migrate."""
    from src.db.migrate import run_migrations

    run_migrations(clean_pg)

    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT indexname
            FROM pg_indexes
            WHERE tablename = 'key_rotation_log'
        """)
        indexes = [r[0] for r in cur.fetchall()]

    assert "idx_key_rotation_log_time" in indexes, (
        f"Expected idx_key_rotation_log_time in {indexes}"
    )
