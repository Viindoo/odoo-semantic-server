# SPDX-License-Identifier: AGPL-3.0-or-later
"""lifecycle-audit (ADR-0056 B11, plan T26) and the drift fixes it reports.

Business rules protected here:

- The audit is a DRY RUN: the graph, the ledger (``module_presence``), ``repos``,
  ``profiles``, ``embeddings`` and every sequence are byte-identical before and
  after it, in ``--json`` and in text mode, and it holds no ledger lock (an index
  run that wants the lock is never delayed by it, and it is not delayed by one).
- It lists exactly what the next real index run will do, proven by running that
  run afterwards and comparing the graph with the prediction.
- ``--fail-on-findings`` is the weekly drift detector (Q9): exit 4 on any
  finding, 0 on a clean graph. The JSON schema ``osm.lifecycle-audit/3`` is a
  contract for that timer and for the P3/P4 rollout review (``/2``: a
  soft-gate-held entity prune is a finding, ``held_prunes``; ``/3``: the
  shared-module entity prune of F49 is a finding, ``shared_prunes``, with
  ``shared_prune_waiting`` / ``shared_prune_rewrites`` per version and the
  post-deploy ``shared_parse_backlog`` per repo).
- F36: ONE plain run heals Module nodes whose path is not the registry winner
  (F15 posbox stub, F7 untracked ``.odoo-ai`` copy) or that name a stale repo;
  two co-owners of one module never re-write each other every night.
- ``_index_repo`` and the audit take the same skip / sync / incremental / full
  decision for the same repo state.

Real cases: ``viin_ai_rag`` merged into ``viin_ai`` then deleted,
``test_pylint`` renamed to ``test_viin_pylint`` (E2 list), the posbox
``point_of_sale`` stub nested in core ``point_of_sale`` (F15), 553 untracked
``.odoo-ai`` manifests in a tvtmaaddons13 clone (F7), a Module with a real path
and an empty profile list that the read side would answer "No" for (F24), a
never-cloned repo whose profile's orphans must wait (F22).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import textwrap
import threading
from pathlib import Path

import pytest

from tests.conftest import TEST_VERSION, make_manifest

pytestmark = [pytest.mark.postgres, pytest.mark.neo4j]

V = TEST_VERSION
POSBOX_STUB = "point_of_sale/tools/posbox/overwrite_after_init/home/pi/odoo/addons/point_of_sale"
# The real stub manifest (odoo17 addons/point_of_sale/tools/posbox/...): no name, no version.
POSBOX_STUB_MANIFEST = "{\n    'license': 'LGPL-3',\n}\n"

REQUIRED_FINDING_KEYS = {
    "would_retire", "would_drop_owner", "undecidable", "blocked", "orphan_modules",
    "child_orphans", "embedding_orphans", "modules_without_profile", "would_rewrite",
    "wrong_paths", "unapplied_changes", "errors", "held_prunes", "shared_prunes",
    "unparseable_kept",
}


# --------------------------------------------------------------------------- helpers

def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    )
    return r.stdout.strip()


def _write_module(
    repo: Path, rel: str, name: str | None = None, depends: tuple[str, ...] = (),
) -> None:
    name = name or Path(rel).name
    d = repo / rel
    make_manifest(d, name=name, version=f"{V}.1.0.0", depends=list(depends))
    (d / "__init__.py").write_text("from . import models\n")
    (d / "models").mkdir(parents=True, exist_ok=True)
    (d / "models" / "__init__.py").write_text(f"from . import {name}\n")
    (d / "models" / f"{name}.py").write_text(textwrap.dedent(f"""
        from odoo import fields, models


        class Thing(models.Model):
            _name = 'x_{name}.thing'
            _description = 'thing'

            label = fields.Char()
    """).lstrip())


def _touch_module(repo: Path, name: str, marker: str) -> None:
    f = repo / name / "models" / f"{name}.py"
    f.write_text(f.read_text() + f"\n# {marker}\n")


def _make_repo(
    tmp_path: Path, basename: str, modules: list[str], deps: dict | None = None,
) -> tuple[Path, Path]:
    deps = deps or {}
    origin = tmp_path / f"{basename}.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    work = tmp_path / basename
    work.mkdir()
    _git(work, "init")
    _git(work, "checkout", "-b", V)
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "T")
    for m in modules:
        _write_module(work, m, depends=deps.get(m, ()))
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "-u", "origin", V)
    return origin, work


def _commit_push(work: Path, message: str) -> None:
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", message)
    _git(work, "push", "-q", "origin", V)


def _register(profile: str, repos: list[tuple[Path, Path]]) -> list[int]:
    from src.db.pg import repo_store

    pid = repo_store().add_profile(profile, V)
    return [repo_store().add_repo(pid, str(o), V, str(w)) for o, w in repos]


def _repos_of(*profiles: str) -> list[dict]:
    from src.db.pg import repo_store

    out = []
    for p in profiles:
        out += [{**r, "profile_name": p} for r in repo_store().get_repos_for_profile(p)]
    return out


def _writer():
    from src.indexer.writer_neo4j import Neo4jWriter

    w = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    w.setup_indexes()
    return w


def _index(pg, profile: str) -> dict:
    from src.indexer.pipeline import index_profile

    return index_profile(pg, profile_name=profile, refresh=False)


def _module_node(driver, name: str) -> dict | None:
    with driver.session() as s:
        row = s.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) RETURN properties(m) AS p",
            n=name, v=V,
        ).single()
    return None if row is None else row["p"]


def _module_names(driver) -> set[str]:
    """Indexed modules (a real path); dependency stubs are not lifecycle-managed."""
    with driver.session() as s:
        return {
            r["n"] for r in s.run(
                "MATCH (m:Module {odoo_version: $v}) "
                "WHERE m.path IS NOT NULL AND m.path <> '' RETURN m.name AS n",
                v=V,
            ).data()
        }


def _set_node(driver, name: str, **props) -> None:
    with driver.session() as s:
        s.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) SET m += $props",
            n=name, v=V, props=props,
        ).consume()


def _path_prefix(driver, name: str) -> str:
    """How the writer stores Module.path, minus the module dir (abs or relative)."""
    path = _module_node(driver, name)["path"]
    assert path.endswith(name), path
    return path[: -len(name)]


def _snapshot(driver, pg) -> dict:
    """Every node/rel with its properties, the lifecycle tables and all sequences."""
    with driver.session() as s:
        nodes = sorted(
            json.dumps([sorted(r["l"]), r["p"]], sort_keys=True, default=str)
            for r in s.run("MATCH (n) RETURN labels(n) AS l, properties(n) AS p").data()
        )
        rels = sorted(
            json.dumps(r, sort_keys=True, default=str)
            for r in s.run(
                "MATCH (a)-[r]->(b) RETURN type(r) AS t, properties(r) AS p, "
                "elementId(a) AS a, elementId(b) AS b"
            ).data()
        )
    out: dict = {"nodes": len(nodes), "rels": len(rels)}
    out["graph_sha"] = hashlib.sha256("\n".join(nodes + rels).encode()).hexdigest()
    with pg.cursor() as cur:
        for table in ("module_presence", "repos", "profiles", "embeddings"):
            cur.execute(
                f"SELECT row_to_json(t)::text FROM (SELECT * FROM public.{table} ORDER BY id) t"
            )
            rows = [r[0] for r in cur.fetchall()]
            out[f"{table}_rows"] = len(rows)
            out[f"{table}_sha"] = hashlib.sha256("\n".join(rows).encode()).hexdigest()
        cur.execute(
            "SELECT sequencename, last_value FROM pg_sequences "
            "WHERE schemaname = 'public' ORDER BY sequencename"
        )
        out["sequences"] = [list(r) for r in cur.fetchall()]
    return out


def _cli(monkeypatch, capsys, *argv: str) -> tuple[int, str, str]:
    from src.indexer.__main__ import main
    from tests import conftest

    monkeypatch.setenv("PG_DSN", conftest.get_test_dsn())
    capsys.readouterr()
    code = main(["lifecycle-audit", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def _names(items) -> list[str]:
    return [i["name"] for i in items]


def _repo_entry(report: dict, basename: str) -> dict:
    [entry] = [r for r in report["repos"] if r["basename"] == basename]
    return entry


def _version_entry(report: dict) -> dict:
    [entry] = [v for v in report["versions"] if v["odoo_version"] == V]
    return entry


@pytest.fixture
def pg(clean_pg, clean_neo4j):
    from src.db.migrate import run_migrations

    run_migrations(clean_pg)
    return clean_pg


# --------------------------------------------------------------------------- scenario

PROFILE = "audit_p"
REPO = "tvtmaaddons"


@pytest.fixture
def drifted(pg, neo4j_driver, tmp_path):
    """One indexed repo, then upstream changes and the stale-graph debris of prod.

    Returns the expected audit prediction, derived from the rules:
    - deleted ``viin_ai_rag`` and renamed ``test_pylint`` -> would_retire, each
      with its removing commit; the rename carries its git successor;
    - ``viin_ai_skill`` deleted in git but without a ledger row (pre-ledger
      ghost) and ``tvtma_phantom`` (indexed from an untracked ``.odoo-ai`` copy,
      no git history) -> orphan modules;
    - a Model whose module has no Module node -> child orphan;
    - an embeddings row of a module no repo ships -> embedding orphan;
    - ``noprof_mod``: shipped, but its node lost its profile list -> F24, and
      the next (incremental) run re-writes it (F44) -> also would_rewrite
      (no_node on an existing node);
    - ``point_of_sale`` indexed at the posbox stub path, ``viin_ai`` at its
      untracked ``.odoo-ai`` copy, ``mod_moved`` naming the repo's old clone dir
      -> would_rewrite (path_drift shadowed / untracked, repo_drift).
    """
    modules = [
        "viin_ai", "viin_ai_rag", "test_pylint", "viin_ai_skill",
        "point_of_sale", "mod_moved", "noprof_mod",
    ]
    origin, work = _make_repo(tmp_path, REPO, modules)
    stub = work / POSBOX_STUB
    stub.mkdir(parents=True)
    (stub / "__manifest__.py").write_text(POSBOX_STUB_MANIFEST)
    _commit_push(work, "[ADD] point_of_sale: posbox image files")
    [rid] = _register(PROFILE, [(origin, work)])
    _index(pg, PROFILE)
    prefix = _path_prefix(neo4j_driver, "viin_ai")

    # Upstream history.
    _git(work, "rm", "-r", "-q", "viin_ai_rag")
    _commit_push(work, "[REM] viin_ai_rag: merged into viin_ai")
    _git(work, "rm", "-r", "-q", "viin_ai_skill")
    _commit_push(work, "[REM] viin_ai_skill: dropped")
    _git(work, "mv", "test_pylint", "test_viin_pylint")
    _commit_push(work, "[REF] test_pylint: rename the module to test_viin_pylint")
    # F7: untracked .odoo-ai copies on disk (never committed).
    _write_module(work, ".odoo-ai/viin_ai", "viin_ai")
    _write_module(work, ".odoo-ai/tvtma_phantom", "tvtma_phantom")

    with pg.cursor() as cur:
        cur.execute("DELETE FROM module_presence WHERE name = 'viin_ai_skill'")
        zero = "[" + ",".join(["0.0"] * 1024) + "]"
        cur.execute(
            "INSERT INTO embeddings (chunk_type, module, odoo_version, entity_name, "
            "model_name, file_path, chunk_idx, content, vec, profile_name) VALUES "
            "('method', 'ghost_embed', %s, 'ghost_embed.x', NULL, '/x.py', 0, 'x', "
            "%s::vector, %s)",
            (V, zero, PROFILE),
        )
    with neo4j_driver.session() as s:
        s.run(
            "CREATE (m:Module {name: 'tvtma_phantom', odoo_version: $v, profile: [$p], "
            "repo: $repo, repo_id: $rid, path: $path}) "
            "CREATE (:Model {name: 'x_tvtma_phantom.thing', module: 'tvtma_phantom', "
            "odoo_version: $v})-[:DEFINED_IN]->(m) "
            "CREATE (:Model {name: 'x_lost.thing', module: 'lost_mod', odoo_version: $v}) "
            # A dependency stub (dep-target MERGE of a depends entry no repo
            # ships): no path, no profile, no repo_id. Never an F24 finding; it
            # is reclaimed and re-created by the dep-stub GC, not the lifecycle.
            "CREATE (:Module {name: 'ai_base_ext', odoo_version: $v})",
            v=V, p=PROFILE, repo=REPO, rid=rid, path=prefix + ".odoo-ai/tvtma_phantom",
        ).consume()
    _set_node(neo4j_driver, "point_of_sale", path=prefix + POSBOX_STUB)
    _set_node(neo4j_driver, "viin_ai", path=prefix + ".odoo-ai/viin_ai")
    _set_node(neo4j_driver, "mod_moved", repo="tvtmaaddons_old", repo_id=None)
    _set_node(neo4j_driver, "noprof_mod", profile=[])
    return {
        "rid": rid,
        "work": work,
        "prefix": prefix,
        "would_retire": ["test_pylint", "viin_ai_rag"],
        "orphans": ["tvtma_phantom", "viin_ai_skill"],
        "child_orphans": ["lost_mod"],
        "embedding_orphans": ["ghost_embed"],
        "without_profile": ["noprof_mod"],
        "would_rewrite": {
            "point_of_sale": ("path_drift", "shadowed"),
            "viin_ai": ("path_drift", "untracked"),
            "mod_moved": ("repo_drift", None),
            "noprof_mod": ("no_node", None),
        },
    }


# --------------------------------------------------------------------------- T26

def test_audit_writes_nothing_in_json_and_text_mode(drifted, pg, neo4j_driver, monkeypatch, capsys):
    """A dry run on a drifted graph leaves graph, ledger, repos, profiles,
    embeddings and every sequence byte-identical - with findings present, in
    both output modes."""
    before = _snapshot(neo4j_driver, pg)

    code, out, _ = _cli(monkeypatch, capsys, "--profile", PROFILE, "--json", "--fail-on-findings")
    assert code == 4
    assert json.loads(out)["has_findings"] is True
    assert _snapshot(neo4j_driver, pg) == before

    code, out, _ = _cli(monkeypatch, capsys, "--profile", PROFILE)
    assert code == 0, "without --fail-on-findings a report with findings still exits 0"
    assert _snapshot(neo4j_driver, pg) == before


def test_audit_lists_exactly_what_the_next_run_will_do(
    drifted, pg, neo4j_driver, monkeypatch, capsys,
):
    """Each seeded drift appears in its category with its evidence, and nothing
    else is reported.

    Updated for F44: the next run is incremental, and an incremental run
    re-writes the unchanged F24 module ``noprof_mod`` (node present, profile
    lost), so the audit now lists it in ``would_rewrite`` as well as in
    ``modules_without_profile`` (the old count of 3 under-predicted the run).
    The findings dict gains ``held_prunes`` (schema /2), zero here.

    Updated for F49 (schema /3): the findings dict also carries
    ``shared_prunes`` - what the shared-module rule would prune or holds. This
    fixture has no module two repos ship, so the prediction is 0 and the
    version entry lists none, waits for none and sends no owner back; every
    other count is unchanged.

    Updated for E2E-D1: the findings dict also carries ``unparseable_kept`` -
    indexed modules whose manifest was read but does not parse, kept as they
    are (the next run exits 3 for them). Every module of this fixture parses,
    so the prediction is 0 and the repo entry lists none; every other count is
    unchanged (the dict is still compared whole, so an unexpected finding
    still fails)."""
    code, out, _ = _cli(monkeypatch, capsys, "--profile", PROFILE, "--json")
    assert code == 0
    report = json.loads(out)
    repo = _repo_entry(report, REPO)
    assert repo["next_run"] == "incremental"
    assert repo["scan"]["trusted"] is True
    assert repo["scan"]["untracked_manifests"] == 2
    assert repo["scan"]["shadowed"] == 1

    retire = {i["name"]: i for i in repo["would_retire"]}
    assert sorted(retire) == drifted["would_retire"]
    assert _names(repo["would_retire"]) == sorted(retire), "name lists are sorted"
    assert retire["viin_ai_rag"]["removing_commit"]["subject"] == (
        "[REM] viin_ai_rag: merged into viin_ai"
    )
    assert retire["test_pylint"]["removing_commit"]["subject"] == (
        "[REF] test_pylint: rename the module to test_viin_pylint"
    )
    assert retire["test_pylint"]["successor"]["names"] == ["test_viin_pylint"]
    assert repo["would_drop_owner"] == [] and repo["undecidable"] == []
    assert repo["blocked"] == []

    drift = {i["name"]: (i["reason"], i.get("kind")) for i in repo.get("would_rewrite", [])}
    assert drift == drifted["would_rewrite"]
    by_name = {i["name"]: i for i in repo["would_rewrite"]}
    assert by_name["point_of_sale"]["indexed_path"] == POSBOX_STUB
    assert by_name["point_of_sale"]["winner_path"] == "point_of_sale"
    assert by_name["viin_ai"]["indexed_path"] == ".odoo-ai/viin_ai"
    assert by_name["noprof_mod"].get("has_node") is True, by_name["noprof_mod"]
    assert repo["wrong_paths"] == [], "the next run heals them: they are not left-over drift"

    ver = _version_entry(report)
    orphans = {o["name"]: o for o in ver["orphan_modules"]}
    assert sorted(orphans) == drifted["orphans"]
    assert all(o["deferred_for"] == [] for o in orphans.values())
    assert orphans["viin_ai_skill"]["evidence"]["removing_commit"]["subject"] == (
        "[REM] viin_ai_skill: dropped"
    )
    assert ver["child_orphans"] == drifted["child_orphans"]
    assert [(e["module"], e["profile"], e["rows"]) for e in ver["embedding_orphans"]] == [
        ("ghost_embed", PROFILE, 1)
    ]
    assert _names(ver["modules_without_profile"]) == drifted["without_profile"], (
        "a dependency stub (no path, no profile) is not an F24 finding"
    )
    assert ver["gates_tripped"] == [] and ver["unsynced_repos"] == []

    assert report["findings"] == {
        "would_retire": 2, "would_drop_owner": 0, "undecidable": 0, "blocked": 0,
        "orphan_modules": 2, "child_orphans": 1, "embedding_orphans": 1,
        "modules_without_profile": 1, "would_rewrite": 4, "wrong_paths": 0,
        "unapplied_changes": 0, "errors": 0, "held_prunes": 0, "shared_prunes": 0,
        "unparseable_kept": 0,
    }
    assert repo["unparseable_kept"] == []
    assert ver["orphans_unparseable"] == []
    assert ver["shared_prunes"] == [] and ver["shared_prune_waiting"] == {}
    assert ver["shared_prune_rewrites"] == {}


def test_next_real_run_does_what_the_audit_predicted(
    drifted, pg, neo4j_driver, monkeypatch, capsys,
):
    """Oracle: run the real index afterwards; every predicted retire / sweep /
    re-write happened, nothing unpredicted was removed, and a second audit is
    clean (F36: one plain run corrects the drifted paths)."""
    code, out, _ = _cli(monkeypatch, capsys, "--profile", PROFILE, "--json")
    report = json.loads(out)
    repo = _repo_entry(report, REPO)
    ver = _version_entry(report)
    predicted_gone = set(_names(repo["would_retire"])) | set(_names(ver["orphan_modules"]))
    modules_before = _module_names(neo4j_driver)

    _index(pg, PROFILE)

    modules_after = _module_names(neo4j_driver)
    assert modules_before - modules_after == predicted_gone
    assert "test_viin_pylint" in modules_after
    with neo4j_driver.session() as s:
        child_left = s.run(
            "MATCH (c {odoo_version: $v}) WHERE c.module IN $m RETURN count(c) AS n",
            v=V, m=ver["child_orphans"],
        ).single()["n"]
    assert child_left == 0
    with pg.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM embeddings WHERE module = ANY(%s)",
            ([e["module"] for e in ver["embedding_orphans"]],),
        )
        assert cur.fetchone()[0] == 0

    prefix = drifted["prefix"]
    assert _module_node(neo4j_driver, "point_of_sale")["path"] == prefix + "point_of_sale"
    assert _module_node(neo4j_driver, "viin_ai")["path"] == prefix + "viin_ai"
    assert _module_node(neo4j_driver, "mod_moved")["repo"] == REPO
    assert _module_node(neo4j_driver, "noprof_mod")["profile"] == [PROFILE], (
        "F24: the run re-writes a shipped module whose node lost its profile"
    )

    code, out, err = _cli(monkeypatch, capsys, "--profile", PROFILE, "--json", "--fail-on-findings")
    second = json.loads(out)
    assert code == 0, err
    assert second["has_findings"] is False
    assert set(second["findings"].values()) == {0}
    assert _repo_entry(second, REPO)["next_run"] == "skip"


def test_fail_on_findings_exits_zero_on_a_clean_graph(
    pg, neo4j_driver, tmp_path, monkeypatch, capsys,
):
    """The weekly detector stays green on a freshly indexed repo."""
    origin, work = _make_repo(tmp_path, "clean_addons", ["a1", "a2"])
    _register("clean_p", [(origin, work)])
    _index(pg, "clean_p")

    code, out, err = _cli(
        monkeypatch, capsys, "--profile", "clean_p", "--json", "--fail-on-findings",
    )
    report = json.loads(out)
    assert code == 0, err
    assert report["has_findings"] is False
    assert set(report["findings"].values()) == {0}
    assert _repo_entry(report, "clean_addons")["next_run"] == "skip"

    code, out, err = _cli(monkeypatch, capsys, "--profile", "clean_p", "--fail-on-findings")
    assert code == 0, err
    assert out.strip().splitlines()[-1].startswith("findings: none")


def test_fail_on_findings_exits_4_and_names_the_findings(drifted, monkeypatch, capsys):
    code, out, err = _cli(monkeypatch, capsys, "--profile", PROFILE, "--fail-on-findings")
    assert code == 4
    assert "would_retire=2" in err
    last = out.strip().splitlines()[-1]
    assert last.startswith("findings: ") and "would_retire=2" in last
    for name in ("viin_ai_rag", "test_viin_pylint", "tvtma_phantom", "noprof_mod"):
        assert name in out


def test_json_report_keeps_the_osm_lifecycle_audit_3_schema(drifted, monkeypatch, capsys):
    """Keys and types the weekly timer and the rollout review read.

    Updated for the held-prune finding: the shape changed (``held_prunes`` in
    each repo entry and in ``findings``; ``prune_rewrites`` in each version
    entry), so the schema id is ``osm.lifecycle-audit/2``. Every /1 key is
    still pinned with its type.

    Updated for F49 (the shared-module entity prune): the shape changed again -
    ``shared_prunes`` in ``findings`` and in each version entry (with
    ``shared_prune_waiting`` and ``shared_prune_rewrites``), and
    ``shared_parse_backlog`` in each repo entry - so the schema id is
    ``osm.lifecycle-audit/3``. Every /1 and /2 key is still pinned with its
    type; the new keys are pinned too.

    Updated for E2E-D1 and D1/D3 (additive, so the schema id stays ``/3``):
    ``findings`` gains ``unparseable_kept``; each repo entry gains
    ``unparseable_kept`` (list), ``scan.unreadable_manifests`` (int) and
    ``gates.baseline`` (``ledger`` / ``graph``); each version entry gains
    ``orphans_unparseable`` (list) and ``excluded_owner_waiting`` (dict).
    Every earlier key is still pinned with its type."""
    _, out, _ = _cli(monkeypatch, capsys, "--profile", PROFILE, "--json")
    report = json.loads(out)

    assert report["schema"] == "osm.lifecycle-audit/3"
    assert report["dry_run"] is True
    assert isinstance(report["generated_at"], str) and "T" in report["generated_at"]
    assert isinstance(report["duration_s"], float | int)
    assert report["scope"] == {"profile": PROFILE, "all": False, "version": None}
    assert set(report["findings"]) == REQUIRED_FINDING_KEYS
    assert all(isinstance(n, int) for n in report["findings"].values())
    assert report["has_findings"] is any(report["findings"].values())

    repo_keys = {
        "repo_id": int, "profile": str, "url": str, "basename": str, "branch": str,
        "local_path": str, "profile_version": str, "odoo_version": str, "head": str,
        "head_sha": str, "presence_head_sha": (str, type(None)), "next_run": str,
        "scan": dict, "scan_attention": list, "attention": list, "gates": dict,
        "transitions": dict, "pending": list, "unapplied_changes": dict,
        "would_rewrite": list, "wrong_paths": list, "would_retire": list,
        "would_drop_owner": list, "undecidable": list, "blocked": list,
        "held_prunes": list, "shared_parse_backlog": dict, "error": (str, type(None)),
        "unparseable_kept": list,
    }
    repo = _repo_entry(report, REPO)
    for key, typ in repo_keys.items():
        assert key in repo, key
        assert isinstance(repo[key], typ), (key, repo[key])
    assert repo["next_run"] in {"skip", "sync", "incremental", "full", "not_cloned"}
    assert {
        "complete", "trusted", "tracked_available", "present", "excluded", "shadowed",
        "untracked_manifests", "missing_manifests", "unreadable_manifests",
    } <= set(repo["scan"])
    assert isinstance(repo["scan"]["unreadable_manifests"], int)
    assert {
        "scan_ok", "mass_ok", "retire_allowed", "tripped", "bypassed", "reasons",
        "n_soft_drop", "n_present_before", "baseline",
    } <= set(repo["gates"])
    assert repo["gates"]["baseline"] in {"ledger", "graph"}
    assert set(repo["shared_parse_backlog"]) == {"remaining", "next_run"}
    assert isinstance(repo["shared_parse_backlog"]["remaining"], int)
    assert isinstance(repo["shared_parse_backlog"]["next_run"], list)
    for item in repo["would_retire"]:
        assert {"name", "repo_id", "repo", "profile", "path", "removing_commit",
                "successor", "blocked_by"} <= set(item)
        assert {"sha", "date", "subject"} <= set(item["removing_commit"])

    ver_keys = {
        "odoo_version": str, "would_retire": list, "would_drop_owner": list,
        "undecidable": dict, "blocked": dict, "other_pending": list,
        "orphan_modules": list, "child_orphans": list, "embedding_orphans": list,
        "gates_tripped": list, "unsynced_repos": list, "modules_without_profile": list,
        "errors": dict, "prune_rewrites": list, "shared_prunes": list,
        "shared_prune_waiting": dict, "shared_prune_rewrites": dict,
        "orphans_unparseable": list, "excluded_owner_waiting": dict,
    }
    ver = _version_entry(report)
    for key, typ in ver_keys.items():
        assert key in ver, key
        assert isinstance(ver[key], typ), (key, ver[key])
    for o in ver["orphan_modules"]:
        assert {"name", "deferred_for", "evidence"} <= set(o)
    for m in ver["modules_without_profile"]:
        assert {"name", "path", "repo", "repo_id", "children"} <= set(m)
    for e in ver["embedding_orphans"]:
        assert {"module", "profile", "rows"} <= set(e)


def test_unknown_profile_is_a_usage_error(pg, monkeypatch, capsys):
    code, _, err = _cli(monkeypatch, capsys, "--profile", "no_such_profile")
    assert code == 2
    assert "no repos registered for profile 'no_such_profile'" in err


def test_version_filter_audits_only_repos_keyed_at_that_version(drifted, monkeypatch, capsys):
    other = "97.0"
    _, out, _ = _cli(monkeypatch, capsys, "--profile", PROFILE, "--version", other, "--json")
    report = json.loads(out)
    assert report["scope"]["version"] == other
    assert report["repos"] == []
    assert all(v["odoo_version"] == other for v in report["versions"])

    _, out, _ = _cli(monkeypatch, capsys, "--profile", PROFILE, "--version", V, "--json")
    assert [r["basename"] for r in json.loads(out)["repos"]] == [REPO]


# --------------------------------------------------------------------------- no lock

class _LockSamplingWriter:
    """Delegates the graph reads; samples pg_locks of this database at each one."""

    def __init__(self, writer, pg, holder_pid: int) -> None:
        self._writer = writer
        self._pg = pg
        self._holder_pid = holder_pid
        self.samples: list[list] = []

    def __getattr__(self, name):
        target = getattr(self._writer, name)
        if not callable(target):
            return target

        def call(*args, **kwargs):
            with self._pg.cursor() as cur:
                cur.execute(
                    "SELECT l.locktype, l.mode, coalesce(c.relname, ''), a.application_name "
                    "FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                    "LEFT JOIN pg_class c ON c.oid = l.relation "
                    "WHERE l.database = (SELECT oid FROM pg_database "
                    "                    WHERE datname = current_database()) "
                    "AND l.pid NOT IN (pg_backend_pid(), %s) AND l.granted "
                    "AND (l.locktype = 'advisory' OR (l.locktype = 'relation' "
                    "     AND c.relnamespace = 'public'::regnamespace "
                    "     AND l.mode NOT IN ('AccessShareLock')))",
                    (self._holder_pid,),
                )
                self.samples.append(cur.fetchall())
            return target(*args, **kwargs)

        return call


def test_audit_takes_no_ledger_lock_and_is_not_blocked_by_one(drifted, pg, neo4j_driver):
    """An index run holding retire:<version> does not stall the audit, and the
    audit holds no advisory lock and no write lock on a real table while it
    runs, so it can never delay an index run."""
    import psycopg2

    from src.db.module_presence import retire_lock_id
    from src.indexer.lifecycle_audit import audit_lifecycle, open_simulation
    from tests import conftest

    holder = psycopg2.connect(conftest.get_test_dsn())
    holder.autocommit = True
    writer = _writer()
    try:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (retire_lock_id(V),))
            cur.execute("SELECT pg_backend_pid()")
            holder_pid = cur.fetchone()[0]
        spy = _LockSamplingWriter(writer, pg, holder_pid)
        conn = open_simulation(conftest.get_test_dsn())
        result: dict = {}

        def run():
            try:
                result["report"] = audit_lifecycle(
                    _repos_of(PROFILE), writer=spy, conn=conn, scope={"profile": PROFILE},
                )
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
                result["error"] = exc

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=90)
        assert not t.is_alive(), "the audit waited on the ledger lock held by an index run"
        conn.close()
    finally:
        with holder.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock_all()")
        holder.close()
        writer.close()

    assert "error" not in result, result.get("error")
    report = result["report"]
    assert report["findings"]["errors"] == 0
    assert _names(_repo_entry(report, REPO)["would_retire"]) == drifted["would_retire"]
    assert spy.samples, "the audit made graph reads"
    assert all(sample == [] for sample in spy.samples), spy.samples


def test_simulation_session_refuses_writes_to_the_real_tables(pg):
    """The audit's PG session can only write its private copies."""
    from psycopg2 import errors

    from src.db.pg import repo_store
    from src.indexer.lifecycle_audit import open_simulation
    from tests import conftest

    pid = repo_store().add_profile("ro_p", V)
    rid = repo_store().add_repo(pid, "file:///x", V, "/nonexistent/x")
    conn = open_simulation(conftest.get_test_dsn())
    try:
        for stmt, args in (
            ("UPDATE public.repos SET head_sha = 'x' WHERE id = %s", (rid,)),
            ("INSERT INTO public.profiles (name, odoo_version) VALUES ('ro_q', %s)", (V,)),
            ("DELETE FROM public.module_presence", None),
        ):
            with pytest.raises(errors.ReadOnlySqlTransaction):
                with conn.cursor() as cur:
                    cur.execute(stmt, args)
        with conn.cursor() as cur:
            cur.execute("UPDATE repos SET head_sha = 'simulated' WHERE id = %s", (rid,))
            assert cur.rowcount == 1
    finally:
        conn.close()
    with pg.cursor() as cur:
        cur.execute("SELECT head_sha FROM repos WHERE id = %s", (rid,))
        assert cur.fetchone()[0] is None


