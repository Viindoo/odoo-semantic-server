# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_cli_rotate_fernet.py
"""Tests for FERNET rotation hardening — atomic rollback, audit log, env-var delivery.

Unit tests (no Docker) cover:
  - Atomic rollback when any row fails to decrypt.
  - Audit row written to key_rotation_log on success.
  - Rotation covers both ssh_key_pairs AND totp_secrets (atomic, single txn).
  - Legacy --old-key/--new-key flags are REMOVED (breaking change, WI-7).
  - Env-var-name indirection (--old-key-env/--new-key-env).

Integration tests (pytestmark = postgres) cover:
  - Full round-trip against a real PostgreSQL database (ssh + totp).
  - key_rotation_log table schema and index existence.
"""
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from src.cli import (
    _build_parser,
    _cmd_rotate_fernet,
    _key_fingerprint,
)

# ---------------------------------------------------------------------------
# Helper: build args via argparse so defaults are applied correctly
# ---------------------------------------------------------------------------

def _make_rotate_args(
    old_key_env="OLD_FERNET_KEY",
    new_key_env="NEW_FERNET_KEY",
):
    """Return a parsed Namespace for rotate-fernet with the given options."""
    argv = [
        "rotate-fernet",
        f"--old-key-env={old_key_env}",
        f"--new-key-env={new_key_env}",
    ]
    return _build_parser().parse_args(argv)


def _make_mock_conn(ssh_rows, totp_rows=None, totp_table_exists=True):
    """Build a mock psycopg2 connection for rotate-fernet tests.

    The rotate-fernet command makes these cursor calls in order:
      1. execute("BEGIN")
      2. execute("SELECT ... FROM ssh_key_pairs ... FOR UPDATE")
      3. fetchall()  → ssh_rows
      4. execute("SELECT EXISTS (...totp_secrets...)")
      5. fetchone()  → (totp_table_exists,)
      [if totp_table_exists:]
      6. execute("SELECT ... FROM totp_secrets ... FOR UPDATE")
      7. fetchall()  → totp_rows
    """
    if totp_rows is None:
        totp_rows = []

    mock_cur = MagicMock()
    # fetchall returns different values on successive calls:
    # call 1 → ssh rows, call 2 → totp rows
    mock_cur.fetchall.side_effect = [ssh_rows, totp_rows]
    # fetchone returns totp table existence
    mock_cur.fetchone.return_value = (totp_table_exists,)

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    return mock_conn, mock_cur


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

    def test_rollback_when_one_ssh_row_undecryptable(self, monkeypatch):
        """3 SSH rows, 1 corrupted → rollback called, no rows committed."""
        old_key = Fernet.generate_key()
        new_key = Fernet.generate_key()
        old_f = Fernet(old_key)
        other_f = Fernet(Fernet.generate_key())  # wrong key for row 2

        good1 = old_f.encrypt(b"private-key-1").decode()
        corrupted = other_f.encrypt(b"private-key-2").decode()  # undecryptable by old_key
        good3 = old_f.encrypt(b"private-key-3").decode()

        ssh_rows = [(1, good1), (2, corrupted), (3, good3)]
        mock_conn, mock_cur = _make_mock_conn(ssh_rows)

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

    def test_rollback_when_totp_row_undecryptable(self, monkeypatch):
        """1 good SSH row + 1 corrupted TOTP row → rollback entire transaction."""
        old_key = Fernet.generate_key()
        new_key = Fernet.generate_key()
        old_f = Fernet(old_key)
        wrong_f = Fernet(Fernet.generate_key())

        good_ssh = old_f.encrypt(b"ssh-priv-key").decode()
        bad_totp = wrong_f.encrypt(b"totp-secret").decode()  # undecryptable by old_key

        mock_conn, mock_cur = _make_mock_conn(
            ssh_rows=[(1, good_ssh)],
            totp_rows=[(42, bad_totp)],
            totp_table_exists=True,
        )

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

    def test_rollback_prevents_commit_on_decrypt_failure(self, monkeypatch):
        """When a row fails to decrypt, rollback is called and commit is never reached.

        Intent: atomicity guarantee — UPDATEs issued for valid rows before the
        failure MUST be rolled back, not committed.  The code path is:
          1. UPDATE may be issued for rows that decrypt successfully before the
             bad row is encountered.
          2. InvalidToken detected → conn.rollback() called.
          3. conn.commit() is NEVER called.
        The real DB round-trip is covered by the integration test
        ``test_rotation_covers_ssh_and_totp`` which verifies that ciphertext in
        the DB is unchanged after a failed rotation (old key still decrypts).
        """
        old_key = Fernet.generate_key()
        new_key = Fernet.generate_key()
        bad_f = Fernet(Fernet.generate_key())

        rows = [(1, bad_f.encrypt(b"k1").decode())]
        mock_conn, mock_cur = _make_mock_conn(rows)

        monkeypatch.setenv("OLD_FERNET_KEY", old_key.decode())
        monkeypatch.setenv("NEW_FERNET_KEY", new_key.decode())

        args = _make_rotate_args()

        with patch("psycopg2.connect", return_value=mock_conn):
            with patch("src.cli._get_pg_dsn", return_value="postgresql://u:p@h/d"):
                with pytest.raises(SystemExit):
                    _cmd_rotate_fernet(args)

        mock_conn.rollback.assert_called_once()
        mock_conn.commit.assert_not_called()


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

        mock_conn, mock_cur = _make_mock_conn(
            ssh_rows=[(42, encrypted)],
            totp_rows=[],
            totp_table_exists=True,
        )

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
        assert row_count == 1  # 1 ssh + 0 totp
        assert len(old_fp) == 16
        assert len(new_fp) == 16
        assert old_fp != new_fp


