# SPDX-License-Identifier: AGPL-3.0-or-later
"""A module two repos ship loses what NEITHER copy defines any more (F49, B14).

Graph children carry no repo, so one copy's re-parse can never prune a module
another present repo also ships (B14 rule c). Before F49 nothing else removed an
entity both copies had dropped: it stayed in Neo4j and in every owner profile's
embeddings forever, so ``validate_domain`` / ``find_examples`` kept serving a
field no copy of the module has (product rule "never blur data for the AI").
The version reconcile of every normal run now decides it from the ledger's
complete-parse records of the module's present owners. Rules protected here
(finalfix contract F49 + round 2):

1. both copies drop an entity (any order, plain incremental runs, no --full):
   after the run in which the last owner re-parsed, it leaves the graph and its
   embedding rows of every chunk kind leave every owner profile; what at least
   one copy still defines stays with its embeddings, and so does the extender's
   same-name INHERITS edge;
2. dropped from only ONE copy: it stays (graph + embeddings);
3. an owner whose latest parse predates a child no owner's latest parse accounts
   for is flagged ``needs_rewrite`` (the other owner is not) and its next run,
   with no commit, re-parses and the residue goes; owners that each wrote their
   own extra entity never ping-pong (0-work runs);
4. a degraded parse of any copy, or an unsynced repo that may ship the module,
   blocks the rule (nothing deleted) until it is resolved;
5. a mass drop (> 50 % and >= 20 nodes) is held with gate ``entity_prune:M@v``
   (operator attention) on every run until one runs with --allow-mass-retire;
6. the dry-run ``lifecycle-audit`` predicts ``would_prune`` / ``held`` as the
   finding ``shared_prunes``; after the prune it is 0 and the next run reports
   nothing (watermark);
7. after a deploy (no complete-parse records yet) each repo's normal runs
   re-parse at most ``SHARED_PARSE_BOOTSTRAP_PER_RUN`` shared copies at an
   unchanged HEAD; the audit backlog counts down to 0 and the residue goes
   once every owner recorded. A copy without a record blocks its module.

Every test drives the real ``index_profile`` (and the real CLI) over temp git
repos with a bare origin, against real Neo4j and PostgreSQL + pgvector.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import constants
from src.db.migrate import _vector_extension_available, run_migrations
from tests import _retirement_fixture as rf
from tests._lifecycle_repo import (
    GitRepo,
    V,
    children,
    lc,
    ledger,
    needs_attention,
    register,
    run,
    write_module,
)
from tests.test_entity_prune_pipeline import (
    _RAG_MODELS_NO_RAG_COUNT,
    _REMOVED_EMBEDDINGS,
    _REMOVED_NODES,
    _SURVIVING_NODES,
    _count,
    _emb,
    _nodes,
    _rag_models,
    _rag_repo,
    _remove_rag_entities,
    _write,
)

pytestmark = [pytest.mark.postgres, pytest.mark.neo4j]

RAG = rf.RETIRED  # "viin_ai_rag" - a LIVE module both repos ship
AI = rf.SURVIVOR  # "viin_ai"
PA, PB, PC = "pa_99", "pb_99", "pc_99"
NOTE = "rag_note"
_BUDGET_ENV = "OSM_SHARED_PARSE_BOOTSTRAP_PER_RUN"


@pytest.fixture
def pg(clean_pg, clean_neo4j):
    """Migrated PG (with pgvector) + clean Neo4j; the pool comes from pg_conn."""
    run_migrations(clean_pg)
    if not _vector_extension_available(clean_pg):
        pytest.skip("pgvector extension not installed - embeddings cannot be checked")
    return clean_pg


# ---------------------------------------------------------------------------
# Builders + observations
# ---------------------------------------------------------------------------

def _with_own_field(models: str, field: str) -> str:
    """The rag models text plus a field only this copy defines (on ai.rag.source)."""
    return models.replace(
        "    name = fields.Char()\n", f"    name = fields.Char()\n    {field} = fields.Char()\n", 1,
    )


def _copy(parent: Path, name: str, own_field: str) -> GitRepo:
    repo = _rag_repo(parent, name)
    _rag_models(repo, _with_own_field(rf._RAG_MODELS, own_field))
    repo.commit(f"{own_field}: this copy's own field")
    return repo


def _drop_everything_removable(repo: GitRepo, own_field: str) -> None:
    """The split residue (one artifact of every kind leaves), own field kept."""
    _remove_rag_entities(repo)
    _rag_models(
        repo,
        _with_own_field(
            _RAG_MODELS_NO_RAG_COUNT.replace(
                "\n    def action_reindex(self):\n        return True\n", "\n",
            ),
            own_field,
        ),
    )
    repo.commit(f"[REM] {RAG}: keep {own_field}")


_PER_REPO_TEST_LABELS = {"TestClass", "TestMethod"}


def _test_class_repos(driver, name: str) -> list[str]:
    with driver.session() as s:
        rows = s.run(
            "MATCH (t:TestClass {name: $n, module: $m, odoo_version: $v}) RETURN t.repo AS r",
            n=name, m=RAG, v=V,
        ).data()
    return sorted(str(r["r"]) for r in rows)


def _node_counts(driver, specs) -> dict:
    return {(label, tuple(sorted(p.items()))): _count(driver, label, **p) for label, p in specs}


def _field(driver, name: str, module: str = RAG) -> int:
    return _count(driver, "Field", module=module, name=name)


def _field_emb(pg_conn, module: str, profile: str, entity_suffix: str) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM embeddings WHERE odoo_version = %s AND module = %s "
            "AND profile_name = %s AND chunk_type = 'field' AND entity_name LIKE %s",
            (V, module, profile, f"%.{entity_suffix}"),
        )
        return cur.fetchone()[0]


def _same_name_inherits(driver) -> int:
    """Edges from viin_ai_rag's ai.assistant extender to viin_ai's definition."""
    with driver.session() as s:
        return s.run(
            """
            MATCH (x:Model {name: 'ai.assistant', module: $rag, odoo_version: $v})
                  -[r:INHERITS]->(d:Model {name: 'ai.assistant', module: $ai, odoo_version: $v})
            RETURN count(r) AS n
            """,
            rag=RAG, ai=AI, v=V,
        ).single()["n"]


def _audit(monkeypatch, capsys, *argv: str) -> tuple[int, dict]:
    from src.indexer.__main__ import main
    from tests import conftest

    monkeypatch.setenv("PG_DSN", conftest.get_test_dsn())
    capsys.readouterr()
    code = main(["lifecycle-audit", *argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def _version(report: dict) -> dict:
    [ver] = [v for v in report["versions"] if v["odoo_version"] == V]
    return ver


def _repo_entry(report: dict, basename: str) -> dict:
    [entry] = [r for r in report["repos"] if r["basename"] == basename]
    return entry


def _shared_reports(summary: dict, key: str) -> dict:
    """Union of reconcile report *key* (a dict) across the run's versions."""
    out: dict = {}
    for rep in lc(summary).get("reports", []):
        out.update(rep.get(key) or {})
    return out