# --------------------------------------------------------------------------- gates, owners, F22

def test_all_profiles_audit_gates_owner_drops_and_never_cloned_repo(
    pg, neo4j_driver, tmp_path, monkeypatch, capsys,
):
    """--all: a 25-module repo losing 22 trips the mass-retire gate (nothing
    retired, all 22 blocked); a module two repos ship, dropped by one, loses only
    that owner; a never-cloned repo keeps its profile's orphans deferred (F22)
    while another profile's orphan is swept. The real runs afterwards match."""
    big = [f"b{i:02d}" for i in range(24)]
    o_big, w_big = _make_repo(tmp_path, "big", [*big, "shared_mod"])
    o_oth, w_oth = _make_repo(tmp_path, "other", ["shared_mod", "o1"])
    [big_id] = _register("pa", [(o_big, w_big)])
    [other_id] = _register("pb", [(o_oth, w_oth)])
    _index(pg, "pa")
    _index(pg, "pb")
    [never_id] = _register("pc", [(tmp_path / "never.git", tmp_path / "never_cloned")])
    with neo4j_driver.session() as s:
        # Indexed modules no repo ships any more (a repo_id and a child, so the
        # dep-stub GC never touches them; only the lifecycle sweep may).
        s.run(
            "CREATE (a:Module {name: 'pc_ghost', odoo_version: $v, profile: ['pc'], "
            "repo: 'never_cloned', repo_id: $nid, path: 'pc_ghost'}) "
            "CREATE (:Model {name: 'x_pc_ghost.thing', module: 'pc_ghost', "
            "odoo_version: $v})-[:DEFINED_IN]->(a) "
            "CREATE (b:Module {name: 'pb_ghost', odoo_version: $v, profile: ['pb'], "
            "repo: 'other', repo_id: $oid, path: 'pb_ghost'}) "
            "CREATE (:Model {name: 'x_pb_ghost.thing', module: 'pb_ghost', "
            "odoo_version: $v})-[:DEFINED_IN]->(b)",
            v=V, nid=never_id, oid=other_id,
        ).consume()
    _git(w_big, "rm", "-r", "-q", *big[:22])
    _commit_push(w_big, "[REM] drop 22 modules")
    _git(w_oth, "rm", "-r", "-q", "shared_mod")
    _commit_push(w_oth, "[REM] shared_mod: now shipped by big only")

    before = _snapshot(neo4j_driver, pg)
    code, out, _ = _cli(monkeypatch, capsys, "--all", "--json", "--fail-on-findings")
    assert code == 4
    assert _snapshot(neo4j_driver, pg) == before
    report = json.loads(out)
    assert report["scope"]["all"] is True

    b = _repo_entry(report, "big")
    assert b["gates"]["tripped"] == ["mass_retire"]
    assert _names(b["blocked"]) == sorted(big[:22])
    assert {i["reason"] for i in b["blocked"]} == {"gate:mass_retire"}
    assert b["would_retire"] == []

    o = _repo_entry(report, "other")
    assert _names(o["would_drop_owner"]) == ["shared_mod"]
    assert o["would_drop_owner"][0]["kept_by"] == [f"big (repo id={big_id})"]
    assert o["would_retire"] == []

    n = _repo_entry(report, "never_cloned")
    assert n["next_run"] == "not_cloned"

    ver = _version_entry(report)
    orphans = {x["name"]: x["deferred_for"] for x in ver["orphan_modules"]}
    assert orphans == {"pc_ghost": ["pc"], "pb_ghost": []}
    unsynced = {u["repo"]: u["why"] for u in ver["unsynced_repos"]}
    assert unsynced["never_cloned"] == "no checkout"
    assert "big" in unsynced, "a tripped gate keeps big's ledger behind its HEAD"
    assert "other" not in unsynced

    # Oracle: the real runs.
    _index(pg, "pa")
    _index(pg, "pb")
    names = _module_names(neo4j_driver)
    assert set(big[:22]) <= names, "blocked by the mass-retire gate: nothing deleted"
    assert "pb_ghost" not in names and "pc_ghost" in names
    shared = _module_node(neo4j_driver, "shared_mod")
    assert "pa" in shared["profile"] and "pb" not in shared["profile"]


