# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the mint-time tenant resolvers on AuthStore (ADR-0034, m13_019).

Covers:
  - get_public_tenant_id() raises (fail-closed) when the 'public' tenant is absent.
  - get_viindoo_tenant_id() raises when the Viindoo tenant is absent.
  - resolve_default_mint_tenant_id() returns the Viindoo id for @viindoo.com users
    and the public id for everyone else (incl. user_id=None), never None.

Requires PostgreSQL (pytestmark = pytest.mark.postgres). Throwaway DSN only.
"""
from __future__ import annotations

import pytest

from src.db.migrate import run_migrations
from src.db.pg import auth_store

pytestmark = pytest.mark.postgres


@pytest.fixture
def migrated_pg(clean_pg):
    run_migrations(clean_pg)
    return clean_pg


def _del_tenant(conn, name):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM tenants WHERE name = %s", (name,))
    if not conn.autocommit:
        conn.commit()


def _insert_user(conn, username, email):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO webui_users (username, email, password_hash, is_admin, is_active) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (username, email, "x", False, True),
        )
        uid = cur.fetchone()[0]
    if not conn.autocommit:
        conn.commit()
    return uid


class TestTenantResolvers:
    def test_get_public_tenant_id_raises_when_absent(self, migrated_pg):
        # m13_019 creates the 'public' tenant; remove it to test fail-closed.
        _del_tenant(migrated_pg, "public")
        with pytest.raises(RuntimeError, match="public tenant missing"):
            auth_store().get_public_tenant_id()

    def test_get_viindoo_tenant_id_raises_when_absent(self, migrated_pg):
        _del_tenant(migrated_pg, "Viindoo Technology JSC")
        with pytest.raises(RuntimeError, match="Viindoo tenant missing"):
            auth_store().get_viindoo_tenant_id()

    def test_present_after_migration(self, migrated_pg):
        store = auth_store()
        assert isinstance(store.get_public_tenant_id(), int)
        assert isinstance(store.get_viindoo_tenant_id(), int)

    def test_resolve_viindoo_email_to_viindoo_tenant(self, migrated_pg):
        store = auth_store()
        uid = _insert_user(migrated_pg, "vdev", "Person@Viindoo.com")  # mixed case
        got = store.resolve_default_mint_tenant_id(uid)
        assert got == store.get_viindoo_tenant_id()

    def test_resolve_other_email_to_public_tenant(self, migrated_pg):
        store = auth_store()
        uid = _insert_user(migrated_pg, "gdev", "someone@gmail.com")
        got = store.resolve_default_mint_tenant_id(uid)
        assert got == store.get_public_tenant_id()

    def test_resolve_none_user_to_public_tenant(self, migrated_pg):
        store = auth_store()
        got = store.resolve_default_mint_tenant_id(None)
        assert got == store.get_public_tenant_id()

    def test_resolver_never_returns_none(self, migrated_pg):
        store = auth_store()
        uid = _insert_user(migrated_pg, "ndev", "no-at-symbol-weird")
        assert store.resolve_default_mint_tenant_id(uid) is not None