def _simple_module(repo: GitRepo, name: str, fields_text: str) -> None:
    write_module(repo, name, model=f"{name.replace('_', '.')}.record", extra_field=fields_text)


# ---------------------------------------------------------------------------
# Rule 1 + 2 - both copies drop it: gone everywhere; one copy only: kept
# ---------------------------------------------------------------------------

def test_entities_both_copies_dropped_leave_graph_and_every_profiles_embeddings(
    pg, neo4j_driver, tmp_path,
):
    """viin_ai_rag ships from addons_a (profile pa_99, own field only_in_a) and
    addons_b (pb_99, own field only_in_b). copy a drops one artifact of every
    kind (field, methods, view + lint violation, report + template, JS patch,
    OWL component, stylesheet, JS test suite, test class/method): copy b still
    ships them, so NOTHING leaves. Then copy b drops the same set: its next
    plain run removes every such node and the embedding rows of every chunk kind
    from BOTH profiles, and keeps each copy's own field, every entity both
    still define (with their rows), the same-name INHERITS edge of the surviving
    extender model and every node of viin_ai. Later runs do zero work."""
    a = _copy(tmp_path / "a", "addons_a", "only_in_a")
    b = _copy(tmp_path / "b", "addons_b", "only_in_b")
    register(PA, a)
    register(PB, b)
    run(pg, PA)
    run(pg, PB)
    removed_before = _node_counts(neo4j_driver, _REMOVED_NODES)
    assert all(removed_before.values()), f"precondition: every artifact indexed {removed_before}"
    assert _test_class_repos(neo4j_driver, "TestRagSource") == ["addons_a", "addons_b"]
    for profile in (PA, PB):
        assert _REMOVED_EMBEDDINGS <= _emb(pg, RAG, profile), (
            f"precondition: every removable chunk kind embedded in {profile}"
        )
    inherits = _same_name_inherits(neo4j_driver)
    assert inherits >= 1, "precondition: the extender's same-name INHERITS edge exists"
    ai_nodes = _nodes(neo4j_driver, AI)

    _drop_everything_removable(a, "only_in_a")
    run(pg, PA)

    # Test nodes are per repo (each copy has its own TestClass / TestMethod):
    # copy a's leave with copy a's tests; copy b's stay. Every other entity is
    # module-scoped and copy b still ships it.
    expected = {
        k: (n - 1 if k[0] in _PER_REPO_TEST_LABELS else n) for k, n in removed_before.items()
    }
    assert _node_counts(neo4j_driver, _REMOVED_NODES) == expected, (
        "dropped from one copy only: the other copy still ships them"
    )
    assert _test_class_repos(neo4j_driver, "TestRagSource") == ["addons_b"]
    for profile in (PA, PB):
        assert _REMOVED_EMBEDDINGS <= _emb(pg, RAG, profile), profile

    _drop_everything_removable(b, "only_in_b")
    run(pg, PB)

    left = {k: n for k, n in _node_counts(neo4j_driver, _REMOVED_NODES).items() if n}
    assert left == {}, f"entities no copy defines are still in the graph: {left}"
    for profile in (PA, PB):
        stale = _REMOVED_EMBEDDINGS & _emb(pg, RAG, profile)
        assert stale == set(), f"{profile} kept embeddings no copy produces: {stale}"
    kept = _node_counts(neo4j_driver, _SURVIVING_NODES)
    assert all(kept.values()), f"an entity both copies define was pruned: {kept}"
    assert _field(neo4j_driver, "only_in_a") == 1 and _field(neo4j_driver, "only_in_b") == 1
    assert ("field", "ai.rag.source.only_in_a") in _emb(pg, RAG, PA)
    assert ("field", "ai.rag.source.only_in_b") in _emb(pg, RAG, PB)
    for profile in (PA, PB):
        assert ("field", "ai.rag.source.name") in _emb(pg, RAG, profile), profile
    assert _same_name_inherits(neo4j_driver) == inherits
    assert _nodes(neo4j_driver, AI) == ai_nodes

    for profile in (PA, PB):
        again = run(pg, profile)
        assert again["modules"] == 0, (profile, again)
        assert _shared_reports(again, "shared_pruned") == {}, profile


