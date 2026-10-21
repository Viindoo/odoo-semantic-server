# SPDX-License-Identifier: AGPL-3.0-or-later
"""A failed repo or profile keeps the lifecycle outcome of the healthy ones, a
missing checkout is cloned or reported, never a silent hold, and the orphan
embedding sweep is judged by git (PR #379 review, ADR-0056, ADR-0057 phase 2,
issue #383 item 2).

Business rules protected here:

- A repo that fails to index (its indexing raises) never stops the other
  repos of its profile from being reconciled: a healthy repo of that profile
  whose removed module was retired ends the run synced (``presence_head_sha
  == head_sha``), in ``index_all`` sequential AND parallel
  (``profile_workers=2``).
- The gates tripped by a healthy repo of a failed profile stay in the run's
  lifecycle outcome and reach stderr of ``index-repo``.
- ``index-repo --all`` exits 1 whenever a repo or profile failed, whatever
  ``--profile-workers`` is (a failed run outranks exit 3, never reads as 0/3),
  still prints the "Lifecycle needs attention" lines and marks the job error
  (``test_index_repo_marks_the_job_of_a_failed_run_as_error``).
- A registered repo whose clone directory does not exist (repo 388) is cloned
  by the run; when that fails it is reported (``unavailable_repos``, status
  error, attention, exit 3, no traceback) and is no failure of its profile;
  once the branch exists the next run clones, indexes and syncs it.
- A registered repo with no checkout holds its profile's orphans and the
  module-less children of the version (fail-safe), but never silently: the
  unsynced repo carries attention, ``lifecycle-audit`` lists the held names in
  ``child_orphans_deferred`` and counts them in ``findings.child_orphans``
  (``--fail-on-findings`` exits 4). Once the repo is cloned and synced, the
  next run sweeps those children.
- The orphan embedding sweep deletes the (module, profile) groups no checkout
  of the profile ships, whatever their share of the version (no ratio, no
  approval) and ``lifecycle-audit`` predicts it; a group whose module a
  checkout still tracks is kept; a graph that lost the Module node of a
  present module holds every group (``embedding_sweep:graph_unhealthy``, exit
  3) until the module is re-written; the pattern catalogue (``__global__``)
  never counts.

Every test drives the real ``index_profile`` / ``index_all`` / CLI ``main`` over
temp git repos with a bare origin, against real Neo4j and PostgreSQL + pgvector.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.constants import GLOBAL_PROFILE
from src.db.migrate import _vector_extension_available, run_migrations
from src.db.pg import job_store, repo_store
from src.indexer.embedder import FakeEmbedder
from src.indexer.pipeline import index_all
from tests._lifecycle_repo import (
    GitRepo,
    V,
    assert_gone,
    assert_live,
    children,
    embeddings,
    lc,
    ledger,
    register,
    repo_row,
    run,
    run_git,
    write_module,
)

pytestmark = [pytest.mark.postgres, pytest.mark.neo4j]




@pytest.fixture
def pg(clean_pg, clean_neo4j, tmp_path, monkeypatch):
    """Migrated PG (with pgvector) + clean Neo4j; the pool comes from pg_conn.
    HOME is a temp dir: the run's clone retry (#383 item 2) clones into
    ``~/.local/share/odoo-semantic-mcp/clones``."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
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
    """A registered repo whose clone directory does not exist (repo 388: its
    Web UI clone failed, ``clone_status='error'``, so the run retries it)."""
    store = repo_store()
    existing = {p["name"] for p in store.list_profiles()}
    pid = _profile_id(profile) if profile in existing else store.add_profile(profile, V)
    path = parent / name
    assert not path.exists()
    return store.add_repo(
        pid, f"file://{parent / f'{name}.origin.git'}", V, str(path), clone_status="error",
    )


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


