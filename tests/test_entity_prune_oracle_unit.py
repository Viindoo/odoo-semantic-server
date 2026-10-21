# SPDX-License-Identifier: AGPL-3.0-or-later
"""The per-repo entity prune decides from the source, not from a threshold
(ADR-0057; ``pipeline_repo._prune_reparsed_modules``). No database: a fake
writer stands in for Neo4j, the module lives in a real temp git repository.

Business rules:

* what git proves gone is deleted - however much of the module that is (no
  50 % / 20 gate, no ``--allow-mass-retire``, no exit 3, no attention);
* what the HEAD source still defines but the parse no longer produces is kept
  with its embedding rows and recorded once on the Module node (parser gap),
  without an operator signal and without a nightly retry;
* a recorded parser gap is re-evaluated only when the module's tree or the
  indexer code changes;
* the test-node GC leaves the modules the prune judged alone (so the census is
  what the prune decides, and nothing leaves without its embeddings).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from src.indexer import pipeline_repo, prune_evidence
from src.indexer.models import ModuleInfo
from src.indexer.parse_health import ModuleParseHealth

V = "17.0"
MOD = "viin_freight_patch"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _repo(tmp_path: Path, models_py: str) -> Path:
    root = tmp_path / "repo"
    (root / MOD / "models").mkdir(parents=True)
    (root / MOD / "__manifest__.py").write_text(repr({"name": "P", "depends": ["sale"]}))
    (root / MOD / "models" / "line.py").write_text(models_py)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _commit(root: Path, models_py: str) -> None:
    (root / MOD / "models" / "line.py").write_text(models_py)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "change")


class FakePresence:
    def names_owned_elsewhere(self, version, mods, repo_id):
        return set()

    def potential_owners_unsynced(self, name, version, **kwargs):
        return []


class FakeWriter:
    """Neo4j stand-in: a module's indexed children and which the run wrote."""

    run_started_at = None

    def __init__(self, nodes: list[dict], *, written: set[str]) -> None:
        self.nodes = {n["id"]: n for n in nodes}
        self.written = set(written)  # ids the current run re-wrote
        self.gaps: dict = {}
        self.calls: list = []

    def _stale(self):
        return [n for i, n in self.nodes.items() if i not in self.written]

    def module_children_census(self, v, name, **kw):
        return {"module_exists": True, "stale": len(self._stale()), "total": len(self.nodes),
                "rels_stale": 0, "rels_total": 0}

    def module_stale_children(self, v, name, **kw):
        return {"nodes": [dict(n) for n in self._stale()], "rels": []}

    def prune_module_children(self, v, name, *, only_ids=None, only_rel_ids=None, **kw):
        self.calls.append(("prune", sorted(only_ids or [])))
        for i in only_ids or []:
            self.nodes.pop(i, None)
        return {"deleted": len(only_ids or []), "by_label": {}, "rels_deleted": 0,
                "rels_by_label": {}}

    def record_module_parser_gap(self, v, name, **kw):
        self.gaps[(v, name)] = kw

    def clear_module_parser_gap(self, v, names):
        for n in names:
            self.gaps.pop((v, n), None)
        return 0

    def parser_gap_modules(self, repo_id):
        return [
            {"name": n, "odoo_version": v, "fingerprint": kw["fingerprint"],
             "paths": kw["paths"]}
            for (v, n), kw in sorted(self.gaps.items())
        ]


def _field(fid: str, name: str, model: str = "sale.order.line") -> dict:
    return {"id": fid, "label": "Field", "name": name, "model": model}


def _prune(writer, root: Path, *, pg_conn=None):
    info = ModuleInfo(name=MOD, odoo_version=V, repo="repo", path=str(root / MOD),
                      depends=["sale"], repo_root=root)
    health = ModuleParseHealth(module=MOD, odoo_version=V)
    health.embedded_keys = {("field", "sale.order.line.kept", f"{MOD}/models/line.py", 0)}
    health.embeddings_written = True
    return pipeline_repo._prune_reparsed_modules(
        writer, FakePresence(), {"id": 31, "url": "repo"},
        modules_by_version={V: {MOD: info}}, module_health={(V, MOD): health},
        run_id="run-1", owning_profile="viindoo", repo_path=root, pg_conn=pg_conn,
        lifecycle_on=True, retire=True, scan_ok=True,
    )


