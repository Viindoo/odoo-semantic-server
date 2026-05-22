# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_resolve_allowed_profiles.py
"""WI-3 (ADR-0034) — resolve_allowed_profiles: tenant -> allowed profile names.

Two layers:
- RepoStore.resolve_allowed_profiles(tenant_id): the recursive-CTE DB query
  (postgres-marked integration test).
- session.resolve_allowed_profiles(tenant_id): the 60s-cached wrapper, None =
  admin/unrestricted (pure unit test, no DB).
"""
import pytest

from src.mcp import session

# ---------------------------------------------------------------------------
# Unit tests — session wrapper (no DB)
# ---------------------------------------------------------------------------


def test_none_tenant_is_unrestricted():
    """A None tenant_id (admin / legacy global key) → None (no profile filter)."""
    session.invalidate_allowed_profiles()
    assert session.resolve_allowed_profiles(None) is None


def test_caches_within_ttl_then_refreshes(monkeypatch):
    """Within the 60s TTL the DB is hit once; after expiry it refreshes."""
    session.invalidate_allowed_profiles()
    calls = {"n": 0}

    class _FakeStore:
        def resolve_allowed_profiles(self, tid):
            calls["n"] += 1
            return ["acme_17", "odoo_17"]

    monkeypatch.setattr("src.db.pg.repo_store", lambda: _FakeStore())
    clock = {"t": 1000.0}

    a = session.resolve_allowed_profiles(7, now_fn=lambda: clock["t"])
    b = session.resolve_allowed_profiles(7, now_fn=lambda: clock["t"])  # cached
    assert a == b == ["acme_17", "odoo_17"]
    assert calls["n"] == 1, "second call within TTL must hit cache, not DB"

    clock["t"] += 61.0  # expire the 60s entry
    session.resolve_allowed_profiles(7, now_fn=lambda: clock["t"])
    assert calls["n"] == 2, "expired entry must refresh from DB"


def test_returned_list_is_a_copy(monkeypatch):
    """Mutating the returned list must not corrupt the cache."""
    session.invalidate_allowed_profiles()

    class _FakeStore:
        def resolve_allowed_profiles(self, tid):
            return ["acme_17", "odoo_17"]

    monkeypatch.setattr("src.db.pg.repo_store", lambda: _FakeStore())
    clock = {"t": 0.0}
    first = session.resolve_allowed_profiles(9, now_fn=lambda: clock["t"])
    first.append("HACKED")
    second = session.resolve_allowed_profiles(9, now_fn=lambda: clock["t"])
    assert "HACKED" not in second


# ---------------------------------------------------------------------------
# Integration tests — RepoStore CTE (postgres)
# ---------------------------------------------------------------------------


@pytest.mark.postgres
class TestResolveAllowedProfilesDB:
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

    def test_includes_own_profiles_and_shared_base_ancestor(self, allowed_pg):
        from src.db.pg import repo_store

        base = self._profile(allowed_pg, "t_base_17", "17.0")  # shared base, tenant NULL
        tid = self._tenant(allowed_pg, "t_acme")
        self._profile(allowed_pg, "t_acme_17", "17.0", tenant_id=tid, parent_id=base)

        names = repo_store().resolve_allowed_profiles(tid)
        # tenant's own profile + its shared-base ancestor
        assert set(names) == {"t_acme_17", "t_base_17"}

    def test_profileless_tenant_is_deny_all(self, allowed_pg):
        from src.db.pg import repo_store

        tid = self._tenant(allowed_pg, "t_ghost")
        assert repo_store().resolve_allowed_profiles(tid) == []

    def test_two_tenants_are_isolated(self, allowed_pg):
        from src.db.pg import repo_store

        base = self._profile(allowed_pg, "t_base_17", "17.0")
        t_a = self._tenant(allowed_pg, "t_alpha")
        t_b = self._tenant(allowed_pg, "t_beta")
        self._profile(allowed_pg, "t_alpha_17", "17.0", tenant_id=t_a, parent_id=base)
        self._profile(allowed_pg, "t_beta_17", "17.0", tenant_id=t_b, parent_id=base)

        a = set(repo_store().resolve_allowed_profiles(t_a))
        b = set(repo_store().resolve_allowed_profiles(t_b))
        assert "t_alpha_17" in a and "t_alpha_17" not in b
        assert "t_beta_17" in b and "t_beta_17" not in a
        # both still see the shared base
        assert "t_base_17" in a and "t_base_17" in b