def _add_embedding_groups(pg_conn, modules: list[str], profile: str) -> None:
    """One embedding row per (module, profile) group that no Module and no
    present ledger row accounts for (an orphan group)."""
    zero = "[" + ",".join(["0.0"] * 1024) + "]"
    with pg_conn.cursor() as cur:
        for m in modules:
            cur.execute(
                "INSERT INTO embeddings (chunk_type, module, odoo_version, entity_name, "
                "model_name, file_path, chunk_idx, content, vec, profile_name) VALUES "
                "('method', %s, %s, %s, NULL, '/x.py', 0, 'x', %s::vector, %s)",
                (m, V, f"{m}.x", zero, profile),
            )


def _rows(pg_conn, modules: list[str], profile: str) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM embeddings WHERE odoo_version = %s "
            "AND module = ANY(%s) AND profile_name = %s",
            (V, modules, profile),
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# 7cf09e66 - a failed repo/profile keeps the healthy repos' lifecycle outcome
# ---------------------------------------------------------------------------

def _failed_profile_night(tmp_path: Path, pg_conn, monkeypatch, failure: str) -> dict:
    """Profile ``fail_99``: a healthy repo drops one module, a second healthy repo
    has its branch replaced by an unrelated root (a total wipe git does not
    prove: gate ``total_wipe``), and a third repo fails. *failure*: ``crash``
    (its indexing raises: the profile fails, exit 1) or ``no_checkout``
    (registered without a clone directory and its clone fails - repo 388,
    issue #383 item 2: reported, the profile does not fail, exit 3).
    Profile ``ok_99``: a healthy repo drops one module. First night indexed
    before the failing repo was registered."""
    from src.indexer import pipeline as pipeline_mod

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

    if failure == "crash":
        crash = GitRepo(tmp_path, "crash_addons")
        write_module(crash, "crash_mod")
        crash.commit("add")
        (rid_failing,) = register("fail_99", crash)
        real = pipeline_mod._index_repo

        def crashing(repo, *args, **kwargs):
            if repo["id"] == rid_failing:
                raise RuntimeError("parser crashed")
            return real(repo, *args, **kwargs)

        monkeypatch.setattr(pipeline_mod, "_index_repo", crashing)
    else:
        rid_failing = _register_missing_checkout("fail_99", tmp_path, "never_cloned")
    healthy.rm("healthy_drop")
    healthy.commit("[REM] healthy_drop")
    wiped.force_push_unrelated_root()
    other.rm("other_drop")
    other.commit("[REM] other_drop")
    return {
        "healthy": healthy, "wiped": wiped, "other": other,
        "rid_healthy": rid_healthy, "rid_wiped": rid_wiped, "rid_other": rid_other,
        "rid_failing": rid_failing,
    }


def _assert_healthy_repos_reconciled(pg, neo4j_driver, s: dict, summary: dict) -> None:
    # The healthy repo of the failing profile: retired AND synced.
    assert_gone(neo4j_driver, pg, "healthy_drop")
    assert ledger(pg, s["rid_healthy"], "healthy_drop")["state"] == "retired"
    assert_live(neo4j_driver, pg, "healthy_keep")
    assert _synced(pg, s["rid_healthy"], s["healthy"]), (
        "the healthy repo of a failing profile stays unsynced: "
        f"{repo_row(pg, s['rid_healthy'])['presence_head_sha']!r} != {s['healthy'].head()!r}"
    )
    # The gate tripped by the other healthy repo of the failing profile.
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


@pytest.mark.parametrize("workers", [1, 2], ids=["sequential", "parallel"])
def test_failed_profile_still_syncs_its_healthy_repo_and_keeps_its_gate_in_the_outcome(
    pg, neo4j_driver, tmp_path, monkeypatch, _ephemeral_pg_db, workers,
):
    """A repo whose indexing crashes fails its profile under ``--all``, yet the
    profile's healthy repo retires its removed module AND ends synced; the
    total-wipe gate its other healthy repo tripped is in the run outcome; the
    healthy profile is reconciled as usual."""
    monkeypatch.setenv("PG_DSN", _ephemeral_pg_db)
    s = _failed_profile_night(tmp_path, pg, monkeypatch, "crash")

    summary, exc = _index_all(pg, workers)

    assert summary is not None, f"the failed run lost its summary: {exc!r}"
    assert summary["profiles_failed"] == ["fail_99"], summary
    if workers > 1:
        assert isinstance(exc, RuntimeError), "a parallel run with a failed profile raises"
    assert repo_row(pg, s["rid_failing"])["status"] == "error"
    _assert_healthy_repos_reconciled(pg, neo4j_driver, s, summary)