# ---------------------------------------------------------------------------
# Rule 3 - owners whose parse predates the residue are sent back (no --full)
# ---------------------------------------------------------------------------

def test_owner_older_than_the_residue_is_sent_back_and_its_next_run_prunes_it(
    pg, neo4j_driver, tmp_path,
):
    """addons_b ships viin_ai_rag WITHOUT rag_note and is indexed first; addons_a
    ships it WITH rag_note, then drops it. b's complete parse predates rag_note,
    so its record cannot prove b lacks it: rag_note stays, and b - not a - is
    flagged needs_rewrite for viin_ai_rag. b's next run, with no new commit,
    re-parses the module and rag_note leaves the graph and every profile's
    embeddings; afterwards both owners' runs do zero work."""
    b = GitRepo(tmp_path / "b", "addons_b")
    _simple_module(b, RAG, "")
    b.commit("copy b")
    a = GitRepo(tmp_path / "a", "addons_a")
    _simple_module(a, RAG, f"{NOTE} = fields.Text()")
    a.commit("copy a with rag_note")
    (rid_a,) = register(PA, a)
    (rid_b,) = register(PB, b)
    run(pg, PB)
    run(pg, PA)
    assert _field(neo4j_driver, NOTE) == 1
    _simple_module(a, RAG, "")
    a.commit(f"[REM] {RAG}: drop {NOTE}")

    flagged = run(pg, PA)

    assert _field(neo4j_driver, NOTE) == 1, "b's record predates rag_note: not decidable yet"
    assert ledger(pg, rid_b, RAG)["needs_rewrite"] is True, "b must re-parse the module"
    assert ledger(pg, rid_a, RAG)["needs_rewrite"] is False, "a's latest parse is current"
    rewrites = _shared_reports(flagged, "shared_prune_rewrites")
    assert list(rewrites) == [RAG], rewrites
    assert any("addons_b" in label for label in rewrites[RAG]), rewrites
    assert not any("addons_a" in label for label in rewrites[RAG]), rewrites

    resolved = run(pg, PB)

    assert resolved["modules"] >= 1, "the flagged owner must re-parse without a commit"
    assert _field(neo4j_driver, NOTE) == 0
    for profile in (PA, PB):
        assert _field_emb(pg, RAG, profile, NOTE) == 0, profile
        assert _field_emb(pg, RAG, profile, "label") == 1, profile
    assert ledger(pg, rid_b, RAG)["needs_rewrite"] is False
    for profile in (PA, PB, PA, PB):
        assert run(pg, profile)["modules"] == 0, profile