def test_skip_mode_reports_ledger_changes_the_nightly_run_will_not_apply(
    pg, neo4j_driver, tmp_path, monkeypatch, capsys,
):
    """At an unchanged HEAD the nightly run skips the repo, so a ledger that
    disagrees with the scan stays wrong: the audit reports it as a finding."""
    origin, work = _make_repo(tmp_path, "skipper", ["o1", "o2"])
    _register("skip_p", [(origin, work)])
    _index(pg, "skip_p")
    with pg.cursor() as cur:
        cur.execute(
            "UPDATE module_presence SET state = 'excluded', exclusion_reason = 'unparseable' "
            "WHERE name = 'o1'"
        )
    before = _snapshot(neo4j_driver, pg)
    code, out, _ = _cli(monkeypatch, capsys, "--profile", "skip_p", "--json", "--fail-on-findings")
    assert _snapshot(neo4j_driver, pg) == before
    entry = _repo_entry(json.loads(out), "skipper")
    assert entry["next_run"] == "skip"
    assert entry["unapplied_changes"] == {"included": ["o1"]}
    assert code == 4


def test_skip_mode_reports_a_stub_path_the_next_run_will_not_heal(
    pg, neo4j_driver, tmp_path, monkeypatch, capsys,
):
    """F15 at an unchanged HEAD: the next run is the zero-cost skip, so the stub
    path is left-over drift (wrong_paths), not a predicted re-write."""
    origin, work = _make_repo(tmp_path, "core_like", ["point_of_sale", "web"])
    stub = work / POSBOX_STUB
    stub.mkdir(parents=True)
    (stub / "__manifest__.py").write_text(POSBOX_STUB_MANIFEST)
    _commit_push(work, "[ADD] posbox")
    _register("core_p", [(origin, work)])
    _index(pg, "core_p")
    prefix = _path_prefix(neo4j_driver, "web")
    _set_node(neo4j_driver, "point_of_sale", path=prefix + POSBOX_STUB)

    code, out, _ = _cli(monkeypatch, capsys, "--profile", "core_p", "--json", "--fail-on-findings")
    entry = _repo_entry(json.loads(out), "core_like")
    assert entry["next_run"] == "skip"
    assert [(i["name"], i["kind"]) for i in entry["wrong_paths"]] == [("point_of_sale", "shadowed")]
    assert entry.get("would_rewrite", []) == []
    assert code == 4


