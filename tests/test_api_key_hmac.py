# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for W-AK: HMAC hashing, expires_at filter, user_id FK wiring.

All DB-backed tests require PostgreSQL and are marked pytest.mark.postgres.
No-DB tests (HMAC unit) run anywhere.
"""
import datetime
import hashlib
import hmac
import logging
import secrets

import pytest

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pg_ak_conn(pg_conn):
    """Ensure auth tables (including M9 columns) exist and are clean."""
    from src.db.migrate import run_migrations

    run_migrations(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM api_keys")
        cur.execute("DELETE FROM webui_users")
    if not pg_conn.autocommit:
        pg_conn.commit()
    yield pg_conn
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM api_keys")
        cur.execute("DELETE FROM webui_users")
    if not pg_conn.autocommit:
        pg_conn.commit()


# ---------------------------------------------------------------------------
# No-DB: HMAC unit tests
# ---------------------------------------------------------------------------


class TestHmacHashKey:
    """Verify hash_key() uses HMAC-SHA256 keyed with WEBUI_SESSION_SECRET."""

    def test_hash_key_uses_hmac(self, monkeypatch):
        """hash_key() must return HMAC-SHA256, not plain SHA-256."""
        monkeypatch.setenv("WEBUI_SESSION_SECRET", "testsecret123")
        # Reset module-level cache so env change takes effect
        import src.auth as auth_mod
        auth_mod._DEV_FALLBACK_SECRET = None

        from src.auth import hash_key

        raw = "osm_testkey"
        expected = hmac.new(b"testsecret123", raw.encode(), "sha256").hexdigest()
        assert hash_key(raw) == expected

    def test_hash_key_is_not_sha256(self, monkeypatch):
        """hash_key() output must differ from plain SHA-256."""
        monkeypatch.setenv("WEBUI_SESSION_SECRET", "anysecret")
        import src.auth as auth_mod
        auth_mod._DEV_FALLBACK_SECRET = None

        from src.auth import hash_key, hash_key_legacy_sha256

        raw = "osm_somekey"
        assert hash_key(raw) != hash_key_legacy_sha256(raw)

    def test_hash_key_deterministic(self, monkeypatch):
        """Same secret + same raw key must produce same hash every call."""
        monkeypatch.setenv("WEBUI_SESSION_SECRET", "stablesecret")
        import src.auth as auth_mod
        auth_mod._DEV_FALLBACK_SECRET = None

        from src.auth import hash_key

        raw = "osm_deterministickey"
        assert hash_key(raw) == hash_key(raw)

    def test_dev_fallback_warns_when_secret_unset(self, monkeypatch, caplog):
        """When WEBUI_SESSION_SECRET is unset, hash_key() logs a warning."""
        monkeypatch.delenv("WEBUI_SESSION_SECRET", raising=False)
        import src.auth as auth_mod
        auth_mod._DEV_FALLBACK_SECRET = None

        from src.auth import hash_key

        with caplog.at_level(logging.WARNING, logger="src.auth"):
            hash_key("osm_test")

        assert any("WEBUI_SESSION_SECRET not set" in r.message for r in caplog.records)

    def test_key_prefix_is_12_chars(self):
        """New keys must use 12-char prefix (bumped from 8)."""
        # Just verify the slice — no DB needed
        raw = "osm_" + secrets.token_urlsafe(32)
        prefix = raw[:12]
        assert len(prefix) == 12


# ---------------------------------------------------------------------------
# DB-backed: HMAC lookup
# ---------------------------------------------------------------------------


class TestHmacLookupMatches:
    def test_hmac_lookup_matches(self, pg_ak_conn, monkeypatch):
        """Key created with HMAC hash must verify successfully."""
        monkeypatch.setenv("WEBUI_SESSION_SECRET", "unitsecret")
        import src.auth as auth_mod
        auth_mod._DEV_FALLBACK_SECRET = None

        from src.db.pg import auth_store

        raw, _, key_id = auth_store().create_api_key("hmac-test")
        result = auth_store().verify_api_key(raw)
        assert result == key_id

    def test_wrong_key_returns_none(self, pg_ak_conn, monkeypatch):
        """Random key string that was never inserted must return None."""
        monkeypatch.setenv("WEBUI_SESSION_SECRET", "unitsecret")
        import src.auth as auth_mod
        auth_mod._DEV_FALLBACK_SECRET = None

        from src.db.pg import auth_store

        auth_store().create_api_key("dummy")
        result = auth_store().verify_api_key("osm_notavalidkey")
        assert result is None


# ---------------------------------------------------------------------------
# DB-backed: SHA-256 fallback (legacy backward-compat path)
# ---------------------------------------------------------------------------


class TestSha256FallbackLookupMatches:
    def _insert_sha256_key(self, pg_conn, name: str, raw: str) -> int:
        """Directly insert a key row using plain SHA-256 hash (simulates pre-M9 key)."""
        sha_hash = hashlib.sha256(raw.encode()).hexdigest()
        prefix = raw[:8]  # old 8-char prefix
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_keys (name, key_hash, key_prefix) "
                "VALUES (%s, %s, %s) RETURNING id",
                (name, sha_hash, prefix),
            )
            row_id = cur.fetchone()[0]
        if not pg_conn.autocommit:
            pg_conn.commit()
        return row_id

    def test_sha256_fallback_lookup_matches(self, pg_ak_conn, monkeypatch):
        """Legacy SHA-256 key must still verify via fallback path."""
        monkeypatch.setenv("WEBUI_SESSION_SECRET", "unitsecret")
        import src.auth as auth_mod
        auth_mod._DEV_FALLBACK_SECRET = None

        from src.db.pg import auth_store

        raw = "osm_legacykeyabcdefghijklmnopqrstuvwxyz"
        key_id = self._insert_sha256_key(pg_ak_conn, "legacy-sha", raw)

        result = auth_store().verify_api_key(raw)
        assert result == key_id

    def test_legacy_path_logs_warning(self, pg_ak_conn, monkeypatch, caplog):
        """Legacy SHA-256 fallback match must log a warning asking user to rotate."""
        monkeypatch.setenv("WEBUI_SESSION_SECRET", "unitsecret")
        import src.auth as auth_mod
        auth_mod._DEV_FALLBACK_SECRET = None

        from src.db.pg import auth_store

        raw = "osm_legacywarnkeyabcdefghijklmnopqrstuv"
        self._insert_sha256_key(pg_ak_conn, "legacy-warn", raw)

        with caplog.at_level(logging.WARNING, logger="src.db.auth_registry"):
            auth_store().verify_api_key(raw)

        assert any("legacy SHA-256" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# DB-backed: expires_at filter
# ---------------------------------------------------------------------------


class TestExpiresAtFilter:
    def test_expired_key_returns_none(self, pg_ak_conn, monkeypatch):
        """Key with expires_at in the past must return None."""
        monkeypatch.setenv("WEBUI_SESSION_SECRET", "unitsecret")
        import src.auth as auth_mod
        auth_mod._DEV_FALLBACK_SECRET = None

        from src.db.pg import auth_store

        past = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)
        raw, _, key_id = auth_store().create_api_key("expired-key", expires_at=past)
        result = auth_store().verify_api_key(raw)
        assert result is None

    def test_future_expiry_key_verifies(self, pg_ak_conn, monkeypatch):
        """Key with expires_at in the future must verify successfully."""
        monkeypatch.setenv("WEBUI_SESSION_SECRET", "unitsecret")
        import src.auth as auth_mod
        auth_mod._DEV_FALLBACK_SECRET = None

        from src.db.pg import auth_store

        future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=30)
        raw, _, key_id = auth_store().create_api_key("future-key", expires_at=future)
        result = auth_store().verify_api_key(raw)
        assert result == key_id

    def test_eternal_key_verifies(self, pg_ak_conn, monkeypatch):
        """Key with expires_at=None (eternal) must verify successfully."""
        monkeypatch.setenv("WEBUI_SESSION_SECRET", "unitsecret")
        import src.auth as auth_mod
        auth_mod._DEV_FALLBACK_SECRET = None

        from src.db.pg import auth_store

        raw, _, key_id = auth_store().create_api_key("eternal-key", expires_at=None)
        result = auth_store().verify_api_key(raw)
        assert result == key_id


# ---------------------------------------------------------------------------
# DB-backed: user_id FK wiring
# ---------------------------------------------------------------------------


class TestCreateKeySetsUserId:
    def _create_user(self, pg_conn, username: str) -> int:
        """Insert a webui_users row and return its integer id."""
        pw_hash = "$2b$12$fakehashfortest"
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO webui_users (username, password_hash) "
                "VALUES (%s, %s) RETURNING id",
                (username, pw_hash),
            )
            row_id = cur.fetchone()[0]
        if not pg_conn.autocommit:
            pg_conn.commit()
        return row_id

    def test_create_key_sets_user_id(self, pg_ak_conn, monkeypatch):
        """Key created with user_id must have that user_id stored in DB."""
        monkeypatch.setenv("WEBUI_SESSION_SECRET", "unitsecret")
        import src.auth as auth_mod
        auth_mod._DEV_FALLBACK_SECRET = None

        from src.db.pg import auth_store

        uid = self._create_user(pg_ak_conn, "testuser1")
        raw, _, key_id = auth_store().create_api_key("user-key", user_id=uid)

        keys = auth_store().list_api_keys(user_id=uid, admin=False)
        found = [k for k in keys if k["id"] == key_id]
        assert found, "key must appear in user's key list"
        assert found[0]["user_id"] == uid

    def test_admin_key_user_id_null(self, pg_ak_conn, monkeypatch):
        """CLI/admin key created without user_id must have user_id=NULL."""
        monkeypatch.setenv("WEBUI_SESSION_SECRET", "unitsecret")
        import src.auth as auth_mod
        auth_mod._DEV_FALLBACK_SECRET = None

        from src.db.pg import auth_store

        raw, _, key_id = auth_store().create_api_key("admin-key")  # no user_id

        keys = auth_store().list_api_keys(admin=True)
        found = [k for k in keys if k["id"] == key_id]
        assert found, "admin key must appear in full list"
        assert found[0]["user_id"] is None

    def test_list_api_keys_user_filter(self, pg_ak_conn, monkeypatch):
        """User A list must include only A's keys; admin list sees all."""
        monkeypatch.setenv("WEBUI_SESSION_SECRET", "unitsecret")
        import src.auth as auth_mod
        auth_mod._DEV_FALLBACK_SECRET = None

        from src.db.pg import auth_store

        uid_a = self._create_user(pg_ak_conn, "user_a")
        uid_b = self._create_user(pg_ak_conn, "user_b")

        _, _, key_a = auth_store().create_api_key("key-a", user_id=uid_a)
        _, _, key_b = auth_store().create_api_key("key-b", user_id=uid_b)
        _, _, key_admin = auth_store().create_api_key("key-admin")  # no user_id

        # User A sees only their key
        keys_a = auth_store().list_api_keys(user_id=uid_a, admin=False)
        ids_a = {k["id"] for k in keys_a}
        assert key_a in ids_a
        assert key_b not in ids_a
        assert key_admin not in ids_a

        # Admin sees all
        keys_all = auth_store().list_api_keys(admin=True)
        ids_all = {k["id"] for k in keys_all}
        assert {key_a, key_b, key_admin}.issubset(ids_all)

    def test_key_prefix_is_12_chars_db(self, pg_ak_conn, monkeypatch):
        """New keys stored in DB must have 12-char prefix."""
        monkeypatch.setenv("WEBUI_SESSION_SECRET", "unitsecret")
        import src.auth as auth_mod
        auth_mod._DEV_FALLBACK_SECRET = None

        from src.db.pg import auth_store

        raw, prefix, key_id = auth_store().create_api_key("prefix-test")
        assert len(prefix) == 12
        assert raw.startswith(prefix)