def test_owners_with_different_extra_fields_converge_to_zero_work_runs(
    pg, neo4j_driver, tmp_path,
):
    """Each copy of viin_ai_rag defines a field the other lacks. Each owner's
    latest parse accounts for its own field, so neither is ever flagged: the
    runs after the first ones do zero work, nothing is pruned, and both fields
    keep their embeddings (no ping-pong between the owners)."""
    # GUARD: convergence invariant of the F49 self-driving rule (db0e15b). Before
    # db0e15b no owner was ever flagged, so this cannot fail on its revert; it
    # fails when the unattributed-child check flags an owner for a child its own
    # latest parse wrote.
    a = GitRepo(tmp_path / "a", "addons_a")
    _simple_module(a, RAG, "only_a = fields.Char()")
    a.commit("copy a")
    b = GitRepo(tmp_path / "b", "addons_b")
    _simple_module(b, RAG, "only_b = fields.Char()")
    b.commit("copy b")
    (rid_a,) = register(PA, a)
    (rid_b,) = register(PB, b)
    run(pg, PA)
    run(pg, PB)

    for profile in (PA, PB, PA, PB, PA):
        summary = run(pg, profile)
        assert summary["modules"] == 0, (profile, summary)
        assert _shared_reports(summary, "shared_prune_rewrites") == {}, profile
    assert ledger(pg, rid_a, RAG)["needs_rewrite"] is False
    assert ledger(pg, rid_b, RAG)["needs_rewrite"] is False
    assert _field(neo4j_driver, "only_a") == 1 and _field(neo4j_driver, "only_b") == 1
    assert _field_emb(pg, RAG, PA, "only_a") == 1
    assert _field_emb(pg, RAG, PB, "only_b") == 1


# ---------------------------------------------------------------------------
# Rule 4 - degraded copy / unsynced potential owner block
# ---------------------------------------------------------------------------

def _two_copies_with_note(tmp_path: Path) -> tuple[GitRepo, GitRepo, int, int]:
    a = GitRepo(tmp_path / "a", "addons_a")
    b = GitRepo(tmp_path / "b", "addons_b")
    for repo in (a, b):
        _simple_module(repo, RAG, f"{NOTE} = fields.Text()")
        repo.commit("copy with rag_note")
    (rid_a,) = register(PA, a)
    (rid_b,) = register(PB, b)
    return a, b, rid_a, rid_b