# --------------------------------------------------------------------------- F36

def test_first_plain_run_after_deploy_heals_stub_and_untracked_paths(
    pg, neo4j_driver, tmp_path, monkeypatch, capsys,
):
    """The first run after the ledger ships is a sync run (presence HEAD unset):
    it re-writes the posbox stub path and the .odoo-ai copy path without --full,
    and a second audit is clean."""
    origin, work = _make_repo(tmp_path, "core17", ["point_of_sale", "mail"])
    stub = work / POSBOX_STUB
    stub.mkdir(parents=True)
    (stub / "__manifest__.py").write_text(POSBOX_STUB_MANIFEST)
    _commit_push(work, "[ADD] posbox")
    [rid] = _register("core17_p", [(origin, work)])
    _index(pg, "core17_p")
    _write_module(work, ".odoo-ai/mail", "mail")
    prefix = _path_prefix(neo4j_driver, "mail")
    _set_node(neo4j_driver, "point_of_sale", path=prefix + POSBOX_STUB)
    _set_node(neo4j_driver, "mail", path=prefix + ".odoo-ai/mail")
    with pg.cursor() as cur:
        cur.execute("UPDATE repos SET presence_head_sha = NULL WHERE id = %s", (rid,))

    _, out, _ = _cli(monkeypatch, capsys, "--profile", "core17_p", "--json")
    entry = _repo_entry(json.loads(out), "core17")
    assert entry["next_run"] == "sync"
    assert {i["name"]: i.get("kind") for i in entry.get("would_rewrite", [])} == {
        "point_of_sale": "shadowed", "mail": "untracked",
    }

    _index(pg, "core17_p")
    assert _module_node(neo4j_driver, "point_of_sale")["path"] == prefix + "point_of_sale"
    assert _module_node(neo4j_driver, "mail")["path"] == prefix + "mail"

    code, out, err = _cli(
        monkeypatch, capsys, "--profile", "core17_p", "--json", "--fail-on-findings",
    )
    second = json.loads(out)
    assert code == 0, err
    assert second["findings"]["would_rewrite"] == 0
    assert second["findings"]["wrong_paths"] == 0


