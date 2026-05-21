# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_totp_mfa_sync.py
"""Integration tests for MFA sync (WI-2).

Tests that webui_users.mfa_enabled stays in sync with totp_secrets.enabled.
Both _enable_totp() and _delete_totp() should update both tables in the same transaction.
"""

import os

import pytest

pytestmark = pytest.mark.postgres


@pytest.fixture(autouse=True)
def _set_fernet_key(monkeypatch):
    """Ensure FERNET_KEY is set for encryption/decryption."""
    from cryptography.fernet import Fernet

    if not os.environ.get("FERNET_KEY"):
        monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())


@pytest.fixture(autouse=True)
def _set_session_secret(monkeypatch):
    """Ensure WEBUI_SESSION_SECRET is set for HMAC operations."""
    if not os.environ.get("WEBUI_SESSION_SECRET"):
        monkeypatch.setenv("WEBUI_SESSION_SECRET", "test-session-secret-for-tests-only")


class TestEnrollSetsUsersMfaEnabled:
    """test_enroll_sets_users_mfa_enabled"""

    def test_enroll_sets_users_mfa_enabled(self, pg_conn, clean_pg):
        """_enable_totp() sets webui_users.mfa_enabled = TRUE."""
        import pyotp

        from src.db.migrate import run_migrations
        from src.web_ui.auth import hash_password
        from src.web_ui.routes.totp import _enable_totp, _encrypt_secret

        run_migrations(pg_conn)

        # Create test user with mfa_enabled = FALSE
        secret = pyotp.random_base32()
        secret_enc = _encrypt_secret(secret)
        pw_hash = hash_password("test-password-12345")

        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO webui_users (username, password_hash, mfa_enabled) "
                "VALUES ('enroll_test_user', %s, FALSE) RETURNING id",
                (pw_hash,),
            )
            user_id = cur.fetchone()[0]

            # Create TOTP secret (not yet enabled)
            cur.execute(
                "INSERT INTO totp_secrets (user_id, secret_encrypted, enabled) "
                "VALUES (%s, %s, FALSE)",
                (user_id, secret_enc),
            )
        pg_conn.commit()

        # Verify initial state: mfa_enabled = FALSE
        with pg_conn.cursor() as cur:
            cur.execute("SELECT mfa_enabled FROM webui_users WHERE id = %s", (user_id,))
            initial_mfa_enabled = cur.fetchone()[0]
        assert initial_mfa_enabled is False, "User must start with mfa_enabled = FALSE"

        # Call _enable_totp to enroll
        backup_codes_hashed = [
            {"hash": "hash1", "used_at": None},
            {"hash": "hash2", "used_at": None},
        ]
        _enable_totp(user_id, backup_codes_hashed)

        # Verify both flags are now TRUE
        with pg_conn.cursor() as cur:
            cur.execute("SELECT mfa_enabled FROM webui_users WHERE id = %s", (user_id,))
            mfa_enabled = cur.fetchone()[0]
            cur.execute("SELECT enabled FROM totp_secrets WHERE user_id = %s", (user_id,))
            totp_enabled = cur.fetchone()[0]

        assert mfa_enabled is True, "webui_users.mfa_enabled must be TRUE after _enable_totp()"
        assert totp_enabled is True, "totp_secrets.enabled must be TRUE after _enable_totp()"

        # Cleanup
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM webui_users WHERE id = %s", (user_id,))
        pg_conn.commit()


class TestDisableClearsUsersMfaEnabled:
    """test_disable_clears_users_mfa_enabled"""

    def test_disable_clears_users_mfa_enabled(self, pg_conn, clean_pg):
        """_delete_totp() sets webui_users.mfa_enabled = FALSE."""
        import json

        import pyotp

        from src.db.migrate import run_migrations
        from src.web_ui.auth import hash_password
        from src.web_ui.routes.totp import _delete_totp, _encrypt_secret

        run_migrations(pg_conn)

        # Create test user with mfa_enabled = TRUE and TOTP enabled
        secret = pyotp.random_base32()
        secret_enc = _encrypt_secret(secret)
        pw_hash = hash_password("test-password-12345")

        backup_codes_json = [
            {"hash": "hash1", "used_at": None},
            {"hash": "hash2", "used_at": None},
        ]

        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO webui_users (username, password_hash, mfa_enabled) "
                "VALUES ('disable_test_user', %s, TRUE) RETURNING id",
                (pw_hash,),
            )
            user_id = cur.fetchone()[0]

            # Create enabled TOTP secret
            cur.execute(
                "INSERT INTO totp_secrets "
                "(user_id, secret_encrypted, enabled, backup_codes_hash) "
                "VALUES (%s, %s, TRUE, %s::jsonb)",
                (user_id, secret_enc, json.dumps(backup_codes_json)),
            )
        pg_conn.commit()

        # Verify initial state: both flags are TRUE
        with pg_conn.cursor() as cur:
            cur.execute("SELECT mfa_enabled FROM webui_users WHERE id = %s", (user_id,))
            initial_mfa_enabled = cur.fetchone()[0]
            cur.execute("SELECT enabled FROM totp_secrets WHERE user_id = %s", (user_id,))
            initial_totp_enabled = cur.fetchone()[0]

        assert (
            initial_mfa_enabled is True
        ), "User must start with mfa_enabled = TRUE"
        assert (
            initial_totp_enabled is True
        ), "TOTP must start with enabled = TRUE"

        # Call _delete_totp to disable
        _delete_totp(user_id)

        # Verify mfa_enabled is now FALSE
        with pg_conn.cursor() as cur:
            cur.execute("SELECT mfa_enabled FROM webui_users WHERE id = %s", (user_id,))
            row = cur.fetchone()

        # Check if the user still exists (should, since _delete_totp only deletes totp_secrets)
        # or if the row was deleted (it shouldn't be)
        assert row is not None, "webui_users row must exist after _delete_totp()"
        mfa_enabled = row[0]
        assert (
            mfa_enabled is False
        ), "webui_users.mfa_enabled must be FALSE after _delete_totp()"

        # Verify totp_secrets row is deleted
        with pg_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM totp_secrets WHERE user_id = %s", (user_id,))
            count = cur.fetchone()[0]
        assert count == 0, "totp_secrets row must be deleted after _delete_totp()"

        # Cleanup
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM webui_users WHERE id = %s", (user_id,))
        pg_conn.commit()
