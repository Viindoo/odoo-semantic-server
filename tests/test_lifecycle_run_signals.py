# SPDX-License-Identifier: AGPL-3.0-or-later
"""What an index run and the lifecycle-audit tell the operator, and the drift a
``--full`` run heals (lane-idx: F38, F39, F44, held-prune finding, full-mode heal).

Business rules protected here (lane-idx contract 3.2-3.6):

- F38: ``repos.lifecycle_attention`` of a repo skipped at an unchanged HEAD
  says what stands NOW; a past run's signal (a gate bypassed by
  ``--allow-mass-retire``) does not linger once nothing holds.
- F39: a profile with no repo registered is reported apart
  (``profiles_empty``), neither ok nor failed; the CLI names it; exit 0.
- Full-mode heal: a ``--full`` run heals a module indexed at a drifted path
  and prunes what was indexed under the OLD directory, lint violations
  included (before, only a sync / incremental run did).
- F44: the audit's ``would_rewrite`` is exactly the set of self-heal
  re-writes the next real run performs, on sync, incremental AND full runs;
  a module added in the commit range is never listed.
- A soft-gate-held entity prune is an audit finding (``held_prunes``) under the
  ``osm.lifecycle-audit/3`` schema (``/2`` introduced ``held_prunes``; F49's
  ``/3`` added the shared-module ``shared_prunes`` finding), so
  ``--fail-on-findings`` exits 4 until an operator confirms with
  ``--allow-mass-retire``.

Every test drives the real ``index_profile`` / ``index_all`` / CLI ``main`` over
temp git repos with a bare origin, against real Neo4j and PostgreSQL + pgvector.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from src.db.migrate import _vector_extension_available, run_migrations
from src.db.pg import repo_store
from src.indexer.embedder import FakeEmbedder
from tests import _retirement_fixture as rf
from tests._lifecycle_repo import (
    GitRepo,
    V,
    children,
    module_node,
    register,
    repo_row,
    run,
    run_git,
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

def _cli(monkeypatch, capsys, _ephemeral_pg_db, *argv: str) -> tuple[int, str, str]:
    import src.indexer.__main__ as cli

    monkeypatch.setenv("PG_DSN", _ephemeral_pg_db)
    monkeypatch.setattr(cli, "_build_embedder", lambda: FakeEmbedder(dim=1024))
    capsys.readouterr()
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def _audit(monkeypatch, capsys, _ephemeral_pg_db, *argv: str) -> tuple[int, dict]:
    code, out, _ = _cli(monkeypatch, capsys, _ephemeral_pg_db, "lifecycle-audit", *argv, "--json")
    return code, json.loads(out)


def _repo_entry(report: dict, basename: str) -> dict:
    [entry] = [r for r in report["repos"] if r["basename"] == basename]
    return entry


def _attention(pg_conn, rid: int) -> str | None:
    return repo_row(pg_conn, rid).get("lifecycle_attention")


def _set_node(driver, name: str, **props) -> None:
    with driver.session() as s:
        s.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) SET m += $props",
            n=name, v=V, props=props,
        ).consume()


def _path_prefix(driver, name: str) -> str:
    path = module_node(driver, name)["path"]
    assert path.endswith(name), path
    return path[: -len(name)]


# ---------------------------------------------------------------------------
# F38 - attention of a skipped repo is what stands now
# ---------------------------------------------------------------------------

def test_skipped_repo_attention_drops_the_signal_of_a_confirmed_mass_retire(
    pg, neo4j_driver, tmp_path,
):
    """A commit removes every module: the run trips the total-wipe gate and
    tells the operator. The operator confirms with --allow-mass-retire, the
    modules are retired. The next night nothing changed: the run is the
    zero-cost skip, nothing holds any more, so the repo carries no attention -
    neither the tripped gate nor the confirmed-retire signal."""
    repo = GitRepo(tmp_path, "wiped_addons")
    write_module(repo, "wipe_a")
    write_module(repo, "wipe_b")
    repo.commit("add")
    (rid,) = register("wipe_99", repo)
    run(pg, "wipe_99")
    repo.rm("wipe_a")
    repo.rm("wipe_b")
    repo.commit("remove every module")
    run(pg, "wipe_99")
    assert _attention(pg, rid), "precondition: the tripped gate is signalled"
    run(pg, "wipe_99", allow_mass_retire=True)
    assert module_node(neo4j_driver, "wipe_a") is None
    confirmed_attention = _attention(pg, rid)

    skipped = run(pg, "wipe_99")

    assert skipped["modules"] == 0, "precondition: the unchanged HEAD is skipped"
    assert _attention(pg, rid) is None, (
        f"a past run's signal lingers on the skip path: {_attention(pg, rid)!r} "
        f"(after the confirmed run: {confirmed_attention!r})"
    )


def test_skipped_repo_attention_keeps_a_degraded_parse_that_still_stands(
    pg, neo4j_driver, tmp_path,
):
    """A module whose file does not parse is still broken at the unchanged HEAD:
    the skip path keeps naming that file."""
    # GUARD: pre-existing behaviour (the degraded message survived skips before F38).
    repo = GitRepo(tmp_path, "broken_addons")
    write_module(repo, "ok_mod")
    write_module(repo, "broken_mod")
    repo.commit("add")
    (rid,) = register("broken_99", repo)
    run(pg, "broken_99")
    (repo.path / "broken_mod" / "models" / "record.py").write_text("def broken(:\n    pass\n")
    repo.commit("[WIP] broken_mod: half-done")
    run(pg, "broken_99")
    assert "broken_mod/models/record.py" in (_attention(pg, rid) or "")

    for _ in range(2):
        assert run(pg, "broken_99")["modules"] == 0
        assert "broken_mod/models/record.py" in (_attention(pg, rid) or "")


# ---------------------------------------------------------------------------
# F39 - a profile with no repo is reported apart
# ---------------------------------------------------------------------------

def test_index_all_reports_a_profile_without_repo_apart_from_ok_and_failed(
    pg, neo4j_driver, tmp_path,
):
    """One profile with a repo, one registered without any: the first is ok, the
    second is neither ok nor failed but listed in profiles_empty (so are the
    repo-less root profiles the migrations seed)."""
    from src.indexer.pipeline import index_all

    repo = GitRepo(tmp_path, "real_addons")
    write_module(repo, "real_mod")
    repo.commit("add")
    register("real_99", repo)
    repo_store().add_profile("empty_99", V)
    seeded_empty = {
        p["name"] for p in repo_store().list_profiles()
        if p["name"] not in {"real_99", "empty_99"}
    }

    summary = index_all(pg, embedder=FakeEmbedder(dim=1024))

    assert summary["profiles_ok"] == 1, summary
    assert summary["profiles_failed"] == []
    assert summary["profiles_empty"] == sorted({"empty_99", *seeded_empty})
    assert module_node(neo4j_driver, "real_mod") is not None


def test_cli_names_profiles_without_repo_and_still_exits_0(
    pg, neo4j_driver, tmp_path, monkeypatch, capsys, _ephemeral_pg_db,
):
    repo = GitRepo(tmp_path, "real_addons")
    write_module(repo, "real_mod")
    repo.commit("add")
    register("real_99", repo)
    repo_store().add_profile("empty_99", V)

    code, _, err = _cli(monkeypatch, capsys, _ephemeral_pg_db, "index-repo", "--all", "--no-embed")

    assert code == 0, err
    lines = [x for x in err.splitlines() if "with no repo registered, nothing indexed:" in x]
    assert len(lines) == 1, err
    assert "empty_99" in lines[0] and "real_99" not in lines[0]

    code, _, err = _cli(
        monkeypatch, capsys, _ephemeral_pg_db, "index-repo", "--profile", "empty_99", "--no-embed",
    )

    assert code == 0, err
    assert "Profile 'empty_99' has no repo registered: nothing indexed." in err


# ---------------------------------------------------------------------------
# Full-mode heal - the old directory goes to the prune (8b1d6fa)
# ---------------------------------------------------------------------------

def _with_rng(repo: GitRepo) -> None:
    rng = repo.path / "odoo" / "addons" / "base" / "rng"
    rng.mkdir(parents=True, exist_ok=True)
    for f in rf.RNG_DIR.iterdir():
        shutil.copy(f, rng / f.name)


def _lint_paths(driver) -> list[str]:
    with driver.session() as s:
        return sorted(
            r["f"] for r in s.run(
                "MATCH (l:LintViolation {odoo_version: $v}) RETURN l.file_path AS f", v=V,
            ).data()
        )


def test_full_run_heals_a_drifted_path_and_prunes_the_old_directorys_lint_violations(
    pg, neo4j_driver, tmp_path,
):
    """F7 residue: viin_ai_rag was indexed from an untracked ``.odoo-ai`` copy,
    so its Module path and its view lint violations name that copy. An operator
    runs ``--full``: the module is re-written at the tracked path and nothing
    indexed under the old directory survives - its lint violations are replaced
    by the same violations at the real path."""
    repo = GitRepo(tmp_path, "viindoo_addons")
    rf.write_viin_ai(repo.path)
    rf.write_viin_ai_rag(repo.path)
    _with_rng(repo)
    repo.commit("add viin_ai and viin_ai_rag")
    register("viindoo_99", repo)
    run(pg, "viindoo_99")
    real = [p for p in _lint_paths(neo4j_driver) if p.startswith(f"{rf.RETIRED}/")]
    assert real, "precondition: the rag views carry lint violations"
    prefix = _path_prefix(neo4j_driver, rf.RETIRED)
    old_dir = f".odoo-ai/{rf.RETIRED}"
    shutil.copytree(repo.path / rf.RETIRED, repo.path / old_dir)  # untracked copy
    _set_node(neo4j_driver, rf.RETIRED, path=prefix + old_dir)
    with neo4j_driver.session() as s:
        s.run(
            "MATCH (l:LintViolation {odoo_version: $v}) WHERE l.file_path STARTS WITH $p "
            "SET l.file_path = '.odoo-ai/' + l.file_path",
            v=V, p=f"{rf.RETIRED}/",
        ).consume()
    assert all(not p.startswith(f"{rf.RETIRED}/") for p in _lint_paths(neo4j_driver))

    run(pg, "viindoo_99", full_reindex=True)

    assert module_node(neo4j_driver, rf.RETIRED)["path"] == prefix + rf.RETIRED
    after = _lint_paths(neo4j_driver)
    assert [p for p in after if p.startswith(".odoo-ai/")] == [], (
        "lint violations of the old directory survived the --full heal"
    )
    assert [p for p in after if p.startswith(f"{rf.RETIRED}/")] == real


# ---------------------------------------------------------------------------
# F44 - the audit's would_rewrite is what the next run re-writes, every mode
# ---------------------------------------------------------------------------

P44 = "heal_99"
R44 = "heal_addons"
DRIFTED = ["m_noprof", "m_path", "m_repo"]


def _drifted_repo(tmp_path: Path, pg_conn, driver) -> tuple[GitRepo, int, str]:
    repo = GitRepo(tmp_path, R44)
    for name in (*DRIFTED, "m_ok", "m_touch"):
        write_module(repo, name)
    repo.commit("add")
    (rid,) = register(P44, repo)
    run(pg_conn, P44)
    return repo, rid, _path_prefix(driver, "m_ok")


def _drift(repo: GitRepo, driver, prefix: str) -> None:
    """m_path indexed from an untracked copy, m_noprof lost its profile (F24),
    m_repo names the repo's old clone dir."""
    copy = repo.path / ".odoo-ai" / "m_path"
    shutil.copytree(repo.path / "m_path", copy)
    _set_node(driver, "m_path", path=prefix + ".odoo-ai/m_path")
    _set_node(driver, "m_noprof", profile=[])
    _set_node(driver, "m_repo", repo=f"{R44}_old", repo_id=None)


