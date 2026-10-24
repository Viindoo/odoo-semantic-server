# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 0007: needs_rewrite flags carry their time, and a module without a
ledger row can still be flagged (ADR-0007 W14 note 3), against a real ledger.

- a flag set before the run read its flags is cleared by the run's commit; one
  set after the read (another repo's propagation during the run) survives;
- a propagation that finds a dependent its repo has no ledger row for yet
  leaves a pending entry; the repo's ledger commit turns it into a flag when
  the flag came after the run's read or the module was not re-written, and
  drops it otherwise; a dependent that has a row is flagged directly and gets
  no entry;
- the migration is idempotent by hand; rollback + re-apply are safe, and on the
  rolled-back schema the store clears by name and records nothing (code
  deployed before the migration).
"""
from __future__ import annotations

import time

import pytest

from src.db.migrate import run_migrations
from src.db.module_presence import ModulePresenceStore, ObservedModule

pytestmark = pytest.mark.postgres

V = "99.0"


def _one(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


@pytest.fixture
def migrated(clean_pg):
    run_migrations(clean_pg)
    clean_pg.autocommit = True
    return clean_pg


def _repo(conn, basename: str) -> int:
    pid = _one(conn, "INSERT INTO profiles (name, odoo_version) VALUES (%s, %s) RETURNING id",
               (f"p_{basename}", V))[0]
    return _one(conn, "INSERT INTO repos (profile_id, url, branch, local_path) "
                      "VALUES (%s, %s, %s, %s) RETURNING id",
                (pid, f"file:///x/{basename}.git", V, f"/srv/{basename}"))[0]


def _store():
    return ModulePresenceStore(None, lock_wait_seconds=5.0)  # type: ignore[arg-type]


def _observe(store, conn, repo_id, names):
    store.commit_observed(
        repo_id, profile_name="p", odoo_version=V, head_sha="1" * 40,
        observed=[ObservedModule(name=n, path=n, manifest_file="__manifest__.py")
                  for n in names],
        conn=conn,
    )


def _flag(conn, repo_id, name):
    return _one(conn, "SELECT needs_rewrite FROM module_presence "
                      "WHERE repo_id = %s AND name = %s", (repo_id, name))[0]


def test_a_flag_set_after_the_runs_read_survives_its_clear(migrated):
    store = _store()
    rid = _repo(migrated, "dep_repo")
    _observe(store, migrated, rid, ["old_flag", "new_flag"])
    assert store.mark_needs_rewrite(rid, "old_flag", conn=migrated)
    assert store.mark_needs_rewrite(rid, "new_flag", conn=migrated)

    names, read_at = store.needs_rewrite_snapshot(rid, [V], conn=migrated)
    assert names == ["new_flag", "old_flag"] and read_at is not None
    time.sleep(0.01)
    # Another repo's propagation re-flags new_flag while this run is in progress.
    assert store.mark_dependents_needs_rewrite(V, ["new_flag"], conn=migrated) == [
        (rid, "new_flag"),
    ]

    store.clear_needs_rewrite(rid, names, read_at=read_at, conn=migrated)

    assert _flag(migrated, rid, "old_flag") is False
    assert _flag(migrated, rid, "new_flag") is True, "the flag set during the run survives"
    # Without a snapshot (old callers) the clear is by name, as before.
    store.clear_needs_rewrite(rid, ["new_flag"], conn=migrated)
    assert _flag(migrated, rid, "new_flag") is False


def test_a_dependent_without_a_ledger_row_is_flagged_when_its_row_is_written(migrated):
    store = _store()
    upstream = _repo(migrated, "upstream_repo")
    dep = _repo(migrated, "dep_repo")
    _observe(store, migrated, upstream, ["base_mod"])
    _observe(store, migrated, dep, ["known_dep"])
    # The dependent repo starts its run (reads its flags) ...
    _names, read_at = store.needs_rewrite_snapshot(dep, [V], conn=migrated)
    time.sleep(0.01)
    # ... upstream changes base_mod and walks: known_dep has a row (flagged
    # directly), new_dep and fresh_dep do not (written to the graph only).
    pairs, written = store.flag_dependents(
        V, ["known_dep", "new_dep", "fresh_dep"],
        pending=[("dep_repo", "known_dep"), ("dep_repo", "new_dep"), ("dep_repo", "fresh_dep")],
        exclude_repo_ids=[upstream], conn=migrated,
    )
    assert pairs == [(dep, "known_dep")]
    assert written == 2, "known_dep has a present row: flagged directly, no pending entry"

    # The dependent's ledger commit creates the rows, then applies the entries.
    _observe(store, migrated, dep, ["known_dep", "new_dep", "fresh_dep"])
    applied = store.apply_pending_rewrites(
        dep, V, ["known_dep", "new_dep", "fresh_dep"],
        written={"new_dep", "fresh_dep"}, read_at=read_at, conn=migrated,
    )

    assert applied == ["fresh_dep", "new_dep"], "flagged after the run's read"
    assert _flag(migrated, dep, "new_dep") is True
    assert _one(migrated, "SELECT count(*) FROM module_presence_pending_rewrite")[0] == 0


def test_a_pending_flag_older_than_the_runs_read_is_dropped_for_a_rewritten_module(migrated):
    """The run parsed the module after the upstream change: nothing to redo.
    One it did not re-write keeps the flag."""
    store = _store()
    dep = _repo(migrated, "dep_repo")
    _observe(store, migrated, dep, ["older_mod"])  # the repo has ledger rows at V
    assert store.record_pending_rewrites(
        V, [("dep_repo", "parsed_after"), ("dep_repo", "untouched")], conn=migrated,
    ) == 2
    time.sleep(0.01)
    _names, read_at = store.needs_rewrite_snapshot(dep, [V], conn=migrated)
    _observe(store, migrated, dep, ["older_mod", "parsed_after", "untouched"])

    applied = store.apply_pending_rewrites(
        dep, V, ["parsed_after", "untouched"], written={"parsed_after"}, read_at=read_at,
        conn=migrated,
    )

    assert applied == ["untouched"]
    assert _flag(migrated, dep, "parsed_after") is False
    assert _one(migrated, "SELECT count(*) FROM module_presence_pending_rewrite")[0] == 0


def test_the_backfill_stamps_existing_flags_and_the_migration_reruns(migrated):
    from src.db.migrate import _MIGRATIONS_DIR

    store = _store()
    rid = _repo(migrated, "dep_repo")
    _observe(store, migrated, rid, ["flagged"])
    with migrated.cursor() as cur:
        cur.execute("UPDATE module_presence SET needs_rewrite = TRUE, needs_rewrite_at = NULL "
                    "WHERE repo_id = %s", (rid,))
        cur.execute((_MIGRATIONS_DIR / "0007_module_presence_needs_rewrite_at.sql").read_text())
    stamped, updated = _one(migrated, "SELECT needs_rewrite_at, updated_at FROM module_presence "
                                      "WHERE repo_id = %s", (rid,))
    assert stamped == updated


def test_rollback_falls_back_to_the_clear_by_name_then_reapply(migrated):
    from yoyo import get_backend, read_migrations

    from src.db.migrate import _MIGRATIONS_DIR, _conn_to_uri

    backend = get_backend(_conn_to_uri(migrated))
    target = read_migrations(str(_MIGRATIONS_DIR)).filter(
        lambda m: m.id == "0007_module_presence_needs_rewrite_at")
    assert target, "0007 migration file not found"
    with backend.lock():
        backend.rollback_migrations(backend.to_rollback(target))
    assert _one(migrated, "SELECT to_regclass('public.module_presence_pending_rewrite')")[0] is None

    store = _store()
    rid = _repo(migrated, "dep_repo")
    _observe(store, migrated, rid, ["flagged"])
    assert store.mark_needs_rewrite(rid, "flagged", conn=migrated)
    names, read_at = store.needs_rewrite_snapshot(rid, [V], conn=migrated)
    assert (names, read_at) == (["flagged"], None)
    assert store.record_pending_rewrites(V, [("dep_repo", "x")], conn=migrated) == 0
    store.clear_needs_rewrite(rid, names, read_at=read_at, conn=migrated)
    assert _flag(migrated, rid, "flagged") is False

    run_migrations(migrated)
    store = _store()
    assert store.mark_needs_rewrite(rid, "flagged", conn=migrated)
    assert _one(migrated, "SELECT needs_rewrite_at IS NOT NULL FROM module_presence "
                          "WHERE repo_id = %s", (rid,))[0] is True


def test_same_basename_repos_each_get_and_consume_their_own_entry(migrated):
    """Two checkouts named dep_repo at the version (/srv/a/dep_repo and
    /srv/b/dep_repo): the walk only knows the basename, so both get an entry;
    the first one's ledger commit must not consume the other's."""
    store = _store()
    pid = _one(migrated, "INSERT INTO profiles (name, odoo_version) VALUES ('p_two', %s) "
                         "RETURNING id", (V,))[0]
    first, second = (
        _one(migrated, "INSERT INTO repos (profile_id, url, branch, local_path) "
                       "VALUES (%s, %s, %s, %s) RETURNING id",
             (pid, f"file:///x/{tag}.git", V, f"/srv/{tag}/dep_repo"))[0]
        for tag in ("a", "b")
    )
    _observe(store, migrated, first, ["seen_a"])
    _observe(store, migrated, second, ["seen_b"])

    assert store.record_pending_rewrites(V, [("dep_repo", "new_mod")], conn=migrated) == 2

    _observe(store, migrated, first, ["seen_a", "new_mod"])
    assert store.apply_pending_rewrites(first, V, ["seen_a", "new_mod"], written=set(),
                                        read_at=_one(migrated, "SELECT clock_timestamp()")[0],
                                        conn=migrated) == ["new_mod"]
    assert _one(migrated, "SELECT repo_id FROM module_presence_pending_rewrite")[0] == second
