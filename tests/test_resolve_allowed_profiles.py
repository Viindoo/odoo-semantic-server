# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_resolve_allowed_profiles.py
"""WI-3 (ADR-0034) — tenant profile scope resolution.

Two layers:
- ``RepoStore.resolve_tenant_scope(tenant_id)`` → ``(own, shared)`` — DB query
  (postgres-marked integration test). own = tenant's own profiles; shared = all
  globally-shared (tenant_id IS NULL) profiles. ``resolve_allowed_profiles`` is the
  flat union for single-value filters.
- ``session.resolve_tenant_scope`` / ``resolve_allowed_profiles`` — the 60s-cached
  wrappers; ``None`` tenant = admin/unrestricted (pure unit test, no DB).
"""
import pytest

from src.mcp import session

# ---------------------------------------------------------------------------
# Unit tests — session wrappers (no DB)
# ---------------------------------------------------------------------------


def test_none_tenant_is_unrestricted():
    """A None tenant_id (admin / legacy global key) → unrestricted."""
    session.invalidate_allowed_profiles()
    own, shared = session.resolve_tenant_scope(None)
    assert own is None and shared == []
    assert session.resolve_allowed_profiles(None) is None


def test_caches_within_ttl_then_refreshes(monkeypatch):
    """Within the 60s TTL the DB is hit once; after expiry it refreshes."""
    session.invalidate_allowed_profiles()
    calls = {"n": 0}

    class _FakeStore:
        def resolve_tenant_scope(self, tid):
            calls["n"] += 1
            return ["acme_17"], ["odoo_17"]

    monkeypatch.setattr("src.db.pg.repo_store", lambda: _FakeStore())
    clock = {"t": 1000.0}

    a = session.resolve_tenant_scope(7, now_fn=lambda: clock["t"])
    b = session.resolve_tenant_scope(7, now_fn=lambda: clock["t"])  # cached
    assert a == b == (["acme_17"], ["odoo_17"])
    assert calls["n"] == 1, "second call within TTL must hit cache, not DB"

    clock["t"] += 61.0  # expire the 60s entry
    session.resolve_tenant_scope(7, now_fn=lambda: clock["t"])
    assert calls["n"] == 2, "expired entry must refresh from DB"

    # derived union (for single-value filters) excludes nothing the tenant may see
    assert session.resolve_allowed_profiles(7, now_fn=lambda: clock["t"]) == ["acme_17", "odoo_17"]


def test_returned_lists_are_copies(monkeypatch):
    """Mutating returned own/shared must not corrupt the cache."""
    session.invalidate_allowed_profiles()

    class _FakeStore:
        def resolve_tenant_scope(self, tid):
            return ["acme_17"], ["odoo_17"]

    monkeypatch.setattr("src.db.pg.repo_store", lambda: _FakeStore())
    clk = lambda: 0.0  # noqa: E731
    own, shared = session.resolve_tenant_scope(9, now_fn=clk)
    own.append("HACK")
    shared.append("HACK")
    own2, shared2 = session.resolve_tenant_scope(9, now_fn=clk)
    assert "HACK" not in own2 and "HACK" not in shared2


# ---------------------------------------------------------------------------
# Integration tests — RepoStore (postgres)
# ---------------------------------------------------------------------------


@pytest.mark.postgres
class TestResolveTenantScopeDB:
    @staticmethod
    def _cleanup(pg_conn):
        with pg_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM repos WHERE profile_id IN "
                r"(SELECT id FROM profiles WHERE name LIKE 't\_%%')"
            )
            cur.execute(r"DELETE FROM profiles WHERE name LIKE 't\_%%'")
            cur.execute(r"DELETE FROM tenants WHERE name LIKE 't\_%%'")
        if not pg_conn.autocommit:
            pg_conn.commit()

    @pytest.fixture
    def allowed_pg(self, pg_conn):
        from src.db.migrate import run_migrations

        run_migrations(pg_conn)
        self._cleanup(pg_conn)
        yield pg_conn
        self._cleanup(pg_conn)

    @staticmethod
    def _tenant(pg_conn, name: str) -> int:
        with pg_conn.cursor() as cur:
            cur.execute("INSERT INTO tenants (name) VALUES (%s) RETURNING id", (name,))
            tid = cur.fetchone()[0]
        if not pg_conn.autocommit:
            pg_conn.commit()
        return tid

    @staticmethod
    def _profile(pg_conn, name, version, *, tenant_id=None, parent_id=None) -> int:
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO profiles (name, odoo_version, parent_profile_id, tenant_id) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (name, version, parent_id, tenant_id),
            )
            pid = cur.fetchone()[0]
        if not pg_conn.autocommit:
            pg_conn.commit()
        return pid

    def test_own_is_tenant_only_shared_holds_base(self, allowed_pg):
        from src.db.pg import repo_store

        base = self._profile(allowed_pg, "t_base_17", "17.0")  # shared base, tenant NULL
        tid = self._tenant(allowed_pg, "t_acme")
        self._profile(allowed_pg, "t_acme_17", "17.0", tenant_id=tid, parent_id=base)

        own, shared = repo_store().resolve_tenant_scope(tid)
        assert own == ["t_acme_17"], "own must be the tenant's profiles only (NOT the base)"
        assert "t_base_17" in shared, "shared must hold the global base"
        assert "t_acme_17" not in shared
        # flat union (single-value filters) covers both
        assert set(repo_store().resolve_allowed_profiles(tid)) >= {"t_acme_17", "t_base_17"}

    def test_profileless_tenant_own_empty_but_sees_shared(self, allowed_pg):
        from src.db.pg import repo_store

        self._profile(allowed_pg, "t_base_17", "17.0")  # shared base exists
        tid = self._tenant(allowed_pg, "t_ghost")
        own, shared = repo_store().resolve_tenant_scope(tid)
        assert own == [], "profile-less tenant owns nothing"
        assert "t_base_17" in shared, "but still sees the global shared base"

    def test_two_tenants_are_isolated(self, allowed_pg):
        from src.db.pg import repo_store

        base = self._profile(allowed_pg, "t_base_17", "17.0")
        t_a = self._tenant(allowed_pg, "t_alpha")
        t_b = self._tenant(allowed_pg, "t_beta")
        self._profile(allowed_pg, "t_alpha_17", "17.0", tenant_id=t_a, parent_id=base)
        self._profile(allowed_pg, "t_beta_17", "17.0", tenant_id=t_b, parent_id=base)

        own_a, shared_a = repo_store().resolve_tenant_scope(t_a)
        own_b, shared_b = repo_store().resolve_tenant_scope(t_b)
        assert own_a == ["t_alpha_17"] and own_b == ["t_beta_17"]
        # neither tenant's own profile leaks into the other's scope
        assert "t_beta_17" not in own_a and "t_beta_17" not in shared_a
        assert "t_alpha_17" not in own_b and "t_alpha_17" not in shared_b
        # both still see the shared base
        assert "t_base_17" in shared_a and "t_base_17" in shared_b
