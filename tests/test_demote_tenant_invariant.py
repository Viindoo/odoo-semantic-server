# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the admin-demotion privilege-persistence gap
(ADR-0034, m13_019; read-side guard follow-up).

THE GAP: demoting an admin (PATCH /api/admin/users/{id}/admin is_admin=false)
flips `webui_users.is_admin` but historically did NOT touch the user's API keys
nor the MCP middleware caches. An admin who minted an unrestricted
`tenant_id IS NULL` key therefore kept UNRESTRICTED cross-tenant read access for
up to the 300 s owner-cache TTL after demotion — the read-side guard
`_is_null_tenant_escalation` could not fire while the cache still served
`owner_is_admin=True`.

THE FIX (write-side, do BOTH):
  1. `AuthStore.set_user_admin` re-scopes the user's ACTIVE, `tenant_id IS NULL`
     keys to a concrete tenant (public / viindoo) in the SAME transaction as the
     is_admin flip when demoting. Fail-closed: a resolver failure rolls back the
     whole txn (is_admin stays unchanged).
  2. The route invalidates the per-key MCP middleware cache for EVERY key the
     user owns (on promote AND demote), so the change takes effect immediately.

Requires PostgreSQL (pytestmark = pytest.mark.postgres). Throwaway DSN only —
`clean_pg` DROPs tables, so this MUST NOT run against the prod DSN.
"""
from __future__ import annotations

import pytest

from src.db.auth_registry import LastAdminProtectedError
from src.db.migrate import run_migrations
from src.db.pg import auth_store

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


def _is_admin(conn, user_id):
    with conn.cursor() as cur:
        cur.execute("SELECT is_admin FROM webui_users WHERE id = %s", (user_id,))
        return bool(cur.fetchone()[0])


# ---------------------------------------------------------------------------
# set_user_admin store-layer re-scope (the headline fix)
# ---------------------------------------------------------------------------


class TestDemoteRescopesActiveNullKeys:
    def test_demote_gmail_admin_rescopes_active_null_key_to_public(self, migrated_pg):
        """Demoting an admin who owns an active NULL-tenant key re-scopes the key
        to the PUBLIC tenant (gmail owner) and keeps it active."""
        store = auth_store()
        # A second admin so the last-admin guard does not fire.
        _insert_user(migrated_pg, "other_admin_a", "other@example.com", is_admin=True)
        uid = _insert_user(migrated_pg, "demote_gmail", "exposed@gmail.com", is_admin=True)
        _, _, key_id = store.create_api_key("admin-null-key", user_id=uid, tenant_id=None)

        affected = store.set_user_admin(uid, is_admin=False)

        db = _key_row(migrated_pg, key_id)
        assert db["active"] is True, "re-scoped key must stay active"
        assert db["tenant_id"] is not None, "must NOT remain unrestricted after demote"
        assert db["tenant_id"] == store.get_public_tenant_id()
        assert _is_admin(migrated_pg, uid) is False
        assert key_id in affected

    def test_demote_viindoo_admin_rescopes_to_viindoo_tenant(self, migrated_pg):
        store = auth_store()
        _insert_user(migrated_pg, "other_admin_v", "other2@example.com", is_admin=True)
        uid = _insert_user(migrated_pg, "demote_v", "dev@viindoo.com", is_admin=True)
        _, _, key_id = store.create_api_key("v-null-key", user_id=uid, tenant_id=None)

        store.set_user_admin(uid, is_admin=False)

        db = _key_row(migrated_pg, key_id)
        assert db["active"] is True
        assert db["tenant_id"] == store.get_viindoo_tenant_id()

    def test_demote_leaves_already_scoped_key_untouched(self, migrated_pg):
        """A key already bound to a concrete tenant keeps its tenant on demote."""
        store = auth_store()
        _insert_user(migrated_pg, "other_admin_s", "other3@example.com", is_admin=True)
        uid = _insert_user(migrated_pg, "demote_scoped", "x@gmail.com", is_admin=True)
        viindoo = store.get_viindoo_tenant_id()
        _, _, key_id = store.create_api_key("scoped-key", user_id=uid, tenant_id=viindoo)

        store.set_user_admin(uid, is_admin=False)

        assert _key_row(migrated_pg, key_id)["tenant_id"] == viindoo

    def test_demote_leaves_inactive_null_key_untouched(self, migrated_pg):
        """Inactive NULL-tenant keys are not re-scoped (the read-side guard only
        gates ACTIVE keys; an inactive key cannot authenticate anyway). They will
        be re-scoped if/when reactivated via reactivate_api_key."""
        store = auth_store()
        _insert_user(migrated_pg, "other_admin_i", "other4@example.com", is_admin=True)
        uid = _insert_user(migrated_pg, "demote_inactive", "y@gmail.com", is_admin=True)
        _, _, key_id = store.create_api_key("inactive-key", user_id=uid, tenant_id=None)
        with migrated_pg.cursor() as cur:
            cur.execute("UPDATE api_keys SET active = FALSE WHERE id = %s", (key_id,))
        if not migrated_pg.autocommit:
            migrated_pg.commit()

        store.set_user_admin(uid, is_admin=False)

        db = _key_row(migrated_pg, key_id)
        assert db["active"] is False
        assert db["tenant_id"] is None

    def test_promote_does_not_rescope_keys(self, migrated_pg):
        """Promoting a non-admin to admin must NOT re-scope their keys."""
        store = auth_store()
        uid = _insert_user(migrated_pg, "promote_me", "p@gmail.com", is_admin=False)
        viindoo = store.get_viindoo_tenant_id()
        _, _, key_id = store.create_api_key("scoped-key2", user_id=uid, tenant_id=viindoo)

        affected = store.set_user_admin(uid, is_admin=True)

        assert _key_row(migrated_pg, key_id)["tenant_id"] == viindoo
        assert _is_admin(migrated_pg, uid) is True
        assert key_id in affected  # still enumerated for cache invalidation

    def test_set_user_admin_returns_all_user_keys(self, migrated_pg):
        """The return value enumerates ALL of the user's keys (active + inactive)
        so the route can invalidate the cache for each."""
        store = auth_store()
        _insert_user(migrated_pg, "other_admin_k", "other5@example.com", is_admin=True)
        uid = _insert_user(migrated_pg, "multi_key", "m@gmail.com", is_admin=True)
        _, _, k1 = store.create_api_key("k1", user_id=uid, tenant_id=None)
        _, _, k2 = store.create_api_key("k2", user_id=uid, tenant_id=None)
        with migrated_pg.cursor() as cur:
            cur.execute("UPDATE api_keys SET active = FALSE WHERE id = %s", (k2,))
        if not migrated_pg.autocommit:
            migrated_pg.commit()

        affected = store.set_user_admin(uid, is_admin=False)

        assert set(affected) == {k1, k2}


# ---------------------------------------------------------------------------
# Fail-closed: resolver failure aborts the demote (is_admin not left flipped)
# ---------------------------------------------------------------------------


class TestDemoteFailClosed:
    def test_demote_fails_closed_when_tenant_missing(self, migrated_pg):
        """If the tenant resolver cannot resolve a tenant (public tenant removed)
        while the user has an active NULL-tenant key, the demote RAISES and rolls
        back — is_admin stays TRUE and the key stays unrestricted (no partial
        state where is_admin was flipped without the matching re-scope)."""
        store = auth_store()
        _insert_user(migrated_pg, "other_admin_fc", "other6@example.com", is_admin=True)
        uid = _insert_user(migrated_pg, "demote_fc", "fc@gmail.com", is_admin=True)
        _, _, key_id = store.create_api_key("fc-key", user_id=uid, tenant_id=None)
        # Remove the public tenant so resolve_default_mint_tenant_id raises.
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM tenants WHERE name = %s", ("public",))
        if not migrated_pg.autocommit:
            migrated_pg.commit()

        with pytest.raises(RuntimeError):
            store.set_user_admin(uid, is_admin=False)

        # Fail-closed: NEITHER the is_admin flip NOR the re-scope was committed.
        assert _is_admin(migrated_pg, uid) is True
        assert _key_row(migrated_pg, key_id)["tenant_id"] is None


# ---------------------------------------------------------------------------
# Last-admin demotion still blocked, no key changes
# ---------------------------------------------------------------------------


class TestLastAdminStillProtected:
    def test_demote_last_admin_raises_and_leaves_key_untouched(self, migrated_pg):
        store = auth_store()
        uid = _insert_user(migrated_pg, "sole_admin", "sole@gmail.com", is_admin=True)
        _, _, key_id = store.create_api_key("sole-key", user_id=uid, tenant_id=None)

        with pytest.raises(LastAdminProtectedError):
            store.set_user_admin(uid, is_admin=False)

        assert _is_admin(migrated_pg, uid) is True
        # No re-scope occurred — key still unrestricted (still admin-owned).
        assert _key_row(migrated_pg, key_id)["tenant_id"] is None


# ---------------------------------------------------------------------------
# Route layer invalidates the MCP middleware cache for every owned key
# ---------------------------------------------------------------------------


class TestDemoteRouteInvalidatesCache:
    @pytest.mark.asyncio
    async def test_demote_route_invalidates_cache_for_each_key(self, migrated_pg, monkeypatch):
        """PATCH .../admin (demote) must call _cache_invalidate_by_key_id for every
        key the user owns so the per-key owner cache refreshes immediately."""
        import httpx

        import src.mcp.middleware as mw
        from src.web_ui.app import create_app

        # Auth bypass (mirror test_admin_users): treat caller as admin.
        monkeypatch.setenv("WEBUI_AUTH_DISABLED", "1")

        store = auth_store()
        _insert_user(migrated_pg, "other_admin_rt", "otherrt@example.com", is_admin=True)
        uid = _insert_user(migrated_pg, "demote_rt", "rt@gmail.com", is_admin=True)
        _, _, k1 = store.create_api_key("rt-k1", user_id=uid, tenant_id=None)
        _, _, k2 = store.create_api_key("rt-k2", user_id=uid, tenant_id=None)

        invalidated: list[int] = []
        orig = mw._cache_invalidate_by_key_id

        def _spy(key_id):
            invalidated.append(key_id)
            return orig(key_id)

        monkeypatch.setattr(mw, "_cache_invalidate_by_key_id", _spy)

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.patch(
                f"/api/admin/users/{uid}/admin",
                json={"is_admin": False},
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["user"]["is_admin"] is False
        assert set(invalidated) == {k1, k2}, (
            f"cache must be invalidated for every owned key; got {invalidated}"
        )
        # And the keys were re-scoped (gmail → public).
        assert _key_row(migrated_pg, k1)["tenant_id"] == store.get_public_tenant_id()
        assert _key_row(migrated_pg, k2)["tenant_id"] == store.get_public_tenant_id()

    @pytest.mark.asyncio
    async def test_promote_route_invalidates_cache_but_keeps_scope(self, migrated_pg, monkeypatch):
        """Promote also invalidates the cache (owner_is_admin flips True) but does
        NOT re-scope keys."""
        import httpx

        import src.mcp.middleware as mw
        from src.web_ui.app import create_app

        monkeypatch.setenv("WEBUI_AUTH_DISABLED", "1")

        store = auth_store()
        uid = _insert_user(migrated_pg, "promote_rt", "prt@gmail.com", is_admin=False)
        viindoo = store.get_viindoo_tenant_id()
        _, _, k1 = store.create_api_key("prt-k1", user_id=uid, tenant_id=viindoo)

        invalidated: list[int] = []
        monkeypatch.setattr(
            mw, "_cache_invalidate_by_key_id", lambda kid: invalidated.append(kid)
        )

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.patch(
                f"/api/admin/users/{uid}/admin",
                json={"is_admin": True},
            )

        assert resp.status_code == 200, resp.text
        assert invalidated == [k1]
        assert _key_row(migrated_pg, k1)["tenant_id"] == viindoo