# ---------------------------------------------------------------------------
# Unit: totp_secrets co-rotation
# ---------------------------------------------------------------------------

class TestTotpSecretsCoRotation:
    """Rotation must cover totp_secrets in the same atomic transaction."""

    def test_ssh_and_totp_both_reencrypted(self, monkeypatch):
        """1 SSH key + 1 TOTP secret → both re-encrypted, row_count=2 in audit."""
        old_key = Fernet.generate_key()
        new_key = Fernet.generate_key()
        old_f = Fernet(old_key)

        ssh_enc = old_f.encrypt(b"ssh-priv").decode()
        totp_enc = old_f.encrypt(b"JBSWY3DPEHPK3PXP").decode()

        mock_conn, mock_cur = _make_mock_conn(
            ssh_rows=[(1, ssh_enc)],
            totp_rows=[(10, totp_enc)],
            totp_table_exists=True,
        )

        monkeypatch.setenv("OLD_FERNET_KEY", old_key.decode())
        monkeypatch.setenv("NEW_FERNET_KEY", new_key.decode())
        monkeypatch.setenv("USER", "rotation-test")

        args = _make_rotate_args()

        with patch("psycopg2.connect", return_value=mock_conn):
            with patch("src.cli._get_pg_dsn", return_value="postgresql://u:p@h/d"):
                result = _cmd_rotate_fernet(args)

        assert result == 0
        mock_conn.commit.assert_called_once()

        # row_count in audit should be 2 (1 ssh + 1 totp)
        insert_calls = [
            c for c in mock_cur.execute.call_args_list
            if "key_rotation_log" in str(c)
        ]
        assert len(insert_calls) == 1
        params = insert_calls[0][0][1]
        actor, row_count, old_fp, new_fp = params
        assert row_count == 2

    def test_skips_totp_when_table_absent(self, monkeypatch):
        """When totp_secrets table doesn't exist, rotation still succeeds for ssh."""
        old_key = Fernet.generate_key()
        new_key = Fernet.generate_key()
        old_f = Fernet(old_key)

        ssh_enc = old_f.encrypt(b"ssh-priv").decode()

        mock_conn, mock_cur = _make_mock_conn(
            ssh_rows=[(1, ssh_enc)],
            totp_rows=[],
            totp_table_exists=False,
        )

        monkeypatch.setenv("OLD_FERNET_KEY", old_key.decode())
        monkeypatch.setenv("NEW_FERNET_KEY", new_key.decode())

        args = _make_rotate_args()

        with patch("psycopg2.connect", return_value=mock_conn):
            with patch("src.cli._get_pg_dsn", return_value="postgresql://u:p@h/d"):
                result = _cmd_rotate_fernet(args)

        assert result == 0
        mock_conn.commit.assert_called_once()
        # When totp_table_exists=False, fetchall is only called once (ssh_rows)
        # and the totp SELECT is never issued.
        totp_select_calls = [
            c for c in mock_cur.execute.call_args_list
            if "FROM totp_secrets" in str(c)
        ]
        assert totp_select_calls == [], "Should not SELECT from totp_secrets when table absent"