def test_degraded_parse_of_one_copy_blocks_the_shared_prune_until_fixed(
    pg, neo4j_driver, tmp_path,
):
    """Both copies drop rag_note, but copy b's commit also adds a Python file
    with a syntax error: b's parse is degraded, so b's record cannot say what b
    ships - rag_note and its embeddings stay. The commit that fixes b's file
    re-parses it and rag_note goes."""
    a, b, _rid_a, _rid_b = _two_copies_with_note(tmp_path)
    run(pg, PA)
    run(pg, PB)
    _simple_module(a, RAG, "")
    a.commit(f"[REM] {RAG}: drop {NOTE}")
    run(pg, PA)
    _simple_module(b, RAG, "")
    _write(b.path / RAG / "models" / "broken.py", "def broken(:\n    pass\n")
    _write(b.path / RAG / "models" / "__init__.py", "from . import record\nfrom . import broken\n")
    b.commit(f"[WIP] {RAG}: drop {NOTE}, half-done file")

    run(pg, PB)
    run(pg, PA)

    assert _field(neo4j_driver, NOTE) == 1, "a degraded owner's copy must block the prune"
    assert _field_emb(pg, RAG, PA, NOTE) == 1 and _field_emb(pg, RAG, PB, NOTE) == 1

    (b.path / RAG / "models" / "broken.py").unlink()
    _write(b.path / RAG / "models" / "__init__.py", "from . import record\n")
    b.commit(f"[FIX] {RAG}: finish")
    run(pg, PB)

    assert _field(neo4j_driver, NOTE) == 0
    assert _field_emb(pg, RAG, PA, NOTE) == 0 and _field_emb(pg, RAG, PB, NOTE) == 0
    assert _field(neo4j_driver, "label") == 1


def test_unsynced_repo_that_may_ship_the_module_holds_the_shared_prune(
    pg, neo4j_driver, tmp_path, monkeypatch, capsys,
):
    """A third repo (profile pc_99), cloned but never indexed, tracks its own
    copy of viin_ai_rag: it may still define rag_note. Both indexed copies drop
    rag_note; the prune waits (the audit names the waiting repo) and deletes
    nothing. The third repo's first run (its copy has no rag_note) completes the
    owner set and rag_note goes."""
    a, b, _rid_a, _rid_b = _two_copies_with_note(tmp_path)
    c = GitRepo(tmp_path / "c", "addons_c")
    _simple_module(c, RAG, "")
    c.commit("copy c, never indexed yet")
    register(PC, c)
    run(pg, PA)
    run(pg, PB)
    for repo, profile in ((a, PA), (b, PB)):
        _simple_module(repo, RAG, "")
        repo.commit(f"[REM] {RAG}: drop {NOTE}")
        summary = run(pg, profile)

    assert _field(neo4j_driver, NOTE) == 1, "an unsynced potential owner must hold the prune"
    assert _field_emb(pg, RAG, PA, NOTE) == 1
    waiting = _shared_reports(summary, "shared_prune_waiting")
    assert list(waiting) == [RAG], lc(summary)["reports"]
    assert any("addons_c" in w for w in waiting[RAG]), waiting
    # The owners' audit (pc_99 not simulated, so addons_c stays unsynced) predicts
    # the same wait and no prune.
    _code, report = _audit(monkeypatch, capsys, "--profile", PA)
    assert list(_version(report)["shared_prune_waiting"]) == [RAG]
    assert _version(report)["shared_prunes"] == []

    run(pg, PC)

    assert _field(neo4j_driver, NOTE) == 0
    for profile in (PA, PB, PC):
        assert _field_emb(pg, RAG, profile, NOTE) == 0, profile


# ---------------------------------------------------------------------------
# Rule 5 - mass drop held until --allow-mass-retire
# ---------------------------------------------------------------------------