@pytest.mark.parametrize("workers", [1, 2], ids=["sequential", "parallel"])
def test_a_repo_without_a_checkout_is_reported_and_its_profile_still_reconciles(
    pg, neo4j_driver, tmp_path, monkeypatch, _ephemeral_pg_db, workers,
):
    """Repo 388 (#383 item 2): its clone directory does not exist and the
    run's clone retry fails. It is no failure of its profile any more: it is
    listed in ``unavailable_repos`` (status error, attention), the run needs
    attention (exit 3), and every other repo is indexed and reconciled."""
    monkeypatch.setenv("PG_DSN", _ephemeral_pg_db)
    s = _failed_profile_night(tmp_path, pg, monkeypatch, "no_checkout")

    summary, exc = _index_all(pg, workers)

    assert exc is None and summary["profiles_failed"] == [], (exc, summary)
    unavailable = lc(summary).get("unavailable_repos") or []
    assert [u for u in unavailable if u.startswith(f"repo id={s['rid_failing']} ")], unavailable
    row = repo_row(pg, s["rid_failing"])
    assert row["status"] == "error" and row["clone_status"] == "error", row
    assert "checkout unavailable" in (row["lifecycle_attention"] or ""), row
    _assert_healthy_repos_reconciled(pg, neo4j_driver, s, summary)


@pytest.mark.parametrize("workers", ["1", "2"], ids=["sequential", "parallel"])
def test_index_repo_all_exits_1_on_a_failed_profile_and_still_prints_the_attention_lines(
    pg, neo4j_driver, tmp_path, monkeypatch, capsys, _ephemeral_pg_db, workers,
):
    """A failed profile is never reported as exit 0 or 3: ``index-repo --all``
    exits 1 (outranking the tripped gate's 3), prints the lifecycle attention
    lines with the gate - in both worker modes."""
    s = _failed_profile_night(tmp_path, pg, monkeypatch, "crash")

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


def test_index_repo_exits_3_without_a_traceback_for_a_repo_it_cannot_clone(
    pg, neo4j_driver, tmp_path, monkeypatch, capsys, caplog, _ephemeral_pg_db,
):
    """The 2026-08-07..09-25 nights: exit 1 with a traceback every night. Now
    exit 3 (OnFailure= still mails the admins) and one line naming the repo."""
    s = _failed_profile_night(tmp_path, pg, monkeypatch, "no_checkout")
    caplog.set_level("INFO")

    code, _, err = _cli(monkeypatch, capsys, _ephemeral_pg_db, "index-repo", "--all")

    assert code == 3, err
    assert any(
        x.strip().startswith(f"unavailable_repos: repo id={s['rid_failing']} ")
        for x in err.splitlines()
    ), err
    assert "Traceback" not in err
    mine = [r for r in caplog.records if f"id={s['rid_failing']} " in r.getMessage()]
    assert mine and all(r.exc_info is None for r in mine), "no traceback for the repo"
    assert _synced(pg, s["rid_healthy"], s["healthy"])


def test_index_repo_marks_the_job_of_a_failed_run_as_error(
    pg, neo4j_driver, tmp_path, monkeypatch, capsys, _ephemeral_pg_db,
):
    """The Web UI polls indexer_jobs: a run with a failed profile must leave its
    job in ``error``, never ``queued``/``running``/``done``."""
    _failed_profile_night(tmp_path, pg, monkeypatch, "crash")
    job_id = job_store().create_job("all")

    code, _, err = _cli(
        monkeypatch, capsys, _ephemeral_pg_db, "index-repo", "--all", "--job-id", str(job_id),
    )

    assert code == 1, err
    assert job_store().get_job(job_id)["status"] == "error"


