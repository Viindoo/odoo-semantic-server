# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_migration_0005_profile_tenant_backfill.py
"""Migration 0005 + the write-side visibility invariant, against real Postgres.

The 0005 body is executed on rows that reproduce the shapes observed on prod
on 2026-10-20 (shared viindoo_internal_19 under a private standard_viindoo_19;
shared standard_viindoo_20 under the shared odoo_20 while standard_viindoo_8..19
are private; repos left shared under private profiles). Business rules:

- every shared profile with a private ancestor gets the NEAREST private
  ancestor's tenant;
- standard_viindoo_20 gets the tenant of the standard_viindoo_<N> family only
  under the owner's 2026-10-20 decision guard (named row, family private to
  ONE tenant); no profile is ever assigned a tenant from its NAME alone, so a
  shared erp_18 next to tenant X's erp_16/erp_17 stays shared;
- a private profile under another tenant's private profile aborts the
  migration with nothing committed;
- a repo's tenant follows its private profile; repos of shared profiles stay;
- the migration is idempotent and its rollback restores exactly the logged rows;
- the read-only audit reports each violation kind and is empty afterwards;
- RepoStore enforces the same invariant on real SQL (create / re-parent /
  tenant change / repo add), with no partial write on refusal.
"""
from __future__ import annotations

import pytest

from src.db.migrate import _MIGRATIONS_DIR, run_migrations

pytestmark = pytest.mark.postgres

V = "99.0"
_BODY = (_MIGRATIONS_DIR / "0005_profile_tenant_backfill.sql").read_text()
_ROLLBACK = (_MIGRATIONS_DIR / "0005_profile_tenant_backfill.rollback.sql").read_text()


@pytest.fixture
def migrated(clean_pg):
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("DELETE FROM admin_audit_log WHERE actor = 'migration:0005'")
    return clean_pg


_LOG = ("SELECT detail->>'table', (detail->>'row_id')::int, detail->>'rule'"
        " FROM admin_audit_log WHERE action = 'profile.tenant_backfill'")
_LOG_COUNT = "SELECT count(*) FROM admin_audit_log WHERE action = 'profile.tenant_backfill'"