_LINE_ONLY = "class L(models.Model):\n    _inherit = 'sale.order.line'\n    kept = fields.Char()\n"


def test_a_git_proven_mass_deletion_is_pruned_with_no_gate_and_no_operator_signal(tmp_path):
    """freight patch1: a commit guts the module to one model + one field. 30 of
    31 nodes are stale (far above the old 50 %/20 gate): all are deleted on the
    first plain run, nothing is held, no gate id, no attention, no retry."""
    root = _repo(tmp_path, _LINE_ONLY + "".join(
        f"    f{i:02d} = fields.Char()\n" for i in range(30)))
    _commit(root, _LINE_ONLY)
    nodes = [_field("kept", "kept")] + [_field(f"f{i:02d}", f"f{i:02d}") for i in range(30)]
    writer = FakeWriter(nodes, written={"kept"})

    report = _prune(writer, root)

    assert sorted(writer.nodes) == ["kept"]
    assert report["gates_tripped"] == [] and report["attention"] == []
    assert report["retry"] == [] and report["skipped"] == {}
    assert report["parser_gaps"] == {} and writer.gaps == {}
    assert report["pruned"][(V, MOD)]["kept"] == 0


def test_entities_the_source_still_defines_are_kept_and_recorded_once(tmp_path):
    """viin_ai: the parser lost a model whose ``_name`` became a constant, while
    a commit deleted other fields. Partial prune: the deleted ones go, the
    still-defined one stays and is recorded as a parser gap - not a gate, not
    an attention line, not a needs_rewrite retry."""
    src = _LINE_ONLY + "    still_here = fields.Char()\n"
    root = _repo(tmp_path, src + "    dropped = fields.Char()\n")
    _commit(root, src)
    nodes = [_field("kept", "kept"), _field("still", "still_here"), _field("dropped", "dropped")]
    writer = FakeWriter(nodes, written={"kept"})

    report = _prune(writer, root)

    assert sorted(writer.nodes) == ["kept", "still"]
    assert writer.calls == [("prune", ["dropped"])]
    assert report["gates_tripped"] == [] and report["attention"] == [] and report["retry"] == []
    gap = writer.gaps[(V, MOD)]
    assert (gap["kept"], gap["total"]) == (1, 3)
    assert gap["sample"] == ["Field still_here (sale.order.line)"]
    assert gap["paths"] == [MOD]
    assert report["parser_gaps"][(V, MOD)]["kept"] == 1


def test_a_later_prune_that_keeps_nothing_clears_the_parser_gap_record(tmp_path):
    src = _LINE_ONLY + "    still_here = fields.Char()\n"
    root = _repo(tmp_path, src)
    writer = FakeWriter([_field("kept", "kept"), _field("still", "still_here")],
                        written={"kept"})
    _prune(writer, root)
    assert (V, MOD) in writer.gaps

    _commit(root, _LINE_ONLY)  # the field is really removed now
    _prune(writer, root)

    assert writer.gaps == {} and sorted(writer.nodes) == ["kept"]


def test_the_rows_of_kept_entities_survive_the_embedding_reconcile(tmp_path, monkeypatch):
    src = _LINE_ONLY + "    still_here = fields.Char()\n"
    root = _repo(tmp_path, src + "    dropped = fields.Char()\n")
    _commit(root, src)
    writer = FakeWriter([_field("kept", "kept"), _field("still", "still_here"),
                         _field("dropped", "dropped")], written={"kept"})
    seen: list = []

    def fake_delete(conn, module, version, profile, keep_keys, **kw):
        seen.append(sorted(kw.get("also_keep") or []))
        return 0

    monkeypatch.setattr("src.indexer.writer_pgvector.delete_module_embeddings_except",
                        fake_delete)

    class Conn:
        autocommit = True

    _prune(writer, root, pg_conn=Conn())

    assert seen == [[("field", "sale.order.line.still_here", None)]]