def test_index_repo_profile_exits_1_on_a_failed_repo_and_prints_its_healthy_repos_gate(
    pg, neo4j_driver, tmp_path, monkeypatch, capsys, _ephemeral_pg_db,
):
    """``index-repo --profile``: the crashing repo fails the run (exit 1), the
    healthy repo's tripped gate is still printed, the healthy repo is synced."""
    s = _failed_profile_night(tmp_path, pg, monkeypatch, "crash")

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


def test_a_failed_first_clone_is_healed_by_the_next_run(
    pg, neo4j_driver, tmp_path, monkeypatch, _ephemeral_pg_db,
):
    """#383 item 2 end to end: the repo is registered before its branch exists
    upstream (the clone fails, the run reports it); once the branch is pushed
    the next plain run clones it under its registered clone directory, indexes
    it and syncs it - nobody re-clones by hand."""
    monkeypatch.setenv("PG_DSN", _ephemeral_pg_db)
    upstream = GitRepo(tmp_path / "upstream", "themes", branch="main")
    write_module(upstream, "theme_x")
    upstream.commit("add theme_x")
    store = repo_store()
    pid = store.add_profile("themes_99", V)
    rid = store.add_repo(pid, upstream.url, V, str(tmp_path / "not_cloned_yet"))
    store.set_clone_status(rid, "error", error_msg="Remote branch 99.0 not found")

    first = run(pg, "themes_99")
    assert [u for u in lc(first).get("unavailable_repos") or [] if f"repo id={rid} " in u]
    assert repo_row(pg, rid)["clone_status"] == "error"

    run_git(upstream.path, "push", "-q", "origin", f"HEAD:refs/heads/{V}")
    second = run(pg, "themes_99")

    assert not lc(second).get("unavailable_repos"), lc(second)
    row = repo_row(pg, rid)
    assert row["clone_status"] == "cloned" and Path(row["local_path"], ".git").exists(), row
    assert_live(neo4j_driver, pg, "theme_x")
    assert row["presence_head_sha"] == row["head_sha"], "the cloned repo is synced"


# ---------------------------------------------------------------------------
# e89ae75b - a missing checkout holds the version's module-less children, loudly
# ---------------------------------------------------------------------------

def test_missing_checkout_holds_module_less_children_with_attention_and_an_audit_finding(
    pg, neo4j_driver, tmp_path, monkeypatch, capsys, _ephemeral_pg_db,
):
    """A registered repo without a clone dir (its clone retry fails): the other
    repo's removed module still retires, the version's module-less children are
    kept (fail-safe), the unsynced repo names the hold, and lifecycle-audit
    reports the held name (``child_orphans_deferred``, a ``child_orphans``
    finding, exit 4). After the repo is cloned the next run syncs it and sweeps
    the children."""
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

    assert summary["profiles_failed"] == [], summary
    assert lc(summary).get("unavailable_repos"), lc(summary)
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


# ---------------------------------------------------------------------------
# 167b7360 / ADR-0057 phase 2 - the orphan embedding sweep is judged by git
# ---------------------------------------------------------------------------

def _embedding_profile(tmp_path: Path, pg_conn, n_live: int = 1) -> tuple[GitRepo, int]:
    """Profile ``emb_99`` with *n_live* indexed modules, then a new upstream
    commit, so the next run scans the repo (the orphan sweep, embeddings
    included, runs only for a version a repo was scanned at)."""
    repo = GitRepo(tmp_path, "emb_addons")
    for i in range(n_live):
        write_module(repo, f"emb_live_{i:02d}")
    repo.commit("add")
    (rid,) = register("emb_99", repo)
    run(pg_conn, "emb_99")
    assert embeddings(pg_conn, "emb_live_00", "emb_99") > 0, "precondition: live rows"
    record = repo.path / "emb_live_00" / "models" / "record.py"
    record.write_text(record.read_text() + "\n# touched\n")
    repo.commit("[IMP] emb_live_00: touch")
    return repo, rid