def _one(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _tenant(conn, name):
    return _one(conn, "INSERT INTO tenants (name) VALUES (%s) RETURNING id", (name,))[0]


def _profile(conn, name, version=V, tenant=None, parent=None):
    return _one(
        conn,
        "INSERT INTO profiles (name, odoo_version, tenant_id, parent_profile_id)"
        " VALUES (%s, %s, %s, %s) RETURNING id",
        (name, version, tenant, parent),
    )[0]


def _repo(conn, profile_id, url, tenant=None):
    return _one(
        conn,
        "INSERT INTO repos (profile_id, url, branch, local_path, tenant_id)"
        " VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (profile_id, url, V, f"/tmp/{url.rsplit('/', 1)[-1]}", tenant),
    )[0]


def _tenant_of(conn, table, row_id):
    return _one(conn, f"SELECT tenant_id FROM {table} WHERE id = %s", (row_id,))[0]


@pytest.fixture
def prod_shapes(migrated):
    c = migrated
    vt = _tenant(c, "m5_viindoo")
    other = _tenant(c, "m5_other")
    ids = {"vt": vt, "other": other}
    for n in range(8, 20):
        ids[f"odoo_{n}"] = _profile(c, f"m5odoo_{n}", f"{n}.0")
        ids[f"std_{n}"] = _profile(c, f"standard_viindoo_{n}", f"{n}.0", vt, ids[f"odoo_{n}"])
    ids["odoo_20"] = _profile(c, "m5odoo_20", "20.0")
    ids["std_20"] = _profile(c, "standard_viindoo_20", "20.0", None, ids["odoo_20"])  # owner rule
    ids["int_19"] = _profile(c, "m5int_19", "19.0", None, ids["std_19"])        # chain rule
    ids["int_19_kid"] = _profile(c, "m5intkid_19", "19.0", None, ids["int_19"])  # deeper
    ids["std20_kid"] = _profile(c, "m5stdkid_20", "20.0", None, ids["std_20"])   # rule 3
    # a "versioned family" of another customer: tenant `other` owns erp_16 and
    # erp_17, a different customer's erp_18 was created shared -> never guessed
    ids["erp_16"] = _profile(c, "m5erp_16", "16.0", other)
    ids["erp_17"] = _profile(c, "m5erp_17", "17.0", other)
    ids["erp_18"] = _profile(c, "m5erp_18", "18.0")
    ids["r_std17"] = _repo(c, ids["std_17"], "https://x/m5/tvtmaaddons")
    ids["r_odoo17"] = _repo(c, ids["odoo_17"], "https://x/m5/odoo")
    return ids


def test_audit_reports_the_broken_shapes_before_the_fix(prod_shapes):
    from src.db.pg import repo_store

    kinds = {(r["kind"], r["profile_name"]) for r in repo_store().visibility_violations()}
    assert ("shared_child_of_private", "m5int_19") in kinds
    assert ("shared_child_of_private", "m5intkid_19") in kinds
    assert ("repo_tenant_mismatch", "standard_viindoo_17") in kinds


def test_backfill_repairs_every_shape_and_leaves_shared_families(migrated, prod_shapes):
    ids, c = prod_shapes, migrated
    with c.cursor() as cur:
        cur.execute(_BODY)
    vt = ids["vt"]
    for key in ("int_19", "int_19_kid", "std_20", "std20_kid"):
        assert _tenant_of(c, "profiles", ids[key]) == vt, key
    assert _tenant_of(c, "repos", ids["r_std17"]) == vt
    for key in ("odoo_17", "odoo_20", "erp_18"):
        assert _tenant_of(c, "profiles", ids[key]) is None, key
    assert _tenant_of(c, "repos", ids["r_odoo17"]) is None
    rules = {(t, r): rule for t, r, rule in _all(c, _LOG)}
    assert rules[("profiles", ids["int_19"])] == "nearest_private_ancestor"
    assert rules[("profiles", ids["std_20"])] == "owner_decision"
    assert ("profiles", ids["erp_18"]) not in rules
    assert rules[("profiles", ids["std20_kid"])] == "nearest_private_ancestor"
    assert rules[("repos", ids["r_std17"])] == "repo_follows_profile"
    from src.db.pg import repo_store
    assert repo_store().visibility_violations() == []


def test_name_alone_never_assigns_a_tenant(migrated):
    """A shared profile is not handed to a tenant because of its name."""
    c = migrated
    x = _tenant(c, "m5n_x")
    _profile(c, "m5n_erp_16", "16.0", x)
    _profile(c, "m5n_erp_17", "17.0", x)
    straggler = _profile(c, "m5n_erp_18", "18.0")
    with c.cursor() as cur:
        cur.execute(_BODY)
    assert _tenant_of(c, "profiles", straggler) is None
    assert _one(c, _LOG_COUNT)[0] == 0


@pytest.mark.parametrize("sibling_tenant", ["other", "shared"])
def test_owner_decision_row_needs_a_single_tenant_family(migrated, sibling_tenant):
    """standard_viindoo_20 stays shared when the family is mixed or partly shared."""
    c = migrated
    vt = _tenant(c, "m5o_vt")
    other = _tenant(c, "m5o_other")
    for n in (17, 18):
        _profile(c, f"standard_viindoo_{n}", f"{n}.0", vt)
    _profile(c, "standard_viindoo_19", "19.0", other if sibling_tenant == "other" else None)
    std20 = _profile(c, "standard_viindoo_20", "20.0")
    with c.cursor() as cur:
        cur.execute(_BODY)
    assert _tenant_of(c, "profiles", std20) is None


def test_owner_decision_row_absent_changes_nothing(migrated):
    """Another deployment (no standard_viindoo_20) gets no owner-decision change."""
    c = migrated
    vt = _tenant(c, "m5a_vt")
    for n in (17, 18, 19):
        _profile(c, f"standard_viindoo_{n}", f"{n}.0", vt)
    lookalike = _profile(c, "standard_viindoo_20x", "20.0")
    with c.cursor() as cur:
        cur.execute(_BODY)
    assert _tenant_of(c, "profiles", lookalike) is None
    assert _one(c, _LOG_COUNT)[0] == 0


def test_cross_tenant_chain_aborts_with_nothing_committed(migrated):
    """A private profile under ANOTHER tenant's private profile fails the migration."""
    import psycopg2

    c = migrated
    a = _tenant(c, "m5x_a")
    b = _tenant(c, "m5x_b")
    top = _profile(c, "m5x_top_99", V, a)
    _profile(c, "m5x_foreign_99", V, b, top)
    leak = _profile(c, "m5x_leak_99", V, None, top)  # rule 1 would repair this
    with pytest.raises(psycopg2.Error, match="cross_tenant_chain m5x_foreign_99"):
        with c.cursor() as cur:
            cur.execute(_BODY)
    if not c.autocommit:
        c.rollback()
    assert _tenant_of(c, "profiles", leak) is None, "the whole migration rolled back"
    assert _one(c, _LOG_COUNT)[0] == 0


def _all(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def test_backfill_is_idempotent(migrated, prod_shapes):
    with migrated.cursor() as cur:
        cur.execute(_BODY)
        n = _one(migrated, _LOG_COUNT)[0]
        cur.execute(_BODY)
    assert _one(migrated, _LOG_COUNT)[0] == n > 0


def test_rollback_restores_exactly_the_logged_rows(migrated, prod_shapes):
    ids, c = prod_shapes, migrated
    with c.cursor() as cur:
        cur.execute(_BODY)
        cur.execute(_ROLLBACK)
    assert _tenant_of(c, "profiles", ids["int_19"]) is None
    assert _tenant_of(c, "profiles", ids["std_20"]) is None
    assert _tenant_of(c, "repos", ids["r_std17"]) is None
    assert _tenant_of(c, "profiles", ids["std_19"]) == ids["vt"], "untouched rows stay"
    assert _one(c, "SELECT count(*) FROM admin_audit_log"
                   " WHERE action = 'profile.tenant_backfill.rollback'")[0] == 1


# ---------------------------------------------------------------------------
# RepoStore invariant on real SQL
# ---------------------------------------------------------------------------

def test_store_refuses_ownerless_and_more_visible_profiles(migrated):
    from src.db.exceptions import ProfileTenantRequiredError, ProfileVisibilityError
    from src.db.pg import repo_store

    vt = _tenant(migrated, "m5s_viindoo")
    store = repo_store()
    before = _one(migrated, "SELECT count(*) FROM profiles")[0]
    with pytest.raises(ProfileTenantRequiredError):
        store.add_profile("m5s_orphan_99", V)
    parent = store.add_profile("m5s_std_99", V, tenant_id=vt)
    with pytest.raises(ProfileVisibilityError):
        store.add_profile("m5s_pub_99", V, parent_id=parent, shared=True)
    assert _one(migrated, "SELECT count(*) FROM profiles")[0] == before + 1
    child = store.add_profile("m5s_int_99", V, parent_id=parent)
    assert _tenant_of(migrated, "profiles", child) == vt
    shared = store.add_profile("m5s_shared_99", V, shared=True)
    with pytest.raises(ProfileVisibilityError):
        store.set_profile_parent(shared, parent)
    repo = store.add_repo(child, "https://x/m5s/repo", V, "/tmp/m5s_repo")
    assert _tenant_of(migrated, "repos", repo) == vt
    with pytest.raises(ProfileVisibilityError):
        store.set_profile_tenant(child, None, confirm_shared=True)  # under a private parent
    assert _tenant_of(migrated, "profiles", child) == vt
    moved = store.set_profile_tenant(shared, vt)
    assert moved["changed"] is True
    assert store.visibility_violations() == []


def test_repo_tenant_resync_cannot_race_a_profile_tenant_change(migrated):
    """set_repo_tenant waits for a concurrent profile tenant change and then
    refuses the stale tenant - the repo never ends on the old tenant."""
    import threading

    import psycopg2

    from src.db.exceptions import ProfileVisibilityError
    from src.db.pg import repo_store
    from tests.conftest import get_test_dsn

    c = migrated
    a = _tenant(c, "m5r_a")
    b = _tenant(c, "m5r_b")
    pid = _profile(c, "m5r_p_99", V, a)
    rid = _repo(c, pid, "https://x/m5r/repo", a)
    mover = psycopg2.connect(get_test_dsn())  # an in-flight set_profile_tenant(pid, b)
    outcome: dict = {}
    try:
        with mover.cursor() as cur:
            cur.execute("SELECT 1 FROM profiles WHERE id = %s FOR UPDATE", (pid,))
            cur.execute("UPDATE profiles SET tenant_id = %s WHERE id = %s", (b, pid))
            cur.execute("UPDATE repos SET tenant_id = %s WHERE profile_id = %s", (b, pid))

        def resync():
            try:
                outcome["result"] = repo_store().set_repo_tenant(rid, a)
            except Exception as e:  # noqa: BLE001 - asserted below
                outcome["error"] = e

        t = threading.Thread(target=resync)
        t.start()
        t.join(1.0)
        mover.commit()
        t.join(30)
        assert not t.is_alive()
    finally:
        mover.rollback()
        mover.close()
    assert isinstance(outcome.get("error"), ProfileVisibilityError), outcome
    assert _tenant_of(c, "repos", rid) == b
    assert repo_store().visibility_violations() == []