def test_two_co_owners_do_not_rewrite_each_other_every_night(
    pg, neo4j_driver, tmp_path, monkeypatch, capsys,
):
    """Two repos of one profile both ship shared_mod. Nightly runs with new
    commits elsewhere re-parse only the changed modules (no ping-pong on the
    shared node), and a run with no new commit is the zero-cost skip."""
    # GUARD: pre-existing behaviour (co-owners never re-wrote each other before
    # the repo_drift self-heal; the new rule must keep it that way).
    oa, wa = _make_repo(tmp_path, "repo_a", ["shared_mod", "a_only"])
    ob, wb = _make_repo(tmp_path, "repo_b", ["shared_mod", "b_only"])
    _register("co_p", [(oa, wa), (ob, wb)])
    _index(pg, "co_p")

    for night in range(2):
        _touch_module(wa, "a_only", f"night {night}")
        _commit_push(wa, f"[IMP] a_only: night {night}")
        _touch_module(wb, "b_only", f"night {night}")
        _commit_push(wb, f"[IMP] b_only: night {night}")
        _, out, _ = _cli(monkeypatch, capsys, "--profile", "co_p", "--json")
        report = json.loads(out)
        for basename in ("repo_a", "repo_b"):
            entry = _repo_entry(report, basename)
            assert entry.get("would_rewrite", []) == [], (night, basename)
        summary = _index(pg, "co_p")
        assert summary["modules"] == 2, (
            f"night {night}: only a_only and b_only changed, shared_mod must not be re-written"
        )

    _, out, _ = _cli(monkeypatch, capsys, "--profile", "co_p", "--json", "--fail-on-findings")
    report = json.loads(out)
    assert [r["next_run"] for r in report["repos"]] == ["skip", "skip"]
    assert report["has_findings"] is False
    shared_before = _module_node(neo4j_driver, "shared_mod")
    summary = _index(pg, "co_p")
    assert summary["modules"] == 0
    assert _module_node(neo4j_driver, "shared_mod") == shared_before


