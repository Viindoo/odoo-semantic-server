# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the reactivate / reassign tenant-isolation invariant
(ADR-0034, m13_019; code-review FINDING 1).

SECURITY INVARIANT: a non-admin, user-owned API key must NEVER be
``active=TRUE`` while ``tenant_id IS NULL`` (the unrestricted sentinel). The
m13_019 migration deactivates already-exposed external keys but leaves
``tenant_id=NULL``; the owner-facing reactivate path used to flip
``active=TRUE`` WITHOUT re-scoping the tenant, which re-opened the data-exposure
hole. These tests pin the fixed behaviour for both:

  - ``AuthStore.reactivate_api_key`` (and the module-level wrapper used by the
    web route), and
  - the sibling path ``AuthStore.assign_key_owner`` (admin reassign).

Requires PostgreSQL (pytestmark = pytest.mark.postgres). Throwaway DSN only —
``clean_pg`` DROPs tables, so this MUST NOT run against the prod DSN.
"""
from __future__ import annotations

import pytest

from src.db.auth_registry import reactivate_api_key as reactivate_api_key_fn
from src.db.migrate import run_migrations
from src.db.pg import auth_store, get_pool

pytestmark = pytest.mark.postgres


@pytest.fixture
def migrated_pg(clean_pg):
    run_migrations(clean_pg)
    return clean_pg


def _insert_user(conn, username, email, *, is_admin=False):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO webui_users (username, email, password_hash, is_admin, is_active) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (username, email, "x", is_admin, True),
        )
        uid = cur.fetchone()[0]
    if not conn.autocommit:
        conn.commit()
    return uid


def _key_row(conn, key_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT active, user_id, tenant_id FROM api_keys WHERE id = %s",
            (key_id,),
        )
        active, user_id, tenant_id = cur.fetchone()
    return {"active": active, "user_id": user_id, "tenant_id": tenant_id}


def _deactivate_unscoped(conn, key_id):
    """Reproduce the post-migration state: active=false, tenant_id=NULL."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE api_keys SET active = FALSE, tenant_id = NULL WHERE id = %s",
            (key_id,),
        )
    if not conn.autocommit:
        conn.commit()


# ---------------------------------------------------------------------------
# FINDING 1 — reactivate must re-scope a non-admin unrestricted key
# ---------------------------------------------------------------------------


