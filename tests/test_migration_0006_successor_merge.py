# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 0006 + the successor backfill against a real ledger (#380 item 3).

- after run_migrations a retired row can carry successor_source 'git_merge';
  an unknown source is still rejected;
- re-running the body by hand and a yoyo rollback + re-apply are safe; the
  rollback clears only git_merge successors (the history row stays) and
  narrows the CHECK again;
- on the narrow schema the store drops a git_merge successor instead of
  failing the retirement (code deployed before the migration);
- ops/backfill_successors.py over a real git repo: dry run writes nothing,
  --apply records "merged into", a re-run records nothing, a recorded
  successor is never overwritten.
"""
from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import psycopg2
import pytest

from src.db.migrate import run_migrations
from src.db.module_presence import ModulePresenceStore, ObservedModule, RetireEvidence, Successor
from tests._lifecycle_repo import run_git

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


def _repo_row(conn, local_path: str) -> int:
    pid = _one(conn, "INSERT INTO profiles (name, odoo_version) VALUES ('mg_99', %s) "
                     "RETURNING id", (V,))[0]
    return _one(conn, "INSERT INTO repos (profile_id, url, branch, local_path) "
                      "VALUES (%s, 'file:///x/tvtmaaddons.git', %s, %s) RETURNING id",
                (pid, V, local_path))[0]


def _store():
    return ModulePresenceStore(None, lock_wait_seconds=5.0)  # type: ignore[arg-type]


def _observe(store, conn, repo_id, names, head="1" * 40):
    store.commit_observed(
        repo_id, profile_name="mg_99", odoo_version=V, head_sha=head,
        observed=[ObservedModule(name=n, path=n, manifest_file="__manifest__.py")
                  for n in names],
        conn=conn,
    )


def _retire(store, conn, repo_id, name, *, sha="2" * 40, successor=None):
    assert store.commit_retired(
        repo_id, name, reason="absent",
        evidence=RetireEvidence(sha=sha, date="2026-08-29T10:12:00+07:00",
                                subject=f"[MERGE] {name}: merge into viin_ai"),
        successor=successor, head_sha="3" * 40, conn=conn,
    )


def _row(conn, repo_id, name):
    return _one(conn, "SELECT successor_names, successor_source, state FROM module_presence "
                      "WHERE repo_id = %s AND name = %s", (repo_id, name))


def test_a_git_merge_successor_is_stored_and_an_unknown_source_rejected(migrated):
    store = _store()
    rid = _repo_row(migrated, "/srv/none")
    _observe(store, migrated, rid, ["viin_ai_rag", "viin_ai"])
    _retire(store, migrated, rid, "viin_ai_rag", successor=Successor(("viin_ai",), "git_merge"))
    assert _row(migrated, rid, "viin_ai_rag") == (["viin_ai"], "git_merge", "retired")
    with pytest.raises(psycopg2.errors.CheckViolation):
        with migrated.cursor() as cur:
            cur.execute("UPDATE module_presence SET successor_source = 'similar_name' "
                        "WHERE repo_id = %s AND name = 'viin_ai_rag'", (rid,))


def test_rerun_and_rollback_then_reapply(migrated):
    from yoyo import get_backend, read_migrations

    from src.db.migrate import _MIGRATIONS_DIR, _conn_to_uri

    store = _store()
    rid = _repo_row(migrated, "/srv/none")
    _observe(store, migrated, rid, ["viin_ai_rag", "test_pylint", "viin_ai", "new_mod"])
    _retire(store, migrated, rid, "viin_ai_rag", successor=Successor(("viin_ai",), "git_merge"))
    _retire(store, migrated, rid, "test_pylint", successor=Successor(("new_mod",), "git_rename"))
    body = (_MIGRATIONS_DIR / "0006_module_presence_successor_merge.sql").read_text()
    with migrated.cursor() as cur:
        cur.execute(body)  # idempotent by hand too
    backend = get_backend(_conn_to_uri(migrated))
    target = read_migrations(str(_MIGRATIONS_DIR)).filter(
        lambda m: m.id == "0006_module_presence_successor_merge")
    assert target, "0006 migration file not found"
    with backend.lock():
        backend.rollback_migrations(backend.to_rollback(target))
    assert _row(migrated, rid, "viin_ai_rag") == (None, None, "retired"), "history row kept"
    assert _row(migrated, rid, "test_pylint") == (["new_mod"], "git_rename", "retired")
    with pytest.raises(psycopg2.errors.CheckViolation):
        with migrated.cursor() as cur:
            cur.execute("UPDATE module_presence SET successor_names = '{viin_ai}', "
                        "successor_source = 'git_merge' WHERE repo_id = %s "
                        "AND name = 'viin_ai_rag'", (rid,))
    run_migrations(migrated)
    store.record_successor(
        _one(migrated, "SELECT id FROM module_presence WHERE name = 'viin_ai_rag'")[0],
        Successor(("viin_ai",), "git_merge"), conn=migrated,
    )
    assert _row(migrated, rid, "viin_ai_rag") == (["viin_ai"], "git_merge", "retired")


def test_before_the_migration_a_merge_successor_never_fails_the_retirement(migrated):
    from yoyo import get_backend, read_migrations

    from src.db.migrate import _MIGRATIONS_DIR, _conn_to_uri

    backend = get_backend(_conn_to_uri(migrated))
    target = read_migrations(str(_MIGRATIONS_DIR)).filter(
        lambda m: m.id == "0006_module_presence_successor_merge")
    with backend.lock():
        backend.rollback_migrations(backend.to_rollback(target))
    store = _store()
    rid = _repo_row(migrated, "/srv/none")
    _observe(store, migrated, rid, ["viin_ai_rag", "viin_ai"])
    _retire(store, migrated, rid, "viin_ai_rag", successor=Successor(("viin_ai",), "git_merge"))
    assert _row(migrated, rid, "viin_ai_rag") == (None, None, "retired")


# ---------------------------------------------------------------------------
# ops/backfill_successors.py over a real git repo
# ---------------------------------------------------------------------------

def _backfill_module():
    path = Path(__file__).resolve().parent.parent / "ops" / "backfill_successors.py"
    spec = importlib.util.spec_from_file_location("backfill_successors", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git_merge_repo(path: Path) -> str:
    """viin_ai (pre-existing) absorbs 6 of viin_ai_rag's 10 files; returns the merge sha."""
    path.mkdir(parents=True)
    run_git(path, "init", "-q")
    run_git(path, "config", "user.email", "d@e.x")
    run_git(path, "config", "user.name", "D")
    for name in ("viin_ai", "viin_ai_rag"):
        (path / name / "models").mkdir(parents=True)
        (path / name / "__manifest__.py").write_text(repr({"name": name}) + "\n")
        for i in range(9):
            (path / name / "models" / f"{name}_{i}.py").write_text(
                "".join(f"def {name}_{i}_{j}():\n    return {i * 7 + j}\n" for j in range(6)))
    run_git(path, "add", "-A")
    run_git(path, "commit", "-q", "-m", "init")
    for i in range(6):
        run_git(path, "mv", f"viin_ai_rag/models/viin_ai_rag_{i}.py",
                f"viin_ai/models/viin_ai_rag_{i}.py")
    run_git(path, "rm", "-r", "-q", "viin_ai_rag")
    run_git(path, "commit", "-q", "-m", "[MERGE] viin_ai_rag: merge into viin_ai")
    return run_git(path, "rev-parse", "HEAD")