# --------------------------------------------------------------------------- shared plan

def test_index_repo_and_audit_take_the_same_run_decision(pg, neo4j_driver, tmp_path):
    """For each repo state the audit's next_run is the mode the real
    _index_repo then takes: full (never indexed), skip (unchanged), sync (ledger
    behind an unchanged HEAD), incremental (one module changed), full
    (force-push rewrote the indexed commit)."""
    from src.db.pg import repo_store
    from src.indexer.lifecycle_audit import audit_lifecycle, open_simulation
    from src.indexer.pipeline_repo import _index_repo
    from tests import conftest

    origin, work = _make_repo(tmp_path, "planned", ["m1", "m2", "m3"])
    [rid] = _register("plan_p", [(origin, work)])
    writer = _writer()

    def audited_mode() -> str:
        conn = open_simulation(conftest.get_test_dsn())
        try:
            report = audit_lifecycle(_repos_of("plan_p"), writer=writer, conn=conn)
        finally:
            conn.close()
        return _repo_entry(report, "planned")["next_run"]

    def real_mode() -> tuple[str, int]:
        [repo] = repo_store().get_repos_for_profile("plan_p")
        counters = _index_repo(repo, writer, pg_conn=pg, profile_name="plan_p", refresh=False)
        n = counters["modules"]
        if "lifecycle" not in counters:
            return "skip", n
        if n == 0:
            return "sync", n
        return ("full" if n == 3 else "incremental"), n

    def force_push():
        _git(work, "reset", "-q", "--hard", "HEAD~1")
        _touch_module(work, "m3", "rewritten")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "[FIX] m3: rewritten history")
        _git(work, "push", "-q", "-f", "origin", V)

    def ledger_behind():
        with pg.cursor() as cur:
            cur.execute("UPDATE repos SET presence_head_sha = NULL WHERE id = %s", (rid,))

    def one_module_changed():
        _touch_module(work, "m2", "changed")
        _commit_push(work, "[IMP] m2: changed")

    states = [
        ("never indexed", None, "full"),
        ("unchanged", None, "skip"),
        ("ledger behind HEAD", ledger_behind, "sync"),
        ("one module changed", one_module_changed, "incremental"),
        ("force-push", force_push, "full"),
    ]
    try:
        for label, prepare, expected in states:
            if prepare is not None:
                prepare()
            predicted = audited_mode()
            taken, n = real_mode()
            assert (predicted, taken) == (expected, expected), (label, predicted, taken, n)
    finally:
        writer.close()


