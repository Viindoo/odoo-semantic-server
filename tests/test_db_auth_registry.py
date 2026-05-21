# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for src/db/auth_registry.py — requires PostgreSQL."""

import pytest

from src.db.pg import auth_store

pytestmark = pytest.mark.postgres


@pytest.fixture
def pg_auth_conn(pg_conn):
    """Use the shared postgres fixture and ensure auth tables exist."""
    from src.db.migrate import run_migrations

    run_migrations(pg_conn)
    # Clean up before test
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM api_keys")
        cur.execute("DELETE FROM ssh_key_pairs")
        cur.execute("DELETE FROM usage_log")
    if not pg_conn.autocommit:
        pg_conn.commit()
    yield pg_conn
    # Clean up after test
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM api_keys")
        cur.execute("DELETE FROM ssh_key_pairs")
        cur.execute("DELETE FROM usage_log")
    if not pg_conn.autocommit:
        pg_conn.commit()


class TestCreateAndVerifyApiKey:
    def test_create_returns_raw_key_with_prefix(self, pg_auth_conn):
        raw, prefix, key_id = auth_store().create_api_key("test-key")
        assert raw.startswith("osm_")
        assert raw[:12] == prefix  # M9 W-AK: key_prefix bumped 8 → 12 chars
        assert key_id > 0

    def test_verify_correct_key(self, pg_auth_conn):
        raw, _, key_id = auth_store().create_api_key("verify-test")
        result = auth_store().verify_api_key(raw)
        assert result == key_id

    def test_verify_wrong_key_returns_none(self, pg_auth_conn):
        auth_store().create_api_key("verify-wrong")
        result = auth_store().verify_api_key("osm_notvalid")
        assert result is None

    def test_verify_inactive_key_returns_none(self, pg_auth_conn):
        raw, _, key_id = auth_store().create_api_key("inactive-test")
        auth_store().deactivate_api_key(key_id)
        result = auth_store().verify_api_key(raw)
        assert result is None

    def test_deactivate_then_list(self, pg_auth_conn):
        raw, _, key_id = auth_store().create_api_key("list-test")
        auth_store().deactivate_api_key(key_id)
        keys = auth_store().list_api_keys()
        found = [k for k in keys if k["id"] == key_id]
        assert found
        assert found[0]["active"] is False

    def test_log_usage_no_error(self, pg_auth_conn):
        raw, _, key_id = auth_store().create_api_key("log-test")
        auth_store().log_usage(key_id, "resolve_model", 42)

    def test_log_usage_none_key_id(self, pg_auth_conn):
        auth_store().log_usage(None, "unknown_tool", 10)

    def test_save_and_list_ssh_keys(self, pg_auth_conn):
        ssh_id = auth_store().save_ssh_key("test-key", "ssh-ed25519 AAAA...", "encrypted_blob", 1)
        assert ssh_id > 0
        keys = auth_store().list_ssh_keys()
        found = [k for k in keys if k["id"] == ssh_id]
        assert found
        assert found[0]["name"] == "test-key"