@pytest.mark.parametrize("mode", ["sync", "incremental", "full"])
def test_audit_would_rewrite_is_exactly_what_the_next_run_heals(
    pg, neo4j_driver, tmp_path, monkeypatch, capsys, _ephemeral_pg_db, mode,
):
    """Three drifted modules; the next run is a sync (ledger behind HEAD), an
    incremental (one module touched, one added) or a full run (force-push). In
    every mode the audit lists exactly the three drifted modules - never the
    added one - and the real run heals exactly them; a second audit is clean."""
    repo, rid, prefix = _drifted_repo(tmp_path, pg, neo4j_driver)
    if mode == "sync":
        with pg.cursor() as cur:
            cur.execute("UPDATE repos SET presence_head_sha = NULL WHERE id = %s", (rid,))
    elif mode == "incremental":
        write_module(repo, "m_touch", extra_field="note = fields.Text()")
        write_module(repo, "m_new")
        repo.commit("[IMP] m_touch; [ADD] m_new")
    else:
        write_module(repo, "m_touch", extra_field="note = fields.Text()")
        run_git(repo.path, "add", "-A")
        run_git(repo.path, "commit", "--amend", "-q", "-m", "rewritten history")
        run_git(repo.path, "push", "--force", "origin", f"HEAD:refs/heads/{repo.branch}")
        run_git(repo.path, "fetch", "origin")
    _drift(repo, neo4j_driver, prefix)

    _, report = _audit(monkeypatch, capsys, _ephemeral_pg_db, "--profile", P44)
    entry = _repo_entry(report, R44)
    assert entry["next_run"] == mode
    predicted = sorted(i["name"] for i in entry.get("would_rewrite", []))
    assert predicted == DRIFTED
    if mode == "incremental":
        [noprof] = [i for i in entry["would_rewrite"] if i["name"] == "m_noprof"]
        assert noprof["reason"] == "no_node" and noprof.get("has_node") is True, noprof

    run(pg, P44)

    assert module_node(neo4j_driver, "m_path")["path"] == prefix + "m_path"
    assert module_node(neo4j_driver, "m_noprof")["profile"] == [P44]
    assert module_node(neo4j_driver, "m_repo")["repo"] == R44
    _, second = _audit(monkeypatch, capsys, _ephemeral_pg_db, "--profile", P44)
    again = _repo_entry(second, R44)
    assert again.get("would_rewrite", []) == [] and again["wrong_paths"] == []