def _bulk_fields(n: int) -> str:
    # One field per line at the class-body indent of the (not yet dedented)
    # _lifecycle_repo._model_py template.
    return ("\n" + " " * 12).join(f"f{i:02d} = fields.Char()" for i in range(n))


BULK = "viin_ai_bulk"


def test_shared_mass_drop_is_held_with_a_gate_until_allow_mass_retire(
    pg, neo4j_driver, tmp_path, monkeypatch, capsys,
):
    """Both copies of viin_ai_bulk drop 26 of their 29 extra fields (> 50 % and
    >= 20 of the module's nodes). The run that could prune holds instead: gate
    ``entity_prune:viin_ai_bulk@99.0``, operator attention, nothing deleted; the
    audit reports it ``held``; a later plain run holds again. A run with
    --allow-mass-retire prunes exactly the 26 fields and the audit is clean."""
    a = GitRepo(tmp_path / "a", "addons_a")
    b = GitRepo(tmp_path / "b", "addons_b")
    for repo in (a, b):
        _simple_module(repo, BULK, _bulk_fields(29))
        repo.commit("bulk")
    register(PA, a)
    register(PB, b)
    run(pg, PA)
    run(pg, PB)
    total = children(neo4j_driver, BULK)
    assert total >= 31, "precondition: model, 30 fields and a method"
    for repo in (a, b):
        _simple_module(repo, BULK, _bulk_fields(3))
        repo.commit("[REF] bulk: drop 26 fields")
    run(pg, PA)

    held = run(pg, PB)

    gate = f"entity_prune:{BULK}@{V}"
    assert children(neo4j_driver, BULK) == total, "a mass drop must be held"
    assert any(gate in g for g in lc(held)["gates_tripped"]), lc(held)["gates_tripped"]
    assert needs_attention(held)
    assert list(_shared_reports(held, "shared_prune_held")) == [BULK]
    code, report = _audit(monkeypatch, capsys, "--all", "--fail-on-findings")
    items = [(i["name"], i["outcome"], i["stale"]) for i in _version(report)["shared_prunes"]]
    assert items == [(BULK, "held", 26)], items
    assert report["findings"]["shared_prunes"] == 1 and code == 4

    again = run(pg, PA)
    assert children(neo4j_driver, BULK) == total, "held again: re-evaluated every run"
    assert any(gate in g for g in lc(again)["gates_tripped"])

    run(pg, PB, allow_mass_retire=True)

    assert children(neo4j_driver, BULK) == total - 26
    assert [_field(neo4j_driver, f"f{i:02d}", BULK) for i in (0, 1, 2)] == [1, 1, 1]
    assert _field(neo4j_driver, "f03", BULK) == 0
    code, report = _audit(monkeypatch, capsys, "--all", "--fail-on-findings")
    assert _version(report)["shared_prunes"] == []
    assert report["findings"]["shared_prunes"] == 0
    assert not needs_attention(run(pg, PA))


# ---------------------------------------------------------------------------
# Rule 6 - the audit predicts the prune the next run does
# ---------------------------------------------------------------------------