# ---------------------------------------------------------------------------
# Unit: legacy --old-key/--new-key flags REMOVED (breaking change WI-7)
# ---------------------------------------------------------------------------

class TestLegacyFlagsRemoved:
    """--old-key and --new-key CLI flags must be GONE from the Namespace (WI-7 breaking change).

    Note: argparse allows `--old-key` as an abbreviation of `--old-key-env`, so
    parse_args() still silently maps it to old_key_env — that is expected and fine.
    What matters is that the `old_key` / `new_key` *attributes* no longer exist on
    the Namespace (previously they held the raw key value that leaked to cmdline).
    """

    def test_old_key_attribute_absent_from_namespace(self):
        """Parsed Namespace must NOT have an `old_key` attribute (flag definition removed)."""
        ns = _make_rotate_args()
        assert not hasattr(ns, "old_key"), (
            "Namespace should not have 'old_key' — the flag definition was removed in WI-7"
        )

    def test_new_key_attribute_absent_from_namespace(self):
        """Parsed Namespace must NOT have a `new_key` attribute (flag definition removed)."""
        ns = _make_rotate_args()
        assert not hasattr(ns, "new_key"), (
            "Namespace should not have 'new_key' — the flag definition was removed in WI-7"
        )

    def test_flags_absent_from_help(self):
        """--old-key and --new-key must not appear as standalone entries in formatted help."""
        parser = _build_parser()
        rot_parser = None
        for action in parser._subparsers._group_actions:
            for name, sub in action.choices.items():
                if name == "rotate-fernet":
                    rot_parser = sub
                    break
        assert rot_parser is not None
        help_text = rot_parser.format_help()
        # The flags themselves (as standalone entries) must not appear
        assert "  --old-key " not in help_text
        assert "  --new-key " not in help_text


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
        mock_conn, mock_cur = _make_mock_conn(
            ssh_rows=[(7, encrypted)],
            totp_rows=[],
            totp_table_exists=True,
        )

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


@pytest.mark.postgres
def test_rotation_covers_ssh_and_totp(clean_pg):
    """Integration: rotation re-encrypts one SSH key + one TOTP secret in one txn.

    After rotation: new key decrypts both; old key no longer decrypts either.
    """
    import os

    from src.db.migrate import run_migrations

    run_migrations(clean_pg)

    old_key = Fernet.generate_key()
    new_key = Fernet.generate_key()
    old_f = Fernet(old_key)
    new_f = Fernet(new_key)

    ssh_plaintext = b"-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----"
    totp_secret = b"JBSWY3DPEHPK3PXP"

    ssh_enc_old = old_f.encrypt(ssh_plaintext).decode()
    totp_enc_old = old_f.encrypt(totp_secret).decode()

    # Seed DB
    with clean_pg.cursor() as cur:
        # Insert SSH key
        cur.execute(
            "INSERT INTO ssh_key_pairs (name, public_key, private_key_encrypted) "
            "VALUES ('test-key', 'ssh-ed25519 AAAA...', %s) RETURNING id",
            (ssh_enc_old,),
        )
        ssh_id = cur.fetchone()[0]

        # Insert a user for the TOTP row
        cur.execute(
            "INSERT INTO webui_users (username, password_hash) "
            "VALUES ('totp-user', 'x') RETURNING id"
        )
        user_id = cur.fetchone()[0]

        # Insert TOTP secret
        cur.execute(
            "INSERT INTO totp_secrets (user_id, secret_encrypted, enabled, backup_codes_hash) "
            "VALUES (%s, %s, FALSE, '[]'::jsonb)",
            (user_id, totp_enc_old),
        )
        clean_pg.commit()

    # Run rotation via CLI
    args = _make_rotate_args()

    from tests.conftest import PG_TEST_DSN

    with patch.dict(os.environ, {
        "OLD_FERNET_KEY": old_key.decode(),
        "NEW_FERNET_KEY": new_key.decode(),
    }):
        with patch("src.cli._get_pg_dsn", return_value=PG_TEST_DSN):
            result = _cmd_rotate_fernet(args)

    assert result == 0

    # Verify: new key decrypts SSH row
    with clean_pg.cursor() as cur:
        cur.execute("SELECT private_key_encrypted FROM ssh_key_pairs WHERE id = %s", (ssh_id,))
        new_ssh_enc = cur.fetchone()[0]
    assert new_f.decrypt(new_ssh_enc.encode()) == ssh_plaintext

    # Verify: new key decrypts TOTP row
    with clean_pg.cursor() as cur:
        cur.execute("SELECT secret_encrypted FROM totp_secrets WHERE user_id = %s", (user_id,))
        new_totp_enc = cur.fetchone()[0]
    assert new_f.decrypt(new_totp_enc.encode()) == totp_secret

    # Verify: old key can NO LONGER decrypt either
    from cryptography.fernet import InvalidToken
    with pytest.raises(InvalidToken):
        old_f.decrypt(new_ssh_enc.encode())
    with pytest.raises(InvalidToken):
        old_f.decrypt(new_totp_enc.encode())

    # Verify: key_rotation_log has an entry with row_count=2
    with clean_pg.cursor() as cur:
        cur.execute("SELECT row_count FROM key_rotation_log ORDER BY rotated_at DESC LIMIT 1")
        row = cur.fetchone()
    assert row is not None, "key_rotation_log should have an entry"
    assert row[0] == 2, f"Expected row_count=2, got {row[0]}"


