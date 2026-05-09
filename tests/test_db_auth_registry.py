"""Tests for src/db/auth_registry.py — requires PostgreSQL."""

import pytest

from src.db.auth_registry import (
    create_api_key,
    deactivate_api_key,
    list_api_keys,
    list_ssh_keys,
    log_usage,
    save_ssh_key,
    verify_api_key,
)

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
        raw, prefix, key_id = create_api_key(pg_auth_conn, "test-key")
        assert raw.startswith("osm_")
        assert raw[:8] == prefix
        assert key_id > 0

    def test_verify_correct_key(self, pg_auth_conn):
        raw, _, key_id = create_api_key(pg_auth_conn, "verify-test")
        result = verify_api_key(pg_auth_conn, raw)
        assert result == key_id

    def test_verify_wrong_key_returns_none(self, pg_auth_conn):
        create_api_key(pg_auth_conn, "verify-wrong")
        result = verify_api_key(pg_auth_conn, "osm_notvalid")
        assert result is None

    def test_verify_inactive_key_returns_none(self, pg_auth_conn):
        raw, _, key_id = create_api_key(pg_auth_conn, "inactive-test")
        deactivate_api_key(pg_auth_conn, key_id)
        result = verify_api_key(pg_auth_conn, raw)
        assert result is None

    def test_deactivate_then_list(self, pg_auth_conn):
        raw, _, key_id = create_api_key(pg_auth_conn, "list-test")
        deactivate_api_key(pg_auth_conn, key_id)
        keys = list_api_keys(pg_auth_conn)
        found = [k for k in keys if k["id"] == key_id]
        assert found
        assert found[0]["active"] is False

    def test_log_usage_no_error(self, pg_auth_conn):
        raw, _, key_id = create_api_key(pg_auth_conn, "log-test")
        log_usage(pg_auth_conn, key_id, "resolve_model", 42)

    def test_log_usage_none_key_id(self, pg_auth_conn):
        log_usage(pg_auth_conn, None, "unknown_tool", 10)

    def test_save_and_list_ssh_keys(self, pg_auth_conn):
        ssh_id = save_ssh_key(pg_auth_conn, "test-key", "ssh-ed25519 AAAA...", "encrypted_blob", 1)
        assert ssh_id > 0
        keys = list_ssh_keys(pg_auth_conn)
        found = [k for k in keys if k["id"] == ssh_id]
        assert found
        assert found[0]["name"] == "test-key"
