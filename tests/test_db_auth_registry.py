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


class TestListApiKeysExposesPlanAndOverrides:
    """WI-2 (CLASS 2): list_api_keys must expose plan_id + per-key overrides.

    These columns power the admin api-keys UI: the plan dropdown prefill and the
    per-key Overrides modal.  Before the fix the SELECT omitted them, so the form
    rendered blank and a 0-vs-NULL parse bug blocked Save.  These tests protect
    the *behaviour* (the returned dict carries the persisted values, with NULL
    distinct from 0 per ADR-0041), not the SQL text.
    """

    @staticmethod
    def _plan_id(conn, slug: str) -> int:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM plans WHERE slug = %s", (slug,))
            row = cur.fetchone()
        assert row is not None, f"seed plan {slug!r} missing — migrations not applied?"
        return row[0]

    @staticmethod
    def _set_plan_and_overrides(conn, key_id, plan_id, rate, quota) -> None:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE api_keys SET plan_id = %s, rate_limit_override = %s, "
                "quota_override = %s WHERE id = %s",
                (plan_id, rate, quota, key_id),
            )
        if not conn.autocommit:
            conn.commit()

    def test_list_returns_plan_id_and_override_values(self, pg_auth_conn):
        # Reproduces the visual-verify scenario: a key on a real plan with
        # explicit numeric overrides (rate=120, quota=50000) must surface those
        # exact values so the form can prefill them.
        raw, _, key_id = auth_store().create_api_key("override-key")
        plan_id = self._plan_id(pg_auth_conn, "unlimited")
        self._set_plan_and_overrides(pg_auth_conn, key_id, plan_id, 120, 50000)

        keys = auth_store().list_api_keys()
        found = next((k for k in keys if k["id"] == key_id), None)
        assert found is not None
        # The three columns must be present AND carry the persisted values.
        assert found["plan_id"] == plan_id
        assert found["rate_limit_override"] == 120
        assert found["quota_override"] == 50000

    def test_list_distinguishes_zero_override_from_null(self, pg_auth_conn):
        # ADR-0041: override 0 means "zero allowed" (hard block), NOT "unset".
        # A NULL override means "use plan default".  list_api_keys must preserve
        # the difference so the UI does not collapse 0 into blank or vice versa.
        plan_id = self._plan_id(pg_auth_conn, "free")

        _, _, zero_id = auth_store().create_api_key("zero-override-key")
        self._set_plan_and_overrides(pg_auth_conn, zero_id, plan_id, 0, 0)

        _, _, null_id = auth_store().create_api_key("null-override-key")
        self._set_plan_and_overrides(pg_auth_conn, null_id, plan_id, None, None)

        keys = auth_store().list_api_keys()
        zero = next((k for k in keys if k["id"] == zero_id), None)
        null = next((k for k in keys if k["id"] == null_id), None)
        assert zero is not None and null is not None

        # Explicit zero survives as 0, not None.
        assert zero["rate_limit_override"] == 0
        assert zero["quota_override"] == 0
        # Unset survives as None, not 0.
        assert null["rate_limit_override"] is None
        assert null["quota_override"] is None
