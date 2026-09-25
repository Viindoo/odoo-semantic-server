# SPDX-License-Identifier: AGPL-3.0-or-later
"""A failed repo or profile keeps the lifecycle outcome of the healthy ones and a
missing checkout never holds a version's cleanup silently (PR #379 review,
ADR-0056).

Business rules protected here:

- A repo that fails to index (the real case: registered repo 388 whose clone
  directory does not exist) never stops the other repos of its profile from
  being reconciled: a healthy repo of that profile whose removed module was
  retired ends the run synced (``presence_head_sha == head_sha``), in
  ``index_all`` sequential AND parallel (``profile_workers=2``).
- The gates tripped by a healthy repo of a failed profile stay in the run's
  lifecycle outcome and reach stderr of ``index-repo``.
- ``index-repo --all`` exits 1 whenever a repo or profile failed, whatever
  ``--profile-workers`` is (a failed run outranks exit 3, never reads as 0/3),
  still prints the "Lifecycle needs attention" lines.
- A registered repo with no checkout holds its profile's orphans and the
  module-less children of the version (fail-safe), but never silently: the
  unsynced repo carries attention, ``lifecycle-audit`` lists the held names in
  ``child_orphans_deferred`` and counts them in ``findings.child_orphans``
  (``--fail-on-findings`` exits 4). Once the repo is cloned and synced, the
  next run sweeps those children.

Every test drives the real ``index_profile`` / ``index_all`` / CLI ``main`` over
temp git repos with a bare origin, against real Neo4j and PostgreSQL + pgvector.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.db.migrate import _vector_extension_available, run_migrations
from src.db.pg import repo_store
from src.indexer.embedder import FakeEmbedder
from src.indexer.pipeline import index_all
from tests._lifecycle_repo import (
    GitRepo,
    V,
    assert_gone,
    assert_live,
    children,
    lc,
    ledger,
    register,
    repo_row,
    run,
    write_module,
)

pytestmark = [pytest.mark.postgres, pytest.mark.neo4j]




@pytest.fixture
def pg(clean_pg, clean_neo4j):
    """Migrated PG (with pgvector) + clean Neo4j; the pool comes from pg_conn."""
    run_migrations(clean_pg)
    if not _vector_extension_available(clean_pg):
        pytest.skip("pgvector extension not installed - embeddings cannot be checked")
    return clean_pg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _profile_id(name: str) -> int:
    [pid] = [p["id"] for p in repo_store().list_profiles() if p["name"] == name]
    return pid


def _register_missing_checkout(profile: str, parent: Path, name: str) -> int:
    """A registered repo whose clone directory does not exist (repo 388)."""
    store = repo_store()
    existing = {p["name"] for p in store.list_profiles()}
    pid = _profile_id(profile) if profile in existing else store.add_profile(profile, V)
    path = parent / name
    assert not path.exists()
    return store.add_repo(pid, f"file://{parent / f'{name}.origin.git'}", V, str(path))


def _index_all(pg_conn, workers: int) -> tuple[dict | None, BaseException | None]:
    """``index_all`` as the CLI sees it: the returned summary, or the raised error
    (whose ``summary`` carries the run outcome when the run failed)."""
    try:
        return index_all(pg_conn, embedder=FakeEmbedder(dim=1024), profile_workers=workers), None
    except Exception as exc:  # the failure itself is part of what is asserted
        return getattr(exc, "summary", None), exc


def _cli(monkeypatch, capsys, dsn: str, *argv: str) -> tuple[int, str, str]:
    """The process exit status of ``python -m src.indexer <argv>``: the returned
    code, or 1 for an uncaught exception (what the interpreter exits with)."""
    import src.indexer.__main__ as cli

    monkeypatch.setenv("PG_DSN", dsn)
    monkeypatch.setattr(cli, "_build_embedder", lambda: FakeEmbedder(dim=1024))
    capsys.readouterr()
    try:
        code = cli.main(list(argv))
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    except Exception:
        code = 1
    out = capsys.readouterr()
    return code, out.out, out.err


def _audit(monkeypatch, capsys, dsn: str, *argv: str) -> tuple[int, dict]:
    code, out, err = _cli(monkeypatch, capsys, dsn, "lifecycle-audit", *argv, "--json")
    assert out.strip(), err
    return code, json.loads(out)


def _version_entry(report: dict) -> dict:
    [entry] = [v for v in report["versions"] if v["odoo_version"] == V]
    return entry


def _attention(pg_conn, rid: int) -> str:
    return repo_row(pg_conn, rid).get("lifecycle_attention") or ""


def _synced(pg_conn, rid: int, repo: GitRepo) -> bool:
    return repo_row(pg_conn, rid)["presence_head_sha"] == repo.head()


def _add_module_less_child(driver, module: str) -> None:
    """A Model the index attributes to *module*, which has no Module node (the
    shape a vanished repo leaves behind)."""
    with driver.session() as s:
        s.run(
            "CREATE (:Model {name: $m, module: $mod, odoo_version: $v})",
            m=f"x_{module}.thing", mod=module, v=V,
        ).consume()


# ---------------------------------------------------------------------------
# 7cf09e66 - a failed repo/profile keeps the healthy repos' lifecycle outcome
# ---------------------------------------------------------------------------

def _failed_profile_night(tmp_path: Path, pg_conn) -> dict:
    """Profile ``fail_99``: a healthy repo drops one module, a second healthy repo
    drops every module (total-wipe gate), and a registered repo has no checkout.
    Profile ``ok_99``: a healthy repo drops one module. First night indexed
    before the missing repo was registered."""
    healthy = GitRepo(tmp_path, "healthy_addons")
    write_module(healthy, "healthy_keep")
    write_module(healthy, "healthy_drop")
    healthy.commit("add")
    wiped = GitRepo(tmp_path, "wiped_addons")
    write_module(wiped, "wiped_one")
    write_module(wiped, "wiped_two")
    wiped.commit("add")
    other = GitRepo(tmp_path, "other_addons")
    write_module(other, "other_keep")
    write_module(other, "other_drop")
    other.commit("add")
    rid_healthy, rid_wiped = register("fail_99", healthy, wiped)
    (rid_other,) = register("ok_99", other)
    first, exc = _index_all(pg_conn, 1)
    assert exc is None and first["profiles_failed"] == [], (exc, first)

    rid_missing = _register_missing_checkout("fail_99", tmp_path, "never_cloned")
    healthy.rm("healthy_drop")
    healthy.commit("[REM] healthy_drop")
    wiped.rm("wiped_one")
    wiped.rm("wiped_two")
    wiped.commit("[REM] every module")
    other.rm("other_drop")
    other.commit("[REM] other_drop")
    return {
        "healthy": healthy, "wiped": wiped, "other": other,
        "rid_healthy": rid_healthy, "rid_wiped": rid_wiped, "rid_other": rid_other,
        "rid_missing": rid_missing,
    }


@pytest.mark.parametrize("workers", [1, 2], ids=["sequential", "parallel"])
def test_failed_profile_still_syncs_its_healthy_repo_and_keeps_its_gate_in_the_outcome(
    pg, neo4j_driver, tmp_path, monkeypatch, _ephemeral_pg_db, workers,
):
    """Repo 388 case under ``--all``: the profile with the missing checkout fails,
    yet its healthy repo's removed module retires AND the repo ends synced; the
    total-wipe gate its other healthy repo tripped is in the run outcome; the
    healthy profile is reconciled as usual."""
    monkeypatch.setenv("PG_DSN", _ephemeral_pg_db)
    s = _failed_profile_night(tmp_path, pg)

    summary, exc = _index_all(pg, workers)

    assert summary is not None, f"the failed run lost its summary: {exc!r}"
    assert summary["profiles_failed"] == ["fail_99"], summary
    if workers > 1:
        assert isinstance(exc, RuntimeError), "a parallel run with a failed profile raises"
    assert repo_row(pg, s["rid_missing"])["status"] == "error"

    # The healthy repo of the failed profile: retired AND synced.
    assert_gone(neo4j_driver, pg, "healthy_drop")
    assert ledger(pg, s["rid_healthy"], "healthy_drop")["state"] == "retired"
    assert_live(neo4j_driver, pg, "healthy_keep")
    assert _synced(pg, s["rid_healthy"], s["healthy"]), (
        "the healthy repo of a failed profile stays unsynced: "
        f"{repo_row(pg, s['rid_healthy'])['presence_head_sha']!r} != {s['healthy'].head()!r}"
    )

    # The gate tripped by the other healthy repo of the failed profile.
    lifecycle = lc(summary)
    wiped_gates = [
        g for g in lifecycle.get("gates_tripped", [])
        if f"repo id={s['rid_wiped']} " in g
    ]
    assert wiped_gates and "total_wipe" in wiped_gates[0], lifecycle.get("gates_tripped")
    assert lifecycle.get("needs_attention") is True
    assert_live(neo4j_driver, pg, "wiped_one")

    # The healthy profile.
    assert_gone(neo4j_driver, pg, "other_drop")
    assert _synced(pg, s["rid_other"], s["other"])


@pytest.mark.parametrize("workers", ["1", "2"], ids=["sequential", "parallel"])
def test_index_repo_all_exits_1_on_a_failed_profile_and_still_prints_the_attention_lines(
    pg, neo4j_driver, tmp_path, monkeypatch, capsys, _ephemeral_pg_db, workers,
):
    """A failed profile is never reported as exit 0 or 3: ``index-repo --all``
    exits 1 (outranking the tripped gate's 3), prints the lifecycle attention
    lines with the gate - in both worker modes."""
    s = _failed_profile_night(tmp_path, pg)

    code, _, err = _cli(
        monkeypatch, capsys, _ephemeral_pg_db,
        "index-repo", "--all", "--profile-workers", workers,
    )

    assert code == 1, err
    assert "Lifecycle needs attention (exit 1):" in err, err
    gate_lines = [
        x for x in err.splitlines()
        if x.strip().startswith("gates_tripped:") and f"repo id={s['rid_wiped']} " in x
    ]
    assert gate_lines and "total_wipe" in gate_lines[0], err
    assert _synced(pg, s["rid_healthy"], s["healthy"])


def test_index_repo_profile_exits_1_on_a_failed_repo_and_prints_its_healthy_repos_gate(
    pg, neo4j_driver, tmp_path, monkeypatch, capsys, _ephemeral_pg_db,
):
    """``index-repo --profile``: the missing checkout fails the run (exit 1), the
    healthy repo's tripped gate is still printed, the healthy repo is synced."""
    s = _failed_profile_night(tmp_path, pg)

    code, _, err = _cli(
        monkeypatch, capsys, _ephemeral_pg_db, "index-repo", "--profile", "fail_99",
    )

    assert code == 1, err
    assert "Lifecycle needs attention (exit 1):" in err, err
    assert any(
        f"repo id={s['rid_wiped']} " in x and "total_wipe" in x for x in err.splitlines()
    ), err
    assert _synced(pg, s["rid_healthy"], s["healthy"])
    assert_gone(neo4j_driver, pg, "healthy_drop")


# ---------------------------------------------------------------------------
# e89ae75b - a missing checkout holds the version's module-less children, loudly
# ---------------------------------------------------------------------------

def test_missing_checkout_holds_module_less_children_with_attention_and_an_audit_finding(
    pg, neo4j_driver, tmp_path, monkeypatch, capsys, _ephemeral_pg_db,
):
    """A registered repo without a clone dir: the other repo's removed module
    still retires, the version's module-less children are kept (fail-safe), the
    unsynced repo names the hold, and lifecycle-audit reports the held name
    (``child_orphans_deferred``, a ``child_orphans`` finding, exit 4). After the
    repo is cloned the next run syncs it and sweeps the children."""
    monkeypatch.setenv("PG_DSN", _ephemeral_pg_db)
    healthy = GitRepo(tmp_path, "healthy_addons")
    write_module(healthy, "healthy_keep")
    write_module(healthy, "healthy_drop")
    healthy.commit("add")
    (rid_healthy,) = register("live_99", healthy)
    run(pg, "live_99")
    rid_missing = _register_missing_checkout("late_99", tmp_path, "late_addons")
    _add_module_less_child(neo4j_driver, "gone_mod")
    healthy.rm("healthy_drop")
    healthy.commit("[REM] healthy_drop")

    summary, _exc = _index_all(pg, 1)

    assert summary["profiles_failed"] == ["late_99"], summary
    assert_gone(neo4j_driver, pg, "healthy_drop")
    assert children(neo4j_driver, "gone_mod") == 1, "the hold keeps the module-less child"
    attention = _attention(pg, rid_missing)
    assert "module-less" in attention and "synced" in attention, (
        f"the unsynced repo does not name the version-wide hold: {attention!r}"
    )

    code, report = _audit(monkeypatch, capsys, _ephemeral_pg_db, "--all", "--fail-on-findings")
    ver = _version_entry(report)
    assert ver.get("child_orphans_deferred") == ["gone_mod"], ver
    assert report["findings"]["child_orphans"] == 1, report["findings"]
    assert code == 4

    # Clone the repo at its registered path; the next run syncs it and sweeps.
    late = GitRepo(tmp_path, "late_addons")
    write_module(late, "late_mod")
    late.commit("add")
    assert repo_row(pg, rid_missing)["url"] == late.url

    summary, exc = _index_all(pg, 1)

    assert exc is None and summary["profiles_failed"] == [], (exc, summary)
    assert _synced(pg, rid_missing, late)
    assert children(neo4j_driver, "gone_mod") == 0, "once every repo is synced the children go"
    assert "module-less" not in _attention(pg, rid_missing)
    _, report = _audit(monkeypatch, capsys, _ephemeral_pg_db, "--all")
    assert _version_entry(report).get("child_orphans_deferred") == []
    assert report["findings"]["child_orphans"] == 0
    assert _synced(pg, rid_healthy, healthy)