def test_backfill_dry_run_then_apply_then_rerun(migrated, tmp_path):
    repo = tmp_path / "tvtmaaddons"
    sha = _git_merge_repo(repo)
    store = _store()
    rid = _repo_row(migrated, str(repo))
    _observe(store, migrated, rid, ["viin_ai_rag", "viin_ai"])
    _retire(store, migrated, rid, "viin_ai_rag", sha=sha)  # retired before the oracle: no successor
    mod = _backfill_module()

    out = io.StringIO()
    counts = mod.backfill(store, migrated, apply=False, out=out)
    assert counts["found"] == 1 and counts["recorded"] == 0
    assert "would record viin_ai_rag@99.0" in out.getvalue()
    assert _row(migrated, rid, "viin_ai_rag")[0] is None, "dry run writes nothing"

    counts = mod.backfill(store, migrated, apply=True, out=io.StringIO())
    assert counts["recorded"] == 1
    assert _row(migrated, rid, "viin_ai_rag") == (["viin_ai"], "git_merge", "retired")

    counts = mod.backfill(store, migrated, apply=True, out=io.StringIO())
    assert counts["candidates"] == 0 and counts["recorded"] == 0, "idempotent"


def test_backfill_skips_a_row_whose_recorded_commit_is_not_the_deleting_one(migrated, tmp_path):
    repo = tmp_path / "tvtmaaddons"
    _git_merge_repo(repo)
    store = _store()
    rid = _repo_row(migrated, str(repo))
    _observe(store, migrated, rid, ["viin_ai_rag", "viin_ai"])
    _retire(store, migrated, rid, "viin_ai_rag", sha="f" * 40)
    counts = _backfill_module().backfill(store, migrated, apply=True, out=io.StringIO())
    assert counts["commit_mismatch"] == 1 and counts["recorded"] == 0
    assert _row(migrated, rid, "viin_ai_rag")[0] is None