def _add_catalogue_rows(pg_conn, n: int) -> None:
    """Pattern-catalogue rows: the only shape a ``__global__`` row may take."""
    zero = "[" + ",".join(["0.0"] * 1024) + "]"
    with pg_conn.cursor() as cur:
        for i in range(n):
            cur.execute(
                "INSERT INTO embeddings (chunk_type, module, odoo_version, entity_name, "
                "model_name, file_path, chunk_idx, content, vec, profile_name) VALUES "
                "('pattern_example', '__patterns__', %s, %s, NULL, '/p.json', 0, 'p', "
                "%s::vector, %s)",
                (V, f"lifecycle_test_pattern_{i}", zero, GLOBAL_PROFILE),
            )


def _catalogue_rows(pg_conn) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM embeddings WHERE odoo_version = %s AND profile_name = %s",
            (V, GLOBAL_PROFILE),
        )
        return cur.fetchone()[0]


def test_a_mass_of_orphan_embedding_groups_git_proves_gone_is_deleted_without_a_flag(
    pg, neo4j_driver, tmp_path, monkeypatch, capsys, _ephemeral_pg_db,
):
    """1 live module + 20 orphan embedding groups (20/21: the old G-B ratio
    held this until --allow-mass-retire). No checkout of the profile ships any
    ``ghost_*`` module and the graph is complete, so git proves them gone:
    the audit predicts the delete, the plain run deletes all 20 groups and
    exits 0. The pattern catalogue (``__global__``) is never listed, held or
    deleted."""
    _repo, rid = _embedding_profile(tmp_path, pg)
    ghosts = [f"ghost_{i:02d}" for i in range(20)]
    _add_embedding_groups(pg, ghosts, "emb_99")
    _add_catalogue_rows(pg, 3)
    catalogue = _catalogue_rows(pg)

    code, report = _audit(monkeypatch, capsys, _ephemeral_pg_db, "--profile", "emb_99")
    ver = _version_entry(report)
    assert ver["gates_tripped"] == [], ver["gates_tripped"]
    assert sorted(g["module"] for g in ver["embedding_orphans"]) == ghosts
    assert ver.get("embedding_orphans_held") == []
    assert report["findings"]["embedding_orphans"] == 20, report["findings"]

    code, _, err = _cli(monkeypatch, capsys, _ephemeral_pg_db, "index-repo", "--profile", "emb_99")

    assert code == 0, err
    assert _rows(pg, ghosts, "emb_99") == 0, "git-proven orphan rows are deleted, no approval"
    assert "embedding sweep" not in _attention(pg, rid), _attention(pg, rid)
    assert _catalogue_rows(pg) == catalogue, "the catalogue is never an orphan"
    assert embeddings(pg, "emb_live_00", "emb_99") > 0


def test_orphan_embedding_groups_a_checkout_still_ships_are_kept(pg, neo4j_driver, tmp_path):
    """The profile's checkout still tracks ``ghost_00`` (here: tracked but
    not a module the scan accepts - a legacy ``__openerp__.py`` on a modern
    branch): git cannot prove it gone, so its rows stay; the others go."""
    repo, _rid = _embedding_profile(tmp_path, pg)
    (repo.path / "ghost_00").mkdir()
    (repo.path / "ghost_00" / "__openerp__.py").write_text("{'name': 'ghost_00'}\n")
    repo.commit("[ADD] legacy ghost_00 manifest")
    ghosts = [f"ghost_{i:02d}" for i in range(3)]
    _add_embedding_groups(pg, ghosts, "emb_99")

    summary = run(pg, "emb_99")

    assert not lc(summary).get("needs_attention"), lc(summary)
    assert _rows(pg, ["ghost_00"], "emb_99") == 1, "unproven rows are kept"
    assert _rows(pg, ghosts[1:], "emb_99") == 0


