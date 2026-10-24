# SPDX-License-Identifier: AGPL-3.0-or-later
"""needs_rewrite flags carry their time; a run clears only what it read (no DB).

Business rules protected here (ADR-0007 W14 note 3, migration 0007; the real
SQL runs in ``tests/test_migration_0007_needs_rewrite_at.py``, PostgreSQL):

- every writer of the flag stamps ``needs_rewrite_at`` with the PostgreSQL
  clock; the run's clear is limited to flags stamped at or before the clock
  of its own read, so a flag another repo's propagation sets during the run
  survives (the clear by name used to drop it);
- the read is taken under the version ledger lock every flag writer takes;
- before migration 0007 the store behaves as before (no stamp, clear by name,
  no pending entry) instead of failing the nightly run;
- ``_index_repo`` hands the read's clock to the ledger commit's clear and to
  the application of pending flags.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.db.module_presence import ModulePresenceStore

T0 = dt.datetime(2026, 10, 24, 0, 5, tzinfo=dt.UTC)


class _Cursor:
    def __init__(self, conn) -> None:
        self.conn = conn
        self._rows: list = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def execute(self, sql, params=None) -> None:
        self.conn.log.append((" ".join(sql.split()), params))
        if "information_schema.columns" in sql:
            self._rows = [(self.conn.migrated, self.conn.migrated)]
        elif "pg_try_advisory_lock" in sql:
            self.conn.locked = True
            self.conn.lock_taken = True
            self._rows = [(True,)]
        elif "pg_advisory_unlock" in sql:
            self.conn.locked = False
            self._rows = []
        elif "SELECT DISTINCT odoo_version" in sql:
            self._rows = [{"odoo_version": "17.0"}]
        elif "SELECT name FROM module_presence" in sql:
            self.conn.read_under_lock = self.conn.read_under_lock or self.conn.locked
            self._rows = [{"name": n} for n in self.conn.flagged]
        elif "clock_timestamp() AS t" in sql:
            self._rows = [{"t": T0}]
        else:
            # A write: record whether it ran under the lock and in which
            # transaction (commits so far).
            self.conn.writes.append((" ".join(sql.split()), self.conn.locked,
                                     self.conn.commits, self.conn.autocommit))
            self._rows = []
            self.rowcount = 1

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _Conn:
    autocommit = True

    def __init__(self, *, migrated: bool, flagged=("mod_a",)) -> None:
        self.migrated = migrated
        self.flagged = list(flagged)
        self.log: list = []
        self.writes: list = []
        self.locked = False
        self.lock_taken = False
        self.read_under_lock = False
        self.commits = 0

    def cursor(self, cursor_factory=None):
        return _Cursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        return None

    def updates(self):
        return [(sql, p) for sql, p in self.log if sql.startswith("UPDATE module_presence")]


def _store():
    return ModulePresenceStore(None, lock_wait_seconds=1.0)  # type: ignore[arg-type]


@pytest.mark.parametrize("writer", ["mark_needs_rewrite", "mark_dependents_needs_rewrite"])
def test_every_flag_writer_stamps_the_flag_with_the_clock(writer):
    conn = _Conn(migrated=True)
    store = _store()
    if writer == "mark_needs_rewrite":
        store.mark_needs_rewrite(7, "mod_a", conn=conn)
    else:
        store.mark_dependents_needs_rewrite("17.0", ["mod_a"], conn=conn)

    (sql, _params), = conn.updates()
    assert "needs_rewrite = TRUE" in sql
    assert "needs_rewrite_at = clock_timestamp()" in sql


def test_the_runs_read_is_taken_under_the_ledger_lock_with_its_clock():
    conn = _Conn(migrated=True)

    names, read_at = _store().needs_rewrite_snapshot(7, ["17.0"], conn=conn)

    assert (names, read_at) == (["mod_a"], T0)
    assert conn.read_under_lock is True


def test_the_clear_spares_a_flag_set_after_the_runs_read():
    conn = _Conn(migrated=True)

    _store().clear_needs_rewrite(7, ["mod_a"], read_at=T0, conn=conn)

    (sql, params), = conn.updates()
    assert "needs_rewrite_at IS NULL OR needs_rewrite_at <= %s" in sql
    assert params[-1] == T0


def test_a_clear_without_a_read_is_by_name():
    conn = _Conn(migrated=True)

    _store().clear_needs_rewrite(7, ["mod_a"], conn=conn)

    (sql, params), = conn.updates()
    assert "needs_rewrite_at <=" not in sql and params == (7, ["mod_a"])


def test_a_cleared_flag_drops_its_stamp():
    conn = _Conn(migrated=True)

    _store().clear_needs_rewrite(7, ["mod_a"], read_at=T0, conn=conn)

    (sql, _params), = conn.updates()
    assert "needs_rewrite = FALSE" in sql and "needs_rewrite_at = NULL" in sql


def test_a_run_without_flags_reads_the_clock_without_waiting_for_the_lock():
    """Most runs have no flag: they must not wait (up to 900s, then fail) for
    a reconcile holding the ledger lock. The clear only touches names in the
    read set, and the pending application needs only the clock."""
    conn = _Conn(migrated=True, flagged=())

    names, read_at = _store().needs_rewrite_snapshot(7, ["17.0"], conn=conn)

    assert (names, read_at) == ([], T0)
    assert conn.lock_taken is False


def test_the_direct_flags_and_the_pending_entries_share_one_lock_and_transaction():
    """A dependent repo's commit_observed takes the same lock: between two
    transactions it could create the row step 1 could not flag and step 2
    then skips. One hold, one commit."""
    conn = _Conn(migrated=True)

    _store().flag_dependents(
        "17.0", ["mod_a"], pending=[("dep_repo", "new_mod")], exclude_repo_ids=[3], conn=conn,
    )

    flag = [w for w in conn.writes if w[0].startswith("UPDATE module_presence")]
    insert = [w for w in conn.writes if w[0].startswith("INSERT INTO module_presence_pending")]
    assert flag and insert
    (_s1, locked1, tx1, auto1), = flag
    (_s2, locked2, tx2, auto2), = insert
    assert locked1 and locked2, "both under the ledger lock"
    assert tx1 == tx2 and not auto1 and not auto2, "in one transaction"
    assert conn.commits == tx1 + 1, "committed once, after both"


def test_pending_entries_are_keyed_by_repo_and_found_by_the_ledgers_basename():
    """Each same-basename repo gets its own entry (none consumes another's),
    resolved from module_presence.repo_basename, not a regex over the path."""
    conn = _Conn(migrated=True)

    _store().record_pending_rewrites("17.0", [("dep_repo", "new_mod")], conn=conn)
    _store().apply_pending_rewrites(7, "17.0", ["new_mod"], written=set(), read_at=T0,
                                    conn=conn)

    insert = next(w[0] for w in conn.writes if w[0].startswith("INSERT"))
    apply = next(w[0] for w in conn.writes if w[0].startswith("WITH hit"))
    assert "owner.repo_basename = u.b" in insert and "regexp_replace" not in insert
    assert "(odoo_version, repo_id, name)" in insert
    assert "p.repo_id = %s" in apply
    assert "GREATEST(mp.needs_rewrite_at, hit.flagged_at)" in apply


def test_the_migration_probe_follows_the_search_path():
    conn = _Conn(migrated=True)

    _store().needs_rewrite_snapshot(7, ["17.0"], conn=conn)

    probe = next(sql for sql, _ in conn.log if "information_schema.columns" in sql)
    assert "table_schema = current_schema()" in probe
    assert "to_regclass('module_presence_pending_rewrite')" in probe


def test_before_the_migration_the_store_works_as_before(caplog):
    conn = _Conn(migrated=False)
    store = _store()

    store.mark_needs_rewrite(7, "mod_a", conn=conn)
    names, read_at = store.needs_rewrite_snapshot(7, ["17.0"], conn=conn)
    store.clear_needs_rewrite(7, names, read_at=read_at, conn=conn)
    recorded = store.record_pending_rewrites("17.0", [("dep_repo", "new_mod")], conn=conn)
    applied = store.apply_pending_rewrites(7, "17.0", ["mod_a"], written=set(),
                                           read_at=T0, conn=conn)

    assert read_at is None and conn.read_under_lock is False
    assert all("needs_rewrite_at" not in sql for sql, _ in conn.updates())
    assert recorded == 0 and applied == []
    assert not any(
        "pending_rewrite" in sql for sql, _ in conn.log if "information_schema" not in sql
    )
    assert "apply migration 0007" in caplog.text


def test_the_index_run_hands_its_read_clock_to_the_ledger_commit(tmp_path):
    """``_index_repo``: the clock of the run-start read reaches the clear and
    the pending-flag application of its ledger commit."""
    from tests.test_cross_repo_propagation_unit import _LifecycleRepo, _StatefulLedger

    repo = _LifecycleRepo(tmp_path)
    ledger = _StatefulLedger(repo.sha("a"))
    ledger.store.needs_rewrite_snapshot.return_value = (["mod_old"], T0)

    _store, _walk, _counters = repo.run(ledger, head_sha="a", walk={})

    ledger.store.needs_rewrite_snapshot.assert_called_once_with(7, ["17.0"])
    clear = ledger.store.clear_needs_rewrite.call_args
    assert clear.args == (7, ["mod_old"]) and clear.kwargs["read_at"] == T0
    apply = ledger.store.apply_pending_rewrites.call_args
    assert apply.kwargs["read_at"] == T0
    assert set(apply.args[2]) == {"mod_old", "mod_new"}
    assert set(apply.kwargs["written"]) == {"mod_old", "mod_new"}