def test_rows_git_has_no_evidence_for_never_hide_the_rows_after_them(migrated, tmp_path):
    """--limit pages by ledger id with a cursor: rows without git evidence keep
    no successor, so without a cursor every re-run would read the same first
    page and never reach the rows that git can prove."""
    repo = tmp_path / "tvtmaaddons"
    sha = _git_merge_repo(repo)
    store = _store()
    rid = _repo_row(migrated, str(repo))
    ghosts = [f"aaa_gone_{i}" for i in range(3)]  # sort and insert before viin_ai_rag
    _observe(store, migrated, rid, [*ghosts, "viin_ai_rag", "viin_ai"])
    for name in ghosts:
        _retire(store, migrated, rid, name, sha=sha)  # no manifest in git: no evidence
    _retire(store, migrated, rid, "viin_ai_rag", sha=sha)
    mod = _backfill_module()

    first = mod.backfill(store, migrated, apply=True, limit=3, out=io.StringIO())
    assert first["candidates"] == 3 and first["no_evidence"] == 3 and first["recorded"] == 0
    assert first["next_after_id"] > 0, "a full page must hand out the next cursor"
    again = mod.backfill(store, migrated, apply=True, limit=3, out=io.StringIO())
    assert again["recorded"] == 0, "without the cursor the same page is read again"

    out = io.StringIO()
    nxt = mod.backfill(store, migrated, apply=True, limit=3,
                       after_id=first["next_after_id"], out=out)
    assert nxt["recorded"] == 1, out.getvalue()
    assert _row(migrated, rid, "viin_ai_rag") == (["viin_ai"], "git_merge", "retired")
    assert nxt["next_after_id"] == 0, "a partial page is the last one"


def test_record_successor_never_overwrites(migrated):
    store = _store()
    rid = _repo_row(migrated, "/srv/none")
    _observe(store, migrated, rid, ["test_pylint", "new_mod", "viin_ai"])
    _retire(store, migrated, rid, "test_pylint", successor=Successor(("new_mod",), "git_rename"))
    row_id = _one(migrated, "SELECT id FROM module_presence WHERE name = 'test_pylint'")[0]
    assert not store.record_successor(row_id, Successor(("viin_ai",), "git_merge"), conn=migrated)
    assert _row(migrated, rid, "test_pylint") == (["new_mod"], "git_rename", "retired")