def test_a_graph_that_lost_a_present_module_holds_the_embedding_sweep_until_it_is_rewritten(
    pg, neo4j_driver, tmp_path,
):
    """Repo B's module lost its Module node while B is unchanged (its run is
    the unchanged skip, which does not re-write it). The graph is incomplete,
    so no orphan can be told from a live module: every orphan group is held,
    the run exits 3 (``embedding_sweep:graph_unhealthy``) and names the
    profile whose ``--full`` run re-writes the node. After that run the sweep
    proceeds by git."""
    repo_a, rid_a = _embedding_profile(tmp_path, pg)
    repo_b = GitRepo(tmp_path, "emb_b_addons")
    write_module(repo_b, "emb_b_mod")
    repo_b.commit("add")
    register("emb_99", repo_b)
    run(pg, "emb_99")
    with neo4j_driver.session() as s:
        s.run(
            "MATCH (m:Module {name: 'emb_b_mod', odoo_version: $v}) DETACH DELETE m", v=V,
        ).consume()
    record = repo_a.path / "emb_live_00" / "models" / "record.py"
    record.write_text(record.read_text() + "\n# touched again\n")
    repo_a.commit("[IMP] emb_live_00: touch again")
    ghosts = [f"ghost_{i:02d}" for i in range(3)]
    _add_embedding_groups(pg, ghosts, "emb_99")

    held = run(pg, "emb_99")

    assert "embedding_sweep:graph_unhealthy" in " ".join(lc(held).get("gates_tripped", []))
    assert _rows(pg, ghosts, "emb_99") == 3, "nothing is deleted while the graph is incomplete"
    attention = _attention(pg, rid_a)
    assert "emb_b_mod" in attention and "index-repo --profile emb_99 --full" in attention, (
        attention
    )

    rewritten = run(pg, "emb_99", full_reindex=True)

    assert not lc(rewritten).get("needs_attention"), lc(rewritten)
    assert _rows(pg, ghosts, "emb_99") == 0
    assert embeddings(pg, "emb_b_mod", "emb_99") > 0


def test_orphan_embedding_groups_below_the_floor_are_deleted_on_a_normal_run(
    pg, neo4j_driver, tmp_path,
):
    """19 orphan groups next to 1 live module (95% but under the floor of 20):
    deleted by a normal run, no gate, no attention."""
    # GUARD: pre-existing behaviour (small orphan sets were always deleted).
    _repo, rid = _embedding_profile(tmp_path, pg)
    ghosts = [f"ghost_{i:02d}" for i in range(19)]
    _add_embedding_groups(pg, ghosts, "emb_99")

    summary = run(pg, "emb_99")

    assert not lc(summary).get("needs_attention"), lc(summary)
    assert _rows(pg, ghosts, "emb_99") == 0
    assert "embedding sweep" not in _attention(pg, rid)
    assert embeddings(pg, "emb_live_00", "emb_99") > 0


def test_orphan_embedding_groups_below_half_the_version_are_deleted_on_a_normal_run(
    pg, neo4j_driver, tmp_path,
):
    """20 orphan groups (at the floor) next to 21 live modules (20/41 < 50%):
    deleted by a normal run, no gate."""
    # GUARD: pre-existing behaviour (the ratio rule only adds a hold above 50%).
    _repo, _rid = _embedding_profile(tmp_path, pg, n_live=21)
    ghosts = [f"ghost_{i:02d}" for i in range(20)]
    _add_embedding_groups(pg, ghosts, "emb_99")

    summary = run(pg, "emb_99")

    assert not lc(summary).get("needs_attention"), lc(summary)
    assert _rows(pg, ghosts, "emb_99") == 0
