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


def _insert_tenant(conn, name):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (name) VALUES (%s) "
            "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id",
            (name,),
        )
        tid = cur.fetchone()[0]
    if not conn.autocommit:
        conn.commit()
    return tid


def _add_membership(conn, user_id, tenant_id, role="member"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenant_members (user_id, tenant_id, role) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id, tenant_id) DO NOTHING",
            (user_id, tenant_id, role),
        )
    if not conn.autocommit:
        conn.commit()


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

    # --- F3: tenant_members membership takes precedence over email domain ----

    def test_member_of_one_tenant_wins_over_domain(self, migrated_pg):
        """F3: a gmail user who is a member of EXACTLY ONE tenant mints into that
        tenant, not 'public' (which the gmail domain would otherwise select)."""
        store = auth_store()
        uid = _insert_user(migrated_pg, "memberdev", "member@gmail.com")
        paid_tid = _insert_tenant(migrated_pg, "Paid Customer Co")
        _add_membership(migrated_pg, uid, paid_tid)

        got = store.resolve_default_mint_tenant_id(uid)
        assert got == paid_tid, "membership-of-one must win over gmail→public"
        assert got != store.get_public_tenant_id()

    def test_viindoo_email_nonmember_resolves_viindoo(self, migrated_pg):
        """F3: a @viindoo.com user with no membership falls through to domain."""
        store = auth_store()
        uid = _insert_user(migrated_pg, "viidev2", "dev@viindoo.com")
        got = store.resolve_default_mint_tenant_id(uid)
        assert got == store.get_viindoo_tenant_id()

    def test_gmail_nonmember_resolves_public(self, migrated_pg):
        """F3: a gmail user with no membership falls through to public."""
        store = auth_store()
        uid = _insert_user(migrated_pg, "gmaildev2", "nobody@gmail.com")
        got = store.resolve_default_mint_tenant_id(uid)
        assert got == store.get_public_tenant_id()

    def test_member_of_two_tenants_falls_through_to_domain(self, migrated_pg):
        """F3: >1 membership is ambiguous → deterministic domain/public fallback.

        A gmail user in two tenants resolves to public (not a guessed tenant)."""
        store = auth_store()
        uid = _insert_user(migrated_pg, "multidev", "multi@gmail.com")
        t1 = _insert_tenant(migrated_pg, "Tenant One")
        t2 = _insert_tenant(migrated_pg, "Tenant Two")
        _add_membership(migrated_pg, uid, t1)
        _add_membership(migrated_pg, uid, t2)

        got = store.resolve_default_mint_tenant_id(uid)
        assert got == store.get_public_tenant_id()


class TestViindooTenantSSOT:
    """F5: get_viindoo_tenant_id resolves by profile ownership, name as fallback."""

    def test_resolves_by_profile_owner_even_with_different_name(self, migrated_pg):
        """The viindoo tenant is the one that OWNS the viindoo profiles, even if
        its NAME differs from the canonical 'Viindoo Technology JSC'."""
        store = auth_store()
        conn = migrated_pg
        # A differently-named tenant owns the viindoo profiles.
        other_tid = _insert_tenant(conn, "Renamed Viindoo Co")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO profiles (name, odoo_version, tenant_id) "
                "VALUES (%s, %s, %s)",
                ("standard_viindoo_17", "17.0", other_tid),
            )
        if not conn.autocommit:
            conn.commit()

        assert store.get_viindoo_tenant_id() == other_tid

    def test_falls_back_to_name_when_no_owned_profiles(self, migrated_pg):
        """Fresh-install fallback: no viindoo profiles owned yet → resolve by the
        canonical tenant name (created by m13_019)."""
        store = auth_store()
        # migrated_pg has the 'Viindoo Technology JSC' tenant from m13_019 but no
        # viindoo profiles assigned to it, so step 1 yields zero rows.
        by_name = None
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT id FROM tenants WHERE name = %s",
                ("Viindoo Technology JSC",),
            )
            by_name = cur.fetchone()[0]
        assert store.get_viindoo_tenant_id() == by_name

    def test_raises_when_multiple_owners(self, migrated_pg):
        """Data inconsistency: viindoo profiles owned by >1 tenant → fail-closed."""
        store = auth_store()
        conn = migrated_pg
        t1 = _insert_tenant(conn, "Owner A")
        t2 = _insert_tenant(conn, "Owner B")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO profiles (name, odoo_version, tenant_id) "
                "VALUES (%s, %s, %s)",
                ("standard_viindoo_17", "17.0", t1),
            )
            cur.execute(
                "INSERT INTO profiles (name, odoo_version, tenant_id) "
                "VALUES (%s, %s, %s)",
                ("viindoo_internal_17", "17.0", t2),
            )
        if not conn.autocommit:
            conn.commit()

        with pytest.raises(RuntimeError, match="multiple tenants"):
            store.get_viindoo_tenant_id()