def test_audit_predicts_the_shared_prune_and_is_clean_after_the_run_that_does_it(
    pg, neo4j_driver, tmp_path, monkeypatch, capsys,
):
    """Both copies dropped rag_note; the run that re-parsed the last copy ran
    with --no-retire (deletes nothing, so that copy is re-parsed by its next
    run). The dry-run audit predicts ``would_prune`` of exactly one node
    (rag_note) as a ``shared_prunes`` finding naming both owners, deleting
    nothing itself; the next plain run of that copy - no commit - prunes it;
    the audit is then clean and the runs after report no shared prune
    (watermark)."""
    a, b, _rid_a, _rid_b = _two_copies_with_note(tmp_path)
    run(pg, PA)
    run(pg, PB)
    _simple_module(a, RAG, "")
    a.commit(f"[REM] {RAG}: drop {NOTE}")
    run(pg, PA)
    _simple_module(b, RAG, "")
    b.commit(f"[REM] {RAG}: drop {NOTE}")
    run(pg, PB, retire=False)
    assert _field(neo4j_driver, NOTE) == 1, "precondition: --no-retire deleted nothing"

    code, report = _audit(monkeypatch, capsys, "--all", "--fail-on-findings")

    items = _version(report).get("shared_prunes", [])
    assert len(items) == 1, _version(report)
    [item] = items
    assert (item["name"], item["outcome"], item["stale"], item["rels_stale"]) == (
        RAG, "would_prune", 1, 0,
    ), item
    assert len(item["owners"]) == 2, item
    assert {"total", "rels_total", "cutoff", "owners"} <= set(item)
    assert report["findings"]["shared_prunes"] == 1 and code == 4
    assert _field(neo4j_driver, NOTE) == 1, "the audit is a dry run"

    pruned = run(pg, PB)

    assert list(_shared_reports(pruned, "shared_pruned")) == [RAG], lc(pruned)["reports"]
    assert _field(neo4j_driver, NOTE) == 0
    assert _field_emb(pg, RAG, PA, NOTE) == 0 and _field_emb(pg, RAG, PB, NOTE) == 0
    code, report = _audit(monkeypatch, capsys, "--all", "--fail-on-findings")
    assert _version(report)["shared_prunes"] == []
    assert report["findings"]["shared_prunes"] == 0 and code == 0, report["findings"]
    for profile in (PA, PB):
        quiet = run(pg, profile)
        assert quiet["modules"] == 0 and _shared_reports(quiet, "shared_pruned") == {}, profile


# ---------------------------------------------------------------------------
# Rule 7 - post-deploy bootstrap drains within its per-run budget
# ---------------------------------------------------------------------------

def _forget_full_parses(pg_conn) -> None:
    """The ledger right after the deploy: no complete-parse record for any row.

    Only the record columns the schema has are reset, so on a tree without
    some of them the test still reaches its behaviour assertions."""
    record = {
        "last_full_parse_at": "NULL", "last_full_parse_sha": "NULL",
        "last_full_parse_unobserved": "'{}'", "last_full_parse_embedded_at": "NULL",
        "last_full_parse_run": "NULL",
    }
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'module_presence' AND column_name = ANY(%s)",
            (list(record),),
        )
        present = sorted(r[0] for r in cur.fetchall())
        if present:
            cur.execute(
                "UPDATE module_presence SET "
                + ", ".join(f"{c} = {record[c]}" for c in present)
            )


SH1, SH2 = "viin_shared_one", "viin_shared_two"