# --------------------------------------------------------------------------- F37

@pytest.mark.neo4j
def test_writer_index_setup_creates_odoo_version_leading_indexes(clean_neo4j, neo4j_driver):
    """Version-wide lifecycle reads ({odoo_version: $v}) need an index whose
    LEADING key is odoo_version: (odoo_version, module) on module-owned labels,
    LintViolation(odoo_version) (violations are usually module-less) and
    Module(odoo_version, name). orphan_child_keys answers the same with them."""
    from src.indexer.writer_neo4j import MODULE_CHILD_LABELS

    with neo4j_driver.session() as s:
        s.run(
            "CREATE (:Module {name: 'kept', odoo_version: $v}) "
            "CREATE (:Field {name: 'f1', module: 'gone_a', odoo_version: $v}) "
            "CREATE (:Field {name: 'f2', module: 'gone_a', odoo_version: $v}) "
            "CREATE (:Method {name: 'm1', module: 'gone_a', odoo_version: $v}) "
            "CREATE (:View {xmlid: 'gone_b.view_form', module: 'gone_b', odoo_version: $v}) "
            "CREATE (:Field {name: 'f3', module: 'kept', odoo_version: $v}) "
            "CREATE (:LintViolation {view_xmlid: 'gone_c.view_list', odoo_version: $v}) "
            "CREATE (:TestClass {name: 'T', module: '@framework', odoo_version: $v})",
            v=V,
        ).consume()
        leading = [
            r["name"] for r in s.run(
                "SHOW INDEXES YIELD name, properties WHERE properties[0] = 'odoo_version' "
                "RETURN name"
            ).data()
        ]
        for name in leading:
            s.run(f"DROP INDEX `{name}`").consume()

    from src.indexer.writer_neo4j import Neo4jWriter

    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    expected = {"gone_a": {"Field": 2, "Method": 1}, "gone_b": {"View": 1},
                "gone_c": {"LintViolation": 1}}
    try:
        without_indexes = writer.orphan_child_keys(V)
        writer.setup_indexes()
        with neo4j_driver.session() as s:
            s.run("CALL db.awaitIndexes(300)").consume()
            indexes = {
                (tuple(r["l"] or ()), tuple(r["p"] or ()))
                for r in s.run(
                    "SHOW INDEXES YIELD labelsOrTypes AS l, properties AS p, state "
                    "WHERE state = 'ONLINE' RETURN l, p"
                ).data()
            }
        for label in MODULE_CHILD_LABELS:
            want = ("odoo_version",) if label == "LintViolation" else ("odoo_version", "module")
            assert ((label,), want) in indexes, (label, want)
        assert (("Module",), ("odoo_version", "name")) in indexes
        assert without_indexes == expected
        assert writer.orphan_child_keys(V) == expected
    finally:
        writer.setup_indexes()
        writer.close()