def test_a_parser_gap_is_re_evaluated_only_when_the_module_or_the_code_changes(
    tmp_path, monkeypatch,
):
    src = _LINE_ONLY + "    still_here = fields.Char()\n"
    root = _repo(tmp_path, src)
    writer = FakeWriter([_field("kept", "kept"), _field("still", "still_here")],
                        written={"kept"})
    _prune(writer, root)
    repo = {"id": 31}

    assert pipeline_repo._parser_gap_state(writer, repo, root)[1] == set()

    (root / "README").write_text("outside the module\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "unrelated")
    assert pipeline_repo._parser_gap_state(writer, repo, root)[1] == set()

    monkeypatch.setattr(prune_evidence, "_CODE_TOKEN", "src-deployedparserfix")
    assert pipeline_repo._parser_gap_state(writer, repo, root)[1] == {MOD}
    monkeypatch.undo()

    _commit(root, src + "    # touched\n")
    assert pipeline_repo._parser_gap_state(writer, repo, root)[1] == {MOD}


def test_an_unchanged_parser_gap_does_not_make_the_repo_leave_the_unchanged_skip(tmp_path):
    """No nightly loop: with nothing changed the plan stays RUN_SKIP."""
    plan = pipeline_repo.plan_repo_run(
        tmp_path, "abc", "abc", "abc", [], full_reindex=False, ledger=True,
        degraded_changed=False,
    )
    assert plan.mode == pipeline_repo.RUN_SKIP


def test_a_name_pruned_at_one_version_and_shared_at_another_is_judged_per_version(tmp_path):
    """A repo may scan one module name at two versions in a run. The prune
    judged it at 17.0 but skipped it at 18.0 (another live repo owns it
    there): the test GC must still cover the 18.0 copy, whose test nodes the
    prune did not judge."""
    root = _repo(tmp_path, _LINE_ONLY)
    other = "18.0"

    class SharedAt18(FakePresence):
        def names_owned_elsewhere(self, version, mods, repo_id):
            return {MOD} if version == other else set()

    info = {v: ModuleInfo(name=MOD, odoo_version=v, repo="repo", path=str(root / MOD),
                          depends=["sale"], repo_root=root) for v in (V, other)}
    health = {}
    for v in (V, other):
        h = ModuleParseHealth(module=MOD, odoo_version=v)
        h.embeddings_written = True
        health[(v, MOD)] = h
    writer = FakeWriter([_field("kept", "kept")], written={"kept"})
    modules = {V: {MOD: info[V]}, other: {MOD: info[other]}}

    report = pipeline_repo._prune_reparsed_modules(
        writer, SharedAt18(), {"id": 31, "url": "repo"},
        modules_by_version=modules, module_health=health,
        run_id="run-1", owning_profile="viindoo", repo_path=root, pg_conn=None,
        lifecycle_on=True, retire=True, scan_ok=True,
    )

    assert set(report["pruned"]) == {(V, MOD)}
    assert report["skipped"] == {(other, MOD): pipeline_repo.PRUNE_SKIP_SHARED}
    assert pipeline_repo._test_gc_judged(modules, report, health) == {(V, MOD)}


def test_the_soft_gate_is_gone_from_the_retry_reasons():
    assert "soft_gate" not in pipeline_repo._PRUNE_RETRY_REASONS
    assert not hasattr(pipeline_repo, "PRUNE_SKIP_SOFT_GATE")


def test_the_test_node_gc_leaves_the_modules_the_prune_judged_alone():
    """The file-level test GC must not delete (without embeddings) the test
    nodes of a module the oracle-gated prune already judged, nor of a module
    whose parse was degraded; it still covers the other re-parsed modules."""
    class GcWriter:
        def __init__(self):
            self.calls = []

        def gc_stale_test_nodes(self, ver, live, **kw):
            self.calls.append((ver, kw["live_modules_for_file_gc"]))
            return 0

    class Mod:
        def __init__(self, name):
            self.name, self.odoo_version = name, V

    class Result:
        def __init__(self, name):
            self.module = Mod(name)
            self.test_classes = []

    writer = GcWriter()
    pipeline_repo._gc_stale_test_nodes(
        writer, {V: ["judged", "shared_one"]}, [Result("judged"), Result("shared_one")],
        "repo", judged={(V, "judged")},
    )
    assert writer.calls == [(V, ["shared_one"])]