class TestReactivateScopesTenant:
    def test_external_nonadmin_key_reactivates_to_public_not_null(self, migrated_pg):
        """The headline regression: a deactivated non-admin, non-viindoo key
        (the m13_019 deactivation target) must come back with a NON-NULL
        (public) tenant_id — NOT the unrestricted NULL sentinel."""
        store = auth_store()
        uid = _insert_user(migrated_pg, "ext1", "exposed@gmail.com")
        _, _, key_id = store.create_api_key("ext-key", user_id=uid, tenant_id=None)
        _deactivate_unscoped(migrated_pg, key_id)

        row = store.reactivate_api_key(key_id)

        assert row["active"] is True
        assert row["tenant_id"] is not None, "non-admin key must NOT come back unrestricted"
        assert row["tenant_id"] == store.get_public_tenant_id()
        # And the DB agrees.
        db = _key_row(migrated_pg, key_id)
        assert db["active"] is True
        assert db["tenant_id"] == store.get_public_tenant_id()

    def test_viindoo_nonadmin_key_reactivates_to_viindoo_tenant(self, migrated_pg):
        store = auth_store()
        uid = _insert_user(migrated_pg, "vdev1", "dev@viindoo.com")
        _, _, key_id = store.create_api_key("v-key", user_id=uid, tenant_id=None)
        _deactivate_unscoped(migrated_pg, key_id)

        row = store.reactivate_api_key(key_id)

        assert row["active"] is True
        assert row["tenant_id"] == store.get_viindoo_tenant_id()

    def test_admin_owned_key_reactivates_keeping_null_tenant(self, migrated_pg):
        """Admin-owned keys keep tenant_id=NULL (unrestricted, by design)."""
        store = auth_store()
        uid = _insert_user(migrated_pg, "boss", "boss@example.com", is_admin=True)
        _, _, key_id = store.create_api_key("admin-key", user_id=uid, tenant_id=None)
        _deactivate_unscoped(migrated_pg, key_id)

        row = store.reactivate_api_key(key_id)

        assert row["active"] is True
        assert row["tenant_id"] is None

    def test_system_cli_key_reactivates_keeping_null_tenant(self, migrated_pg):
        """System/CLI keys (user_id IS NULL) keep tenant_id=NULL."""
        store = auth_store()
        _, _, key_id = store.create_api_key("cli-key", user_id=None, tenant_id=None)
        _deactivate_unscoped(migrated_pg, key_id)

        row = store.reactivate_api_key(key_id)

        assert row["active"] is True
        assert row["user_id"] is None
        assert row["tenant_id"] is None

    def test_already_scoped_key_keeps_its_tenant(self, migrated_pg):
        """Re-scope only fires when tenant_id IS NULL; an already-scoped key
        keeps its existing (e.g. viindoo) tenant even on reactivation."""
        store = auth_store()
        uid = _insert_user(migrated_pg, "ext2", "someone@gmail.com")
        viindoo = store.get_viindoo_tenant_id()
        _, _, key_id = store.create_api_key("scoped", user_id=uid, tenant_id=viindoo)
        # Deactivate but KEEP the tenant (mimic a normal owner toggle).
        with migrated_pg.cursor() as cur:
            cur.execute("UPDATE api_keys SET active = FALSE WHERE id = %s", (key_id,))

        row = store.reactivate_api_key(key_id)

        assert row["active"] is True
        assert row["tenant_id"] == viindoo

    def test_module_level_wrapper_also_scopes(self, migrated_pg):
        """The module-level reactivate_api_key(pool, key_id) used by the web
        route must enforce the same invariant."""
        store = auth_store()
        uid = _insert_user(migrated_pg, "ext3", "exposed2@gmail.com")
        _, _, key_id = store.create_api_key("ext-key3", user_id=uid, tenant_id=None)
        _deactivate_unscoped(migrated_pg, key_id)

        row = reactivate_api_key_fn(get_pool(), key_id)

        assert row["active"] is True
        assert row["tenant_id"] == store.get_public_tenant_id()

    def test_missing_key_returns_none(self, migrated_pg):
        assert auth_store().reactivate_api_key(999999) is None

    def test_reactivate_fail_closed_when_tenant_missing(self, migrated_pg):
        """If the resolver cannot resolve a tenant (m13_019 tenants removed),
        reactivation FAILS (raises) rather than coming back unrestricted."""
        store = auth_store()
        uid = _insert_user(migrated_pg, "ext4", "exposed3@gmail.com")
        _, _, key_id = store.create_api_key("ext-key4", user_id=uid, tenant_id=None)
        _deactivate_unscoped(migrated_pg, key_id)
        # Remove the public tenant so the resolver fails.
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM tenants WHERE name = %s", ("public",))

        with pytest.raises(RuntimeError, match="public tenant missing"):
            store.reactivate_api_key(key_id)
        # The key must remain deactivated — no partial reactivation.
        assert _key_row(migrated_pg, key_id)["active"] is False


# ---------------------------------------------------------------------------
# Sibling path — assign_key_owner must preserve the invariant
# ---------------------------------------------------------------------------


class TestAssignKeyOwnerScopesTenant:
    def test_reassign_active_null_key_to_nonadmin_rescopes(self, migrated_pg):
        """Reassigning an active, unrestricted (tenant=NULL) key to a non-admin
        owner must re-scope its tenant in the same UPDATE."""
        store = auth_store()
        new_owner = _insert_user(migrated_pg, "newowner", "newowner@gmail.com")
        # Start as a system/CLI key: active, no owner, tenant_id=NULL (legitimate).
        _, _, key_id = store.create_api_key("sys-key", user_id=None, tenant_id=None)

        store.assign_key_owner(key_id, new_owner)

        db = _key_row(migrated_pg, key_id)
        assert db["user_id"] == new_owner
        assert db["tenant_id"] == store.get_public_tenant_id()

    def test_reassign_to_admin_keeps_null_tenant(self, migrated_pg):
        store = auth_store()
        admin = _insert_user(migrated_pg, "adm", "adm@example.com", is_admin=True)
        _, _, key_id = store.create_api_key("sys-key2", user_id=None, tenant_id=None)

        store.assign_key_owner(key_id, admin)

        db = _key_row(migrated_pg, key_id)
        assert db["user_id"] == admin
        assert db["tenant_id"] is None

    def test_clear_owner_keeps_null_tenant(self, migrated_pg):
        store = auth_store()
        uid = _insert_user(migrated_pg, "owner", "owner@gmail.com")
        _, _, key_id = store.create_api_key("k", user_id=uid, tenant_id=None)

        store.assign_key_owner(key_id, None)  # clear → system key

        db = _key_row(migrated_pg, key_id)
        assert db["user_id"] is None
        assert db["tenant_id"] is None