@pytest.mark.postgres
def test_rotation_rollback_leaves_db_unchanged(clean_pg):
    """Integration: failed rotation must not change any row in the DB.

    Atomicity contract: if any row fails to decrypt, rollback must revert ALL
    updates including rows that were successfully re-encrypted before the failure.
    After a failed rotation the old key must still decrypt all rows.
    """
    import os

    from cryptography.fernet import InvalidToken

    from src.db.migrate import run_migrations

    run_migrations(clean_pg)

    old_key = Fernet.generate_key()
    new_key = Fernet.generate_key()
    wrong_key = Fernet.generate_key()
    old_f = Fernet(old_key)
    wrong_f = Fernet(wrong_key)

    ssh_plaintext = b"-----BEGIN OPENSSH PRIVATE KEY-----\ngood\n-----END"
    totp_secret = b"BADTOTPSECRET"

    ssh_enc_old = old_f.encrypt(ssh_plaintext).decode()
    # TOTP row encrypted with wrong key — will fail to decrypt with old_key
    totp_enc_bad = wrong_f.encrypt(totp_secret).decode()

    with clean_pg.cursor() as cur:
        cur.execute(
            "INSERT INTO ssh_key_pairs (name, public_key, private_key_encrypted) "
            "VALUES ('rollback-test', 'ssh-ed25519 AAAA...', %s) RETURNING id",
            (ssh_enc_old,),
        )
        ssh_id = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO webui_users (username, password_hash) "
            "VALUES ('rollback-totp-user', 'x') RETURNING id"
        )
        user_id = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO totp_secrets (user_id, secret_encrypted, enabled, backup_codes_hash) "
            "VALUES (%s, %s, FALSE, '[]'::jsonb)",
            (user_id, totp_enc_bad),
        )
        clean_pg.commit()

    # Snapshot key_rotation_log count before the (failing) rotation
    with clean_pg.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM key_rotation_log")
        count_before = cur.fetchone()[0]

    args = _make_rotate_args()

    from tests.conftest import PG_TEST_DSN

    with patch.dict(os.environ, {
        "OLD_FERNET_KEY": old_key.decode(),
        "NEW_FERNET_KEY": new_key.decode(),
    }):
        with patch("src.cli._get_pg_dsn", return_value=PG_TEST_DSN):
            with pytest.raises(SystemExit) as exc_info:
                _cmd_rotate_fernet(args)

    assert exc_info.value.code == 2

    # DB must be unchanged: old key still decrypts SSH row
    with clean_pg.cursor() as cur:
        cur.execute(
            "SELECT private_key_encrypted FROM ssh_key_pairs WHERE id = %s", (ssh_id,)
        )
        stored_ssh = cur.fetchone()[0]
    assert old_f.decrypt(stored_ssh.encode()) == ssh_plaintext, (
        "SSH row was modified despite rollback — atomicity broken"
    )

    # new key must NOT decrypt the SSH row (rollback reverted any UPDATE)
    new_f = Fernet(new_key)
    with pytest.raises(InvalidToken):
        new_f.decrypt(stored_ssh.encode())

    # key_rotation_log must have NO new entry (no commit happened)
    with clean_pg.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM key_rotation_log")
        count_after = cur.fetchone()[0]
    assert count_after == count_before, (
        f"Expected key_rotation_log count unchanged ({count_before}), "
        f"but got {count_after} — a commit occurred despite the rotation failure"
    )