def test_post_deploy_backlog_drains_one_module_per_run_and_the_residue_goes(
    pg, neo4j_driver, tmp_path, monkeypatch, capsys,
):
    """Two repos ship viin_shared_one and viin_shared_two; both copies of both
    dropped rag_note under the pre-deploy code, which kept it (residue, no
    complete-parse record anywhere). With a budget of 1 per run and no commit:
    the audit reports each repo's backlog as 2 with 1 in the next run; each run
    re-parses exactly one shared copy; a module goes only when its second owner
    recorded (one recorded copy is not enough); after both modules drained the
    runs do zero work, the residue is gone from the graph and every profile,
    and the backlog is 0."""
    a = GitRepo(tmp_path / "a", "addons_a")
    b = GitRepo(tmp_path / "b", "addons_b")
    for repo in (a, b):
        for name in (SH1, SH2):
            _simple_module(repo, name, f"{NOTE} = fields.Text()")
        repo.commit("two shared modules with rag_note")
    register(PA, a)
    register(PB, b)
    monkeypatch.delenv(_BUDGET_ENV, raising=False)  # the default decides here
    # Pre-deploy code: no bootstrap and no complete-parse records.
    monkeypatch.setattr(constants, "SHARED_PARSE_BOOTSTRAP_PER_RUN", 0, raising=False)
    for repo, profile in ((a, PA), (b, PB)):
        run(pg, profile)
        _forget_full_parses(pg)
    for repo, profile in ((a, PA), (b, PB)):
        for name in (SH1, SH2):
            _simple_module(repo, name, "")
        repo.commit(f"[REM] drop {NOTE}")
        run(pg, profile)
        _forget_full_parses(pg)
    assert [_field(neo4j_driver, NOTE, m) for m in (SH1, SH2)] == [1, 1], (
        "precondition: the pre-deploy runs left the residue"
    )

    monkeypatch.setattr(constants, "SHARED_PARSE_BOOTSTRAP_PER_RUN", 1, raising=False)
    _code, report = _audit(monkeypatch, capsys, "--all")
    for basename in ("addons_a", "addons_b"):
        backlog = _repo_entry(report, basename).get("shared_parse_backlog") or {}
        assert (backlog.get("remaining"), len(backlog.get("next_run") or [])) == (2, 1), (
            basename, backlog,
        )

    assert run(pg, PA)["modules"] == 1
    assert [_field(neo4j_driver, NOTE, m) for m in (SH1, SH2)] == [1, 1], (
        "one owner's record is not enough: the other copy has none"
    )
    assert run(pg, PB)["modules"] == 1
    assert sorted(_field(neo4j_driver, NOTE, m) for m in (SH1, SH2)) == [0, 1], (
        "the module both owners recorded must be pruned"
    )
    assert run(pg, PA)["modules"] == 1
    assert run(pg, PB)["modules"] == 1
    assert [_field(neo4j_driver, NOTE, m) for m in (SH1, SH2)] == [0, 0]
    for profile in (PA, PB):
        assert run(pg, profile)["modules"] == 0, profile
        for name in (SH1, SH2):
            assert _field_emb(pg, name, profile, NOTE) == 0, (profile, name)
            assert _field_emb(pg, name, profile, "label") == 1, (profile, name)
    _code, report = _audit(monkeypatch, capsys, "--all")
    for basename in ("addons_a", "addons_b"):
        assert _repo_entry(report, basename).get("shared_parse_backlog") == {
            "remaining": 0, "next_run": [],
        }, basename


SH3 = "viin_shared_three"


def test_bootstrap_budget_env_change_between_two_runs_of_one_process_is_honoured(
    pg, neo4j_driver, tmp_path, monkeypatch,
):
    """A long-lived process (Web UI, scheduler) runs the index several times.
    Its operator changes OSM_SHARED_PARSE_BOOTSTRAP_PER_RUN between runs: 1, then
    0 (disabled), then 2. Each run re-parses exactly that many of the repo's
    three shared copies still lacking a complete-parse record."""
    a = GitRepo(tmp_path / "a", "addons_a")
    b = GitRepo(tmp_path / "b", "addons_b")
    for repo in (a, b):
        for name in (SH1, SH2, SH3):
            _simple_module(repo, name, "")
        repo.commit("three shared modules")
    register(PA, a)
    register(PB, b)
    monkeypatch.setenv(_BUDGET_ENV, "0")
    run(pg, PA)
    run(pg, PB)
    _forget_full_parses(pg)  # the deploy moment: no record anywhere

    monkeypatch.setenv(_BUDGET_ENV, "1")
    first = run(pg, PA)
    monkeypatch.setenv(_BUDGET_ENV, "0")
    disabled = run(pg, PA)
    monkeypatch.setenv(_BUDGET_ENV, "2")
    second = run(pg, PA)

    assert [first["modules"], disabled["modules"], second["modules"]] == [1, 0, 2]


def test_bootstrap_budget_reads_the_environment_on_every_call(monkeypatch):
    """shared_parse_bootstrap_per_run() follows the variable as it changes within
    one process; unset or empty falls back to the default (60)."""
    monkeypatch.delenv(_BUDGET_ENV, raising=False)
    budget = getattr(constants, "shared_parse_bootstrap_per_run", None)
    assert callable(budget), "the budget must be read through a call, not an import-time constant"
    assert budget() == 60
    monkeypatch.setenv(_BUDGET_ENV, "2")
    assert budget() == 2
    monkeypatch.setenv(_BUDGET_ENV, "0")
    assert budget() == 0
    monkeypatch.setenv(_BUDGET_ENV, "")
    assert budget() == 60