# ---------------------------------------------------------------------------
# Held prune - an audit finding until confirmed
# ---------------------------------------------------------------------------

BULK = "viin_ai_bulk"


def _bulk_fields(n: int) -> str:
    # One field per line at the class-body indent of the (not yet dedented)
    # _lifecycle_repo._model_py template.
    return ("\n" + " " * 12).join(f"f{i:02d} = fields.Char()" for i in range(n))


def test_held_entity_prune_is_an_audit_finding_until_allow_mass_retire(
    pg, neo4j_driver, tmp_path, monkeypatch, capsys, _ephemeral_pg_db,
):
    """26 of the module's nodes disappear from one parse (> 50 %, >= 20): the
    run holds the prune. The weekly audit reports it as a held prune with its
    counts and exits 4 under --fail-on-findings, in JSON and text; once the
    operator confirms with --allow-mass-retire, the finding is gone and the
    detector is green.

    Updated for F49: the report schema is ``osm.lifecycle-audit/3`` (the
    shared-module prune finding ``shared_prunes`` joined the findings). The
    module has one owner, so the held prune stays a ``held_prunes`` finding and
    is NOT double-counted as a shared-module one."""
    repo = GitRepo(tmp_path, "bulk_addons")
    write_module(repo, BULK, extra_field=_bulk_fields(29))
    repo.commit("add bulk")
    register("bulk_99", repo)
    run(pg, "bulk_99")
    total = children(neo4j_driver, BULK)
    assert total >= 31, "precondition: the model, 30 fields and a method are indexed"
    write_module(repo, BULK, extra_field=_bulk_fields(3))
    repo.commit("[REF] bulk: drop 26 fields")
    run(pg, "bulk_99")
    assert children(neo4j_driver, BULK) == total, "precondition: the soft gate held the prune"

    code, report = _audit(
        monkeypatch, capsys, _ephemeral_pg_db, "--profile", "bulk_99", "--fail-on-findings",
    )

    held = _repo_entry(report, "bulk_addons").get("held_prunes")
    assert [(h["name"], h["odoo_version"], h["stale"], h["total"]) for h in held or []] == [
        (BULK, V, 26, total)
    ], held
    assert {"name", "odoo_version", "stale", "total", "rels_stale", "rels_total"} <= set(held[0])
    assert report["findings"].get("held_prunes") == 1
    assert report["findings"].get("shared_prunes") == 0
    assert code == 4
    assert report["schema"] == "osm.lifecycle-audit/3"
    code, out, _ = _cli(
        monkeypatch, capsys, _ephemeral_pg_db,
        "lifecycle-audit", "--profile", "bulk_99", "--fail-on-findings",
    )
    assert code == 4
    assert f"entity prune held: {BULK}@{V}" in out

    run(pg, "bulk_99", allow_mass_retire=True)

    assert children(neo4j_driver, BULK) == total - 26
    code, report = _audit(
        monkeypatch, capsys, _ephemeral_pg_db, "--profile", "bulk_99", "--fail-on-findings",
    )
    assert _repo_entry(report, "bulk_addons")["held_prunes"] == []
    assert report["findings"]["held_prunes"] == 0
    assert code == 0, report["findings"]
