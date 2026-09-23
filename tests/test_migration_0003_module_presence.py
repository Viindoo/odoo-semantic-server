# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_migration_0003_module_presence.py
"""Migration 0003_module_presence (ADR-0056, OSM #378) - schema behaviour.

These tests talk to the schema with raw SQL only (no ModulePresenceStore), so
they protect the B1 contract independently of the B2 store:

- the migration is idempotent and survives a yoyo rollback + re-apply;
- the lifecycle history outlives the repo and the profile that owned it
  (H3: repo_id ON DELETE SET NULL + denormalized repo identity);
- the ledger forbids the impossible states of the lifecycle state machine
  (retired+pending, excluded without reason, ...) and keeps one row per
  (repo, technical name) while a deleted repo's history rows never collide;
- RLS: osm_reader reads only the profiles listed in app.allowed_profiles,
  '*' reads everything, an unset GUC reads nothing (R15, PG half);
- update_repo_status does not clobber the new repos lifecycle columns (H4).
"""
from __future__ import annotations

import psycopg2
import psycopg2.errors
import pytest

from src.db.migrate import run_migrations
from tests.conftest import drop_osm_reader, ensure_osm_reader_or_skip

pytestmark = pytest.mark.postgres

V = "99.0"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _one(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _all(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _require_ledger(conn) -> None:
    """Assert (not error) that the ledger exists - keeps a reverted tree an assertion failure."""
    assert _one(conn, "SELECT to_regclass('public.module_presence')")[0] is not None, (
        "module_presence ledger table does not exist after run_migrations"
    )


def _profile(conn, name: str, version: str = V) -> int:
    return _one(
        conn,
        "INSERT INTO profiles (name, odoo_version) VALUES (%s, %s) RETURNING id",
        (name, version),
    )[0]


def _repo(conn, profile_id: int, url: str, local_path: str, branch: str = V) -> int:
    return _one(
        conn,
        "INSERT INTO repos (profile_id, url, branch, local_path) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (profile_id, url, branch, local_path),
    )[0]


def _ledger_row(conn, *, repo_id, name, profile_name, state="present",
                repo_url="git@github.com:Viindoo/tvtmaaddons.git",
                repo_basename="tvtmaaddons", repo_branch=V, version=V,
                exclusion_reason=None, retire_reason=None,
                retire_pending=False, retire_pending_reason=None,
                successor_names=None, successor_source=None) -> int:
    return _one(
        conn,
        """
        INSERT INTO module_presence (
            repo_id, repo_url, repo_basename, repo_branch, profile_name,
            odoo_version, name, path, manifest_file, state, exclusion_reason,
            retire_reason, retire_pending, retire_pending_reason,
            successor_names, successor_source,
            first_seen_sha, first_seen_at, last_seen_sha, last_seen_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, '__manifest__.py', %s, %s,
                %s, %s, %s, %s, %s, 'sha1', NOW(), 'sha1', NOW())
        RETURNING id
        """,
        (repo_id, repo_url, repo_basename, repo_branch, profile_name, version,
         name, name, state, exclusion_reason, retire_reason, retire_pending,
         retire_pending_reason, successor_names, successor_source),
    )[0]


def _migrations_uri_and_list(conn):
    from yoyo import get_backend, read_migrations

    from src.db.migrate import _MIGRATIONS_DIR, _conn_to_uri

    backend = get_backend(_conn_to_uri(conn))
    return backend, read_migrations(str(_MIGRATIONS_DIR))


@pytest.fixture
def migrated(clean_pg):
    run_migrations(clean_pg)
    return clean_pg


# ---------------------------------------------------------------------------
# Idempotency + rollback
# ---------------------------------------------------------------------------

def test_migration_applies_twice_without_error_and_records_0003_once(migrated):
    """A second run_migrations on an already-migrated DB is a clean no-op."""
    _require_ledger(migrated)
    run_migrations(migrated)
    ids = [r[0] for r in _all(migrated, "SELECT migration_id FROM _yoyo_migration")]
    assert ids.count("0003_module_presence") == 1, ids
    _require_ledger(migrated)


def test_rerun_migration_body_on_existing_ledger_is_idempotent(migrated):
    """Executing the 0003 SQL again on a DB that already has it must not raise.

    Deploy reality: ops may re-run the file by hand (psql -f) or a restored
    backup may carry the table without the yoyo row.
    """
    from src.db.migrate import _MIGRATIONS_DIR

    _require_ledger(migrated)
    body = (_MIGRATIONS_DIR / "0003_module_presence.sql").read_text()
    with migrated.cursor() as cur:
        cur.execute(body)
        cur.execute(body)
    _require_ledger(migrated)


def test_rollback_removes_ledger_and_repo_columns_then_reapply_restores_them(migrated):
    _require_ledger(migrated)
    backend, migrations = _migrations_uri_and_list(migrated)
    target = migrations.filter(lambda m: m.id == "0003_module_presence")
    assert target, "0003_module_presence migration file not found"
    with backend.lock():
        backend.rollback_migrations(backend.to_rollback(target))

    assert _one(migrated, "SELECT to_regclass('public.module_presence')")[0] is None
    cols = {r[0] for r in _all(
        migrated,
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'repos'",
    )}
    assert not cols & {"presence_head_sha", "lifecycle_attention", "lifecycle_attention_at"}, cols
    # Rolling back 0003 must not touch the earlier baseline.
    assert _one(migrated, "SELECT to_regclass('public.repos')")[0] is not None

    run_migrations(migrated)
    _require_ledger(migrated)
    cols = {r[0] for r in _all(
        migrated,
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'repos'",
    )}
    assert {"presence_head_sha", "lifecycle_attention", "lifecycle_attention_at"} <= cols


def test_ledger_fk_is_restored_when_repos_was_dropped_under_a_surviving_ledger(migrated):
    """DROP TABLE repos CASCADE drops only the FK; re-migrating must re-add it.

    Otherwise a later repo delete would no longer null repo_id and the ledger
    would point at recycled repo ids (H3 silently lost).
    """
    _require_ledger(migrated)
    with migrated.cursor() as cur:
        cur.execute("DROP TABLE repos CASCADE")
        cur.execute("DELETE FROM _yoyo_migration")
    run_migrations(migrated)
    fks = _all(
        migrated,
        """
        SELECT confdeltype FROM pg_constraint
         WHERE conrelid = 'module_presence'::regclass AND contype = 'f'
           AND confrelid = 'repos'::regclass
        """,
    )
    assert fks == [("n",)], f"expected one ON DELETE SET NULL FK to repos, got {fks}"


def test_reapplying_migration_nulls_dangling_repo_id_and_keeps_denormalized_history(migrated):
    """DROP TABLE repos CASCADE (or a partial restore that recreates repos
    without the old rows) drops only the FK constraint object - it does not
    fire the constraint's ON DELETE SET NULL action row-by-row, because that
    action only fires on a real DELETE FROM repos, never on a DDL-level table
    drop. So unlike the empty-ledger case above, a ledger that still holds
    rows referencing the dropped repo is left with repo_id pointing at an id
    that no longer exists in repos.

    Re-applying 0003 on that dangling state must not abort the whole
    migration run with ForeignKeyViolation (the guarded ADD CONSTRAINT
    validates every existing row by default): it must null exactly the
    dangling repo_id first, replaying what ON DELETE SET NULL would have
    done had it fired, while the denormalized repo_url/repo_basename/
    repo_branch/profile_name history on the row stays intact (H3) - and stay
    a no-op on further re-runs (idempotency).
    """
    _require_ledger(migrated)
    pid = _profile(migrated, "mp_dangling")
    rid = _repo(migrated, pid, "git@github.com:Viindoo/tvtmaaddons.git",
                "/srv/h/tvtmaaddons")
    row_id = _ledger_row(migrated, repo_id=rid, name="test_viin_pylint",
                          profile_name="mp_dangling")

    with migrated.cursor() as cur:
        cur.execute("DROP TABLE repos CASCADE")
        cur.execute("DELETE FROM _yoyo_migration")

    # repos is recreated empty by 0001 before 0003 re-runs, so this row's
    # repo_id now dangles. Must not raise ForeignKeyViolation.
    run_migrations(migrated)

    row = _one(
        migrated,
        "SELECT repo_id, repo_url, repo_basename, repo_branch, profile_name, name "
        "FROM module_presence WHERE id = %s",
        (row_id,),
    )
    assert row == (
        None, "git@github.com:Viindoo/tvtmaaddons.git", "tvtmaaddons", V,
        "mp_dangling", "test_viin_pylint",
    ), "dangling repo_id must be nulled while its denormalized history survives"

    fks = _all(
        migrated,
        """
        SELECT confdeltype FROM pg_constraint
         WHERE conrelid = 'module_presence'::regclass AND contype = 'f'
           AND confrelid = 'repos'::regclass
        """,
    )
    assert fks == [("n",)], f"expected one ON DELETE SET NULL FK to repos, got {fks}"

    # Idempotency: repo_id is already NULL, so neither a second run_migrations()
    # nor two more raw executions of the migration body may raise or move it.
    run_migrations(migrated)
    from src.db.migrate import _MIGRATIONS_DIR

    body = (_MIGRATIONS_DIR / "0003_module_presence.sql").read_text()
    with migrated.cursor() as cur:
        cur.execute(body)
        cur.execute(body)

    still_null = _one(migrated, "SELECT repo_id FROM module_presence WHERE id = %s", (row_id,))
    assert still_null == (None,)


# ---------------------------------------------------------------------------
# H3 - history survives repo and profile deletion
# ---------------------------------------------------------------------------

def test_deleting_repo_keeps_ledger_history_with_repo_identity(migrated):
    from src.db.pg import get_pool
    from src.db.repo_registry import RepoStore

    _require_ledger(migrated)
    pid = _profile(migrated, "mp_h3_repo")
    rid = _repo(migrated, pid, "git@github.com:Viindoo/tvtmaaddons.git",
                "/srv/repos/tvtmaaddons")
    _ledger_row(migrated, repo_id=rid, name="test_pylint", profile_name="mp_h3_repo")

    RepoStore(get_pool()).delete_repo(rid)

    rows = _all(
        migrated,
        "SELECT repo_id, repo_url, repo_basename, repo_branch, profile_name, name "
        "FROM module_presence WHERE name = 'test_pylint'",
    )
    assert rows == [(None, "git@github.com:Viindoo/tvtmaaddons.git", "tvtmaaddons", V,
                     "mp_h3_repo", "test_pylint")]


def test_deleting_profile_keeps_ledger_history_of_all_its_repos(migrated):
    from src.db.pg import get_pool
    from src.db.repo_registry import RepoStore

    _require_ledger(migrated)
    pid = _profile(migrated, "mp_h3_profile")
    r1 = _repo(migrated, pid, "git@github.com:Viindoo/tvtmaaddons.git", "/srv/a/tvtmaaddons")
    r2 = _repo(migrated, pid, "git@github.com:Viindoo/odoo-tvtma.git", "/srv/a/odoo-tvtma")
    _ledger_row(migrated, repo_id=r1, name="viin_ai_rag", profile_name="mp_h3_profile")
    _ledger_row(migrated, repo_id=r2, name="viin_ai_agent", profile_name="mp_h3_profile",
                repo_url="git@github.com:Viindoo/odoo-tvtma.git", repo_basename="odoo-tvtma")

    RepoStore(get_pool()).delete_profile(pid)

    rows = _all(
        migrated,
        "SELECT repo_id, profile_name, repo_basename, name FROM module_presence ORDER BY name",
    )
    assert rows == [
        (None, "mp_h3_profile", "odoo-tvtma", "viin_ai_agent"),
        (None, "mp_h3_profile", "tvtmaaddons", "viin_ai_rag"),
    ]


def test_two_deleted_repos_with_same_module_name_both_keep_history(migrated):
    """Two repos (e.g. two branches of one addons repo) owning the same name:
    after both are deleted their history rows (repo_id NULL) must not collide
    on the one-row-per-(repo, name) key."""
    _require_ledger(migrated)
    pid = _profile(migrated, "mp_h3_twin")
    r1 = _repo(migrated, pid, "git@github.com:Viindoo/tvtmaaddons.git", "/srv/b/t1", "17.0")
    r2 = _repo(migrated, pid, "git@github.com:Viindoo/tvtmaaddons.git", "/srv/b/t2", "17.0-dev")
    _ledger_row(migrated, repo_id=r1, name="viin_ai_skill", profile_name="mp_h3_twin")
    _ledger_row(migrated, repo_id=r2, name="viin_ai_skill", profile_name="mp_h3_twin")
    with migrated.cursor() as cur:
        cur.execute("DELETE FROM repos WHERE id IN (%s, %s)", (r1, r2))
    n = _one(migrated, "SELECT count(*) FROM module_presence WHERE name = 'viin_ai_skill' "
                       "AND repo_id IS NULL")[0]
    assert n == 2


def test_one_row_per_repo_and_module_name(migrated):
    """M2: the ledger never holds two rows for one name inside one repo."""
    _require_ledger(migrated)
    pid = _profile(migrated, "mp_unique")
    rid = _repo(migrated, pid, "https://github.com/odoo/odoo.git", "/srv/c/odoo")
    _ledger_row(migrated, repo_id=rid, name="point_of_sale", profile_name="mp_unique")
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _ledger_row(migrated, repo_id=rid, name="point_of_sale", profile_name="mp_unique")


# ---------------------------------------------------------------------------
# State-machine invariants enforced by the schema
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "overrides, rule",
    [
        ({"state": "retired", "retire_reason": "absent",
          "retire_pending": True, "retire_pending_reason": "absent"},
         "a retired row is never still pending"),
        ({"state": "excluded"}, "excluded needs an exclusion_reason"),
        ({"state": "present", "exclusion_reason": "license_skip"},
         "present carries no exclusion_reason"),
        ({"state": "excluded", "exclusion_reason": "deprecated"},
         "exclusion_reason is one of installable_false/license_skip/unparseable"),
        ({"state": "retired"}, "retired needs a retire_reason"),
        ({"state": "retired", "retire_reason": "renamed"},
         "retire_reason is one of absent/repo_removed/orphan_sweep"),
        ({"retire_pending": True}, "pending needs a pending reason"),
        ({"successor_names": ["test_viin_pylint"]}, "successor names need a source"),
        ({"successor_source": "git_rename"}, "successor source needs names"),
        ({"state": "gone"}, "state is present/excluded/retired"),
    ],
)
def test_ledger_rejects_impossible_lifecycle_states(migrated, overrides, rule):
    _require_ledger(migrated)
    pid = _profile(migrated, "mp_ck")
    rid = _repo(migrated, pid, "https://github.com/odoo/odoo.git", "/srv/d/odoo")
    with pytest.raises(psycopg2.errors.CheckViolation):
        _ledger_row(migrated, repo_id=rid, name="payment_ogone", profile_name="mp_ck",
                    **overrides)


@pytest.mark.parametrize("reason", ["installable_false", "license_skip", "unparseable"])
def test_ledger_accepts_each_exclusion_reason(migrated, reason):
    _require_ledger(migrated)
    pid = _profile(migrated, "mp_ok")
    rid = _repo(migrated, pid, "https://github.com/odoo/odoo.git", "/srv/e/odoo")
    _ledger_row(migrated, repo_id=rid, name="hw_posbox_homepage", profile_name="mp_ok",
                state="excluded", exclusion_reason=reason)


# ---------------------------------------------------------------------------
# H4 - repos lifecycle columns are not clobbered by the status writer
# ---------------------------------------------------------------------------

def test_update_repo_status_leaves_lifecycle_columns_untouched(migrated):
    from src.db.pg import get_pool
    from src.db.repo_registry import RepoStore

    _require_ledger(migrated)
    pid = _profile(migrated, "mp_h4")
    rid = _repo(migrated, pid, "git@github.com:Viindoo/tvtmaaddons.git", "/srv/f/t")
    with migrated.cursor() as cur:
        cur.execute(
            "UPDATE repos SET presence_head_sha = 'abc', "
            "lifecycle_attention = 'G-A tripped', lifecycle_attention_at = NOW() "
            "WHERE id = %s", (rid,),
        )
    RepoStore(get_pool()).update_repo_status(rid, "indexed")
    row = _one(migrated, "SELECT status, presence_head_sha, lifecycle_attention, "
                         "lifecycle_attention_at IS NOT NULL FROM repos WHERE id = %s", (rid,))
    assert row == ("indexed", "abc", "G-A tripped", True)


# ---------------------------------------------------------------------------
# RLS (R15, PG half) - osm_reader sees only the GUC-listed profiles
# ---------------------------------------------------------------------------

@pytest.fixture
def reader_ledger(clean_pg):
    """Migrated DB with osm_reader created BEFORE migrating (production deploy
    order), two tenants' ledger rows seeded, and SET ROLE osm_reader possible."""
    ensure_osm_reader_or_skip(clean_pg)
    try:
        with clean_pg.cursor() as cur:
            cur.execute("GRANT osm_reader TO CURRENT_USER")
    except psycopg2.errors.InsufficientPrivilege as exc:
        drop_osm_reader(clean_pg)
        pytest.skip(f"cannot become osm_reader: {exc}")
    run_migrations(clean_pg)
    _require_ledger(clean_pg)
    pa = _profile(clean_pg, "rls_mp_acme")
    pb = _profile(clean_pg, "rls_mp_globex")
    ra = _repo(clean_pg, pa, "git@github.com:acme/addons.git", "/srv/g/acme")
    rb = _repo(clean_pg, pb, "git@github.com:globex/addons.git", "/srv/g/globex")
    _ledger_row(clean_pg, repo_id=ra, name="acme_sale", profile_name="rls_mp_acme")
    _ledger_row(clean_pg, repo_id=rb, name="globex_sale", profile_name="rls_mp_globex")
    yield clean_pg
    with clean_pg.cursor() as cur:
        cur.execute("RESET ROLE")
    drop_osm_reader(clean_pg)


def _names_as_reader(conn, guc: str | None) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute("SET LOCAL ROLE osm_reader")
        if guc is not None:
            cur.execute("SELECT set_config('app.allowed_profiles', %s, true)", (guc,))
        cur.execute("SELECT name FROM module_presence ORDER BY name")
        names = [r[0] for r in cur.fetchall()]
        cur.execute("ROLLBACK")
    return names


def test_osm_reader_has_select_but_no_write_on_ledger(reader_ledger):
    privs = {
        p: _one(reader_ledger, "SELECT has_table_privilege('osm_reader', 'module_presence', %s)",
                (p,))[0]
        for p in ("SELECT", "INSERT", "UPDATE", "DELETE")
    }
    assert privs == {"SELECT": True, "INSERT": False, "UPDATE": False, "DELETE": False}


def test_osm_reader_sees_only_its_allowed_profile(reader_ledger):
    assert _names_as_reader(reader_ledger, "rls_mp_acme") == ["acme_sale"]


def test_osm_reader_with_several_allowed_profiles_sees_each(reader_ledger):
    assert _names_as_reader(reader_ledger, "rls_mp_acme,rls_mp_globex") == [
        "acme_sale", "globex_sale"]


def test_osm_reader_admin_sentinel_sees_all_profiles(reader_ledger):
    assert _names_as_reader(reader_ledger, "*") == ["acme_sale", "globex_sale"]


def test_osm_reader_without_guc_sees_nothing(reader_ledger):
    assert _names_as_reader(reader_ledger, None) == []
    assert _names_as_reader(reader_ledger, "") == []


def test_osm_reader_cannot_see_rows_of_a_deleted_profile_unless_admin(reader_ledger):
    """History rows of a deleted profile stay fail-closed for tenants (only '*')."""
    with reader_ledger.cursor() as cur:
        cur.execute("DELETE FROM profiles WHERE name = 'rls_mp_globex'")
    assert _names_as_reader(reader_ledger, "rls_mp_acme") == ["acme_sale"]
    assert _names_as_reader(reader_ledger, "*") == ["acme_sale", "globex_sale"]


def test_ops_regrant_script_grants_select_on_ledger(clean_pg):
    """ops/rls_create_osm_reader.sql (run by the regrant script after every
    migration) must grant SELECT on the ledger, not only the migration body."""
    from pathlib import Path

    run_migrations(clean_pg)
    _require_ledger(clean_pg)
    ensure_osm_reader_or_skip(clean_pg)
    try:
        with clean_pg.cursor() as cur:
            cur.execute("REVOKE ALL ON module_presence FROM osm_reader")
        # The file is a psql script (\\set, :'osm_pw' variables, ALTER ROLE on a
        # cluster-wide role), so execute only its DO blocks that grant on the
        # ledger - the exact statements the regrant script runs.
        text = (Path(__file__).parent.parent / "ops" / "rls_create_osm_reader.sql").read_text()
        blocks = [
            "DO $$" + b.split("END $$;")[0] + "END $$;"
            for b in text.split("DO $$")[1:]
            if "module_presence" in b.split("END $$;")[0]
        ]
        assert blocks, "ops/rls_create_osm_reader.sql has no grant block for module_presence"
        with clean_pg.cursor() as cur:
            for block in blocks:
                cur.execute(block)
        granted = _one(
            clean_pg, "SELECT has_table_privilege('osm_reader', 'module_presence', 'SELECT')"
        )[0]
        assert granted is True
    finally:
        drop_osm_reader(clean_pg)
