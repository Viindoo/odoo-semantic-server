# SPDX-License-Identifier: AGPL-3.0-or-later
"""Module retirement cascade - one SSOT delete for a module's whole subtree (ADR-0056 B6).

Business rules protected (plan B6, review M4/M6/M12/H2):

* Retiring a module removes the Module AND every node the index attributes to it
  (every ``MODULE_CHILD_LABELS`` label, including the LintViolation reached only
  through its View, the Python/JS test surface and the addon TestHelper twin).
  Shared/framework nodes and every other module survive; a second retire is a
  no-op.
* ``MODULE_CHILD_LABELS`` is the set of labels the writers really attach to a
  module (T24) - measured by driving the REAL indexer over a fixture module,
  never by copying the constant.
* A module (re)written after the retiring run started is NOT deleted, and
  neither are its children (H2 race guard).
* ``drop_module_owner`` leaves every node of the module owned by exactly the
  surviving owners and removes the departed repo's test nodes (M4/M12).
* Orphan finders report children whose Module is gone and indexed modules
  absent from the scan - never framework / placeholder / dependency-stub nodes.

Real case: ``viin_ai_rag`` was merged into ``viin_ai`` and deleted; the index
must retire ``viin_ai_rag`` while ``viin_ai`` (which it extended) survives.
"""
from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timedelta

import pytest

from src.indexer.writer_neo4j import Neo4jWriter
from tests import _retirement_fixture as fx
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j

V = TEST_VERSION
OTHER_V = "98.0"


@pytest.fixture
def writer(clean_neo4j):
    w = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    w.setup_indexes()
    yield w
    w.close()
    with clean_neo4j.session() as s:
        s.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=OTHER_V)


@pytest.fixture
def indexed(tmp_path, writer, monkeypatch):
    """viin_ai + viin_ai_rag indexed through the real pipeline (one repo, one profile)."""
    repo_dir = fx.build_and_index(tmp_path, writer, monkeypatch)
    return repo_dir


def _count(driver, cypher: str, **params) -> int:
    with driver.session() as s:
        return s.run(cypher, v=V, **params).single()["n"]


def _module_exists(driver, name: str, version: str = V) -> bool:
    with driver.session() as s:
        return s.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) RETURN count(m) AS n",
            n=name, v=version,
        ).single()["n"] > 0


def _seed_shared_nodes(driver) -> None:
    """Version-global nodes that belong to no module (spec catalogue, CLI surface)."""
    with driver.session() as s:
        s.run(
            """
            MERGE (:CoreSymbol {qualified_name: 'odoo.models.Model.write', odoo_version: $v})
            MERGE (:LintRule {rule_id: 'W8140', odoo_version: $v})
            MERGE (:CLICommand {name: 'server', odoo_version: $v})
            MERGE (:CLIFlag {name: '--addons-path', odoo_version: $v})
            """,
            v=V,
        )


def _shared_snapshot(driver) -> dict[str, int]:
    """Counts of every node the retirement of viin_ai_rag must NOT touch."""
    return {
        "survivor_children": sum(fx.labels_with_module(driver, fx.SURVIVOR).values()),
        "survivor_module": int(_module_exists(driver, fx.SURVIVOR)),
        "framework_helpers": _count(
            driver, "MATCH (h:TestHelper {odoo_version: $v, module: '@framework'}) "
                    "RETURN count(h) AS n"),
        "unresolved": _count(
            driver, "MATCH (n {odoo_version: $v, module: '__unresolved__'}) RETURN count(n) AS n"),
        "asset_bundles": _count(
            driver, "MATCH (b:AssetBundle {odoo_version: $v}) RETURN count(b) AS n"),
        "core_symbols": _count(
            driver, "MATCH (c:CoreSymbol {odoo_version: $v}) RETURN count(c) AS n"),
        "lint_rules": _count(
            driver, "MATCH (r:LintRule {odoo_version: $v}) RETURN count(r) AS n"),
        "cli": _count(
            driver, "MATCH (c) WHERE c.odoo_version = $v AND (c:CLICommand OR c:CLIFlag) "
                    "RETURN count(c) AS n"),
        "dep_stubs": _count(
            driver, "MATCH (m:Module {odoo_version: $v}) WHERE m.name IN ['base', 'web'] "
                    "RETURN count(m) AS n"),
    }


def _retired_subtree_counts(driver) -> dict[str, int]:
    """{label: count} of viin_ai_rag's attributed nodes as the graph holds them now."""
    counts = fx.labels_with_module(driver, fx.RETIRED)
    counts.pop("AssetBundle", None)  # version-global, first-contributor stamp only
    lv = fx.lint_violations_of(driver, fx.RETIRED)
    if lv:
        counts["LintViolation"] = lv
    return counts


# ---------------------------------------------------------------------------
# T24 - the constant equals what the writers really attach to a module
# ---------------------------------------------------------------------------


def test_child_labels_constant_equals_labels_the_writers_attach_to_a_module(
    indexed, clean_neo4j,
):
    """T24: every label a writer stamps with ``module`` is either a cascaded child
    or the declared shared label - a new writer label missing from the constant
    would silently survive retirement as a ghost. The actual side is measured on a
    module indexed by the REAL pipeline."""
    from src.indexer.writer_neo4j import MODULE_CHILD_LABELS, MODULE_SHARED_LABELS

    written = set(fx.labels_with_module(clean_neo4j, fx.RETIRED))
    expected = (set(MODULE_CHILD_LABELS) - {"LintViolation"}) | set(MODULE_SHARED_LABELS)
    missing_from_constant = written - expected
    assert not missing_from_constant, (
        f"writers attach {sorted(missing_from_constant)} to a module but the cascade "
        f"does not know them - they would survive retirement"
    )
    never_written = expected - written
    assert not never_written, (
        f"constant lists {sorted(never_written)} but the fixture module (which ships "
        f"every artifact kind) produced none - stale constant or incomplete fixture"
    )


def test_lint_violation_is_the_edge_linked_child_label(indexed, clean_neo4j):
    """T24 (edge side): a LintViolation carries no ``module`` - it belongs to a module
    only through its View - and it is still a cascaded child label."""
    from src.indexer.writer_neo4j import MODULE_CHILD_LABELS

    assert fx.lint_violations_of(clean_neo4j, fx.RETIRED) >= 1, (
        "positive control: the invalid list view must yield a LintViolation"
    )
    assert _count(
        clean_neo4j,
        "MATCH (lv:LintViolation {odoo_version: $v}) WHERE lv.module IS NOT NULL "
        "RETURN count(lv) AS n",
    ) == 0
    assert "LintViolation" in MODULE_CHILD_LABELS


# ---------------------------------------------------------------------------
# retire_modules - full cascade, shared nodes survive, idempotent
# ---------------------------------------------------------------------------


def test_retiring_merged_module_removes_its_whole_subtree(writer, indexed, clean_neo4j):
    """viin_ai_rag retired after its merge into viin_ai: the Module and every node the
    index attributes to it disappear (models, fields, methods, views, the list-view
    LintViolation, QWeb, report, JS patch, OWL component, stylesheet, JS test suite,
    Python TestClass/TestMethod and the addon TestHelper twin)."""
    driver = clean_neo4j
    before = _retired_subtree_counts(driver)
    assert {"TestClass", "TestMethod", "TestHelper", "Stylesheet", "JsTestSuite",
            "LintViolation", "View", "Model"} <= set(before), (
        f"positive control: fixture must have produced the whole subtree, got {before}"
    )

    result = writer.retire_modules(V, [fx.RETIRED], run_started_at=writer.server_now())

    assert not _module_exists(driver, fx.RETIRED)
    assert _retired_subtree_counts(driver) == {}, "no node of viin_ai_rag may survive"
    assert result["modules"] == 1
    assert result["retired"] == [fx.RETIRED]
    assert result["skipped_recent"] == []
    assert result["children"] == sum(before.values())
    assert {k: v for k, v in result["by_label"].items() if v} == before


def test_retiring_a_module_keeps_shared_framework_and_other_module_nodes(
    writer, indexed, clean_neo4j,
):
    """Shared nodes (framework TestHelpers, __unresolved__ placeholders, AssetBundle,
    CoreSymbol, LintRule, CLI*, profile-less dependency stubs) and the surviving
    module viin_ai (which viin_ai_rag extended) are untouched."""
    driver = clean_neo4j
    _seed_shared_nodes(driver)
    before = _shared_snapshot(driver)
    assert before["framework_helpers"] > 0 and before["unresolved"] > 0, (
        f"positive control: fixture must produce framework + placeholder nodes: {before}"
    )

    writer.retire_modules(V, [fx.RETIRED], run_started_at=writer.server_now())

    assert _shared_snapshot(driver) == before
    # viin_ai's own ai.assistant definition survives, only the rag extension is gone.
    assert _count(driver, "MATCH (m:Model {odoo_version: $v, name: 'ai.assistant'}) "
                          "RETURN count(m) AS n") == 1


def test_retiring_a_module_does_not_touch_other_versions(writer, indexed, clean_neo4j):
    """Retirement at 99.0 must leave the same-named module at another version alone."""
    driver = clean_neo4j
    with driver.session() as s:
        s.run(
            """
            MERGE (m:Module {name: $n, odoo_version: $ov}) SET m.profile = ['viindoo_98']
            MERGE (md:Model {name: 'ai.rag.source', module: $n, odoo_version: $ov})
            MERGE (md)-[:DEFINED_IN]->(m)
            """,
            n=fx.RETIRED, ov=OTHER_V,
        )

    writer.retire_modules(V, [fx.RETIRED], run_started_at=writer.server_now())

    assert _module_exists(driver, fx.RETIRED, OTHER_V)
    with driver.session() as s:
        assert s.run(
            "MATCH (md:Model {module: $n, odoo_version: $ov}) RETURN count(md) AS n",
            n=fx.RETIRED, ov=OTHER_V,
        ).single()["n"] == 1


def test_retiring_twice_is_a_no_op(writer, indexed, clean_neo4j):
    """Idempotent: a replayed retirement (crash + resume) deletes nothing more."""
    writer.retire_modules(V, [fx.RETIRED], run_started_at=writer.server_now())
    snapshot = _shared_snapshot(clean_neo4j)

    second = writer.retire_modules(V, [fx.RETIRED], run_started_at=writer.server_now())

    assert second["modules"] == 0
    assert second["children"] == 0
    assert all(v == 0 for v in second["by_label"].values())
    assert _shared_snapshot(clean_neo4j) == snapshot


def test_sentinel_names_are_never_retired(writer, indexed, clean_neo4j):
    """@framework TestHelpers and __unresolved__ placeholders are shared by every
    module; passing their names to retire_modules must delete nothing."""
    before = _shared_snapshot(clean_neo4j)

    result = writer.retire_modules(
        V, ["@framework", "__unresolved__"], run_started_at=writer.server_now(),
    )

    assert result["modules"] == 0 and result["children"] == 0
    assert _shared_snapshot(clean_neo4j) == before


def test_retire_rejects_a_naive_run_started_at(writer, indexed):
    """The race guard compares against the server clock; a naive datetime has no
    comparable instant and must be refused instead of guessing a timezone."""
    with pytest.raises(ValueError):
        writer.retire_modules(V, [fx.RETIRED], run_started_at=datetime.now())
    assert _module_exists(writer.driver, fx.RETIRED)


# ---------------------------------------------------------------------------
# H2 - a module re-seen after the run started keeps its whole subtree
# ---------------------------------------------------------------------------


def test_module_rewritten_after_run_started_is_kept_with_its_children(
    tmp_path, writer, indexed, clean_neo4j, monkeypatch, caplog,
):
    """H2: another run re-wrote viin_ai_rag after this run decided to retire it
    (e.g. the module moved to a repo indexed concurrently). Neither the Module nor
    any child may be deleted, and the operator is warned."""
    driver = clean_neo4j
    run_started_at = writer.server_now()
    # The concurrent owner re-writes the module through the real write path.
    fx.index_repo_dir(writer, monkeypatch, indexed, profile="viindoo_99", repo_id=7801)
    before = _retired_subtree_counts(driver)

    with caplog.at_level(logging.WARNING):
        result = writer.retire_modules(V, [fx.RETIRED], run_started_at=run_started_at)

    assert result["skipped_recent"] == [fx.RETIRED]
    assert result["modules"] == 0 and result["children"] == 0
    assert _module_exists(driver, fx.RETIRED)
    assert _retired_subtree_counts(driver) == before
    assert any(
        r.levelno == logging.WARNING and fx.RETIRED in r.getMessage() for r in caplog.records
    ), "a skipped retirement must be surfaced to the operator"


def _python_only_copy(tmp_path, name: str = fx.RETIRED):
    """The same module as another repo ships it: manifest + Python model only."""
    repo_dir = tmp_path / "concurrent_repo"
    mod = repo_dir / name
    fx._write(mod / "__init__.py", "from . import models\n")
    fx._write(mod / "__manifest__.py", fx._manifest(name, [fx.SURVIVOR]))
    fx._write(mod / "models" / "__init__.py", "from . import ai_rag_source\n")
    fx._write(mod / "models" / "ai_rag_source.py", fx._RAG_MODELS)
    fx.git_init_commit(repo_dir)
    return repo_dir


@pytest.mark.parametrize("step", ["LintViolation", "Model", "Module"])
def test_module_rewritten_during_the_cascade_keeps_every_node_the_concurrent_run_wrote(
    tmp_path, writer, indexed, clean_neo4j, step,
):
    """Final review D2: the first race check passed (viin_ai_rag was stale), then
    - while the cascade deletes children - a concurrent run of another profile
    (which does not hold retire:<v>) re-MERGEs the module and its Python model.
    Every node that run wrote survives, whichever delete step it lands before;
    the Module survives and the name is reported ``skipped_recent`` (so the
    caller keeps its embeddings and does not record it retired). Children the
    concurrent run did not write are stale and still go. Deterministic: the
    concurrent write runs in a hook on the named cascade statement."""
    from src.indexer import writer_neo4j as wn

    driver = clean_neo4j
    concurrent_dir = _python_only_copy(tmp_path)
    stale_labels = {"View", "Stylesheet", "JsTestSuite", "TestClass"}
    assert stale_labels <= set(_retired_subtree_counts(driver)), "positive control"
    run_started_at = writer.server_now()
    real = wn._run_single_with_retry
    written: dict = {}

    def hook(session, name, *args, **kwargs):
        if name == f"retire_modules[{step}]" and not written:
            written["fired"] = True
            w2 = Neo4jWriter(
                uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
                user=os.getenv("NEO4J_TEST_USER", "neo4j"),
                password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
            )
            try:
                with pytest.MonkeyPatch.context() as mp:
                    fx.index_repo_dir(w2, mp, concurrent_dir, profile="other_99",
                                      repo_id=7802)
            finally:
                w2.close()
            with driver.session() as s:
                written["ids"] = set(s.run(
                    "MATCH (n {odoo_version: $v, module: $m}) "
                    "WHERE NOT n:Module AND 'other_99' IN coalesce(n.profile, []) "
                    "RETURN collect(elementId(n)) AS ids", v=V, m=fx.RETIRED,
                ).single()["ids"])
        return real(session, name, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wn, "_run_single_with_retry", hook)
        result = writer.retire_modules(V, [fx.RETIRED], run_started_at=run_started_at)

    assert written.get("fired"), f"the cascade never ran retire_modules[{step}]"
    assert written["ids"], "precondition: the concurrent run wrote children"
    with driver.session() as s:
        left = set(s.run(
            "MATCH (n) WHERE elementId(n) IN $ids RETURN collect(elementId(n)) AS ids",
            ids=sorted(written["ids"]),
        ).single()["ids"])
    assert left == written["ids"], (
        f"{len(written['ids'] - left)} node(s) written after the run started were deleted")
    assert _module_exists(driver, fx.RETIRED)
    assert result["skipped_recent"] == [fx.RETIRED], result
    assert fx.RETIRED not in result["retired"], result
    remaining = set(_retired_subtree_counts(driver))
    assert not (stale_labels & remaining), (
        f"children the concurrent run did not write are stale and must go: {remaining}")


def test_presence_stamp_after_run_started_also_protects_the_module(
    writer, indexed, clean_neo4j,
):
    """H2: the ledger sync path stamps a present module without re-parsing it; that
    stamp is as good as a re-write for the race guard."""
    run_started_at = writer.server_now()
    writer.stamp_module_presence(V, [{"name": fx.RETIRED}], head="abc123")

    result = writer.retire_modules(V, [fx.RETIRED], run_started_at=run_started_at)

    assert result["skipped_recent"] == [fx.RETIRED]
    assert _module_exists(clean_neo4j, fx.RETIRED)


def test_guard_is_per_name_stale_names_in_same_call_are_retired(
    writer, indexed, clean_neo4j,
):
    """H2 is per module: a re-seen name is kept while a stale legacy name (written
    before last_seen_at existed) in the same call is retired with its children."""
    driver = clean_neo4j
    with driver.session() as s:
        s.run(
            """
            MERGE (m:Module {name: 'viin_ai_rag_legacy', odoo_version: $v})
            SET m.profile = ['viindoo_99'], m.repo = 'viindoo_addons'
            MERGE (md:Model {name: 'ai.rag.legacy', module: 'viin_ai_rag_legacy',
                             odoo_version: $v})
            MERGE (md)-[:DEFINED_IN]->(m)
            """,
            v=V,
        )
    run_started_at = writer.server_now()
    writer.stamp_module_presence(V, [{"name": fx.RETIRED}], head="abc123")

    result = writer.retire_modules(
        V, [fx.RETIRED, "viin_ai_rag_legacy"], run_started_at=run_started_at,
    )

    assert result["retired"] == ["viin_ai_rag_legacy"]
    assert result["skipped_recent"] == [fx.RETIRED]
    assert not _module_exists(driver, "viin_ai_rag_legacy")
    assert _count(driver, "MATCH (md:Model {module: 'viin_ai_rag_legacy', odoo_version: $v}) "
                          "RETURN count(md) AS n") == 0
    assert _module_exists(driver, fx.RETIRED)


def test_real_module_write_records_when_it_was_seen(writer, indexed, clean_neo4j):
    """H2 precondition: every real Module write stamps last_seen_at from the server
    clock; profile-less dependency stubs (base, web) are not stamped."""
    with clean_neo4j.session() as s:
        rows = {
            r["name"]: r["seen"] for r in s.run(
                "MATCH (m:Module {odoo_version: $v}) RETURN m.name AS name, "
                "m.last_seen_at AS seen", v=V,
            ).data()
        }
    assert rows[fx.RETIRED] is not None and rows[fx.SURVIVOR] is not None
    assert rows["base"] is None and rows["web"] is None
    assert rows[fx.RETIRED].to_native() <= writer.server_now()


# ---------------------------------------------------------------------------
# M6 - orphan finders
# ---------------------------------------------------------------------------


def test_children_left_behind_by_module_only_gc_are_found_and_retired(
    writer, indexed, clean_neo4j,
):
    """M6: the old Module-only --gc deleted the Module node and stranded its children.
    orphan_child_keys must attribute that debris (LintViolation through its View,
    a dangling LintViolation through its xmlid prefix) and retire_modules must clear
    it - while framework and placeholder nodes are never reported."""
    driver = clean_neo4j
    with driver.session() as s:
        s.run(
            """
            MERGE (lv:LintViolation {view_xmlid: 'viin_ai_rag.gone_view', rule_id: 'rng',
                                     line: 1, file_path: 'viin_ai_rag/views/gone.xml',
                                     odoo_version: $v})
            SET lv.message = 'dangling'
            """,
            v=V,
        )
    expected = _retired_subtree_counts(driver)
    with driver.session() as s:
        s.run("MATCH (m:Module {name: $n, odoo_version: $v}) DETACH DELETE m",
              n=fx.RETIRED, v=V)

    orphans = writer.orphan_child_keys(V)

    assert list(orphans) == [fx.RETIRED], f"only viin_ai_rag debris expected: {orphans}"
    assert orphans[fx.RETIRED] == expected
    assert "@framework" not in orphans and "__unresolved__" not in orphans

    writer.retire_modules(V, list(orphans), run_started_at=writer.server_now())

    assert _retired_subtree_counts(driver) == {}
    assert writer.orphan_child_keys(V) == {}


def test_orphan_module_names_lists_indexed_modules_absent_from_scan(
    writer, indexed, clean_neo4j,
):
    """A module the scan no longer sees is an orphan; dependency stubs (base, web)
    and sentinel names are never orphans; repo= narrows to one repo's modules."""
    with clean_neo4j.session() as s:
        s.run(
            """
            MERGE (m:Module {name: 'tvtma_mrp', odoo_version: $v})
            SET m.profile = ['tvtma_99'], m.repo = 'tvtmaaddons'
            MERGE (u:Module {name: '__unresolved__', odoo_version: $v})
            SET u.profile = ['viindoo_99'], u.repo = 'viindoo_addons'
            """,
            v=V,
        )

    assert writer.orphan_module_names(V, [fx.SURVIVOR]) == sorted([fx.RETIRED, "tvtma_mrp"])
    assert writer.orphan_module_names(V, [fx.SURVIVOR], repo="viindoo_addons") == [fx.RETIRED]
    assert writer.orphan_module_names(V, [fx.SURVIVOR, fx.RETIRED, "tvtma_mrp"]) == []


def test_module_profiles_reports_owning_profiles(writer, indexed, clean_neo4j):
    """module_profiles gives the owners of indexed modules (profile-less stubs excluded);
    a requested name with a node but no profile maps to an empty list."""
    everything = writer.module_profiles(V)
    assert everything == {fx.RETIRED: ["viindoo_99"], fx.SURVIVOR: ["viindoo_99"]}
    assert writer.module_profiles(V, [fx.RETIRED, "base", "no_such_module"]) == {
        fx.RETIRED: ["viindoo_99"], "base": [],
    }


# ---------------------------------------------------------------------------
# stamp_module_presence
# ---------------------------------------------------------------------------


def test_presence_stamp_counts_only_existing_modules_and_never_creates(
    writer, indexed, clean_neo4j,
):
    """The matched count is the self-heal signal: a live name without a node is not
    counted and no node is invented for it; repos come from the ledger rows."""
    matched = writer.stamp_module_presence(
        V,
        [{"name": fx.RETIRED, "repos": ["viindoo_addons", "tvtmaaddons", "viindoo_addons"]},
         {"name": "viin_ai_never_indexed"}],
        head="deadbeef",
    )

    assert matched == 1
    assert not _module_exists(clean_neo4j, "viin_ai_never_indexed")
    with clean_neo4j.session() as s:
        row = s.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) "
            "RETURN m.repos AS repos, m.last_seen_sha AS sha",
            n=fx.RETIRED, v=V,
        ).single()
    assert row["repos"] == ["tvtmaaddons", "viindoo_addons"]
    assert row["sha"] == "deadbeef"


def test_presence_stamp_rejects_naive_now(writer, indexed):
    with pytest.raises(ValueError):
        writer.stamp_module_presence(V, [{"name": fx.RETIRED}], head="x", now=datetime.now())


def test_presence_stamp_with_explicit_now_is_the_guard_instant(writer, indexed, clean_neo4j):
    """An explicit aware ``now`` is what the race guard compares against."""
    future = writer.server_now() + timedelta(hours=1)
    writer.stamp_module_presence(V, [{"name": fx.RETIRED}], head="x", now=future)

    result = writer.retire_modules(
        V, [fx.RETIRED], run_started_at=future - timedelta(minutes=1),
    )
    assert result["skipped_recent"] == [fx.RETIRED]

    result = writer.retire_modules(
        V, [fx.RETIRED], run_started_at=future + timedelta(minutes=1),
    )
    assert result["retired"] == [fx.RETIRED]
    assert not _module_exists(clean_neo4j, fx.RETIRED)


# ---------------------------------------------------------------------------
# drop_module_owner - M4 exact subtree reset, M12 departed repo's tests
# ---------------------------------------------------------------------------


@pytest.fixture
def moved(tmp_path, writer, monkeypatch):
    """viin_ai_rag shipped by two repos (the to_saas_base A->B move mid-flight):
    repo A under profile pa_99, then repo B under pb_99."""
    fx.build_and_index(tmp_path, writer, monkeypatch,
                       repo_name="saas_addons_a", profile="pa_99", repo_id=7811)
    fx.build_and_index(tmp_path, writer, monkeypatch,
                       repo_name="saas_addons_b", profile="pb_99", repo_id=7812)


def _profiles_of_module_nodes(driver, module: str) -> set[tuple[str, ...]]:
    """Distinct profile arrays on the Module and every node attributed to it."""
    with driver.session() as s:
        rows = s.run(
            """
            MATCH (n) WHERE n.odoo_version = $v
              AND ((n:Module AND n.name = $m) OR (n.module = $m AND NOT n:AssetBundle))
            RETURN DISTINCT n.profile AS p
            UNION
            MATCH (lv:LintViolation {odoo_version: $v})
            WHERE lv.view_xmlid STARTS WITH ($m + '.')
            RETURN DISTINCT lv.profile AS p
            """,
            v=V, m=module,
        ).data()
    return {tuple(r["p"] or ()) for r in rows}


def test_drop_owner_leaves_every_node_owned_by_the_surviving_owner_only(
    writer, moved, clean_neo4j,
):
    """M4: after repo A stops shipping the module, the Module and every child
    (LintViolation and addon TestHelper included) carry exactly B's profile - a
    leftover A entry would hide the whole module from a B-only tenant."""
    from src.indexer.models import ModuleOwner

    driver = clean_neo4j
    assert ("pa_99", "pb_99") in _profiles_of_module_nodes(driver, fx.RETIRED), (
        "positive control: both owners stamped the shared nodes"
    )

    writer.drop_module_owner(
        V, fx.RETIRED,
        [ModuleOwner(profile_name="pb_99", repo_basename="saas_addons_b",
                     path=fx.RETIRED, repo_id=7812)],
    )

    assert _profiles_of_module_nodes(driver, fx.RETIRED) == {("pb_99",)}
    with driver.session() as s:
        mod = s.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) "
            "RETURN m.profile AS p, m.repos AS repos, m.repo AS repo, m.repo_id AS rid",
            n=fx.RETIRED, v=V,
        ).single()
    assert mod["p"] == ["pb_99"]
    assert mod["repos"] == ["saas_addons_b"]
    assert mod["repo"] == "saas_addons_b"
    assert mod["rid"] == 7812


def test_drop_owner_deletes_departed_repos_tests_and_keeps_survivors(
    writer, moved, clean_neo4j,
):
    """M12/G7: repo A's TestClass/TestMethod copies of the module (and legacy test
    nodes with no repo) go; repo B's copies stay."""
    from src.indexer.models import ModuleOwner

    driver = clean_neo4j
    with driver.session() as s:
        s.run(
            "MERGE (tc:TestClass {name: 'TestLegacyRag', module: $m, odoo_version: $v, "
            "file_path: 'viin_ai_rag/tests/test_legacy.py'})",
            m=fx.RETIRED, v=V,
        )

    def _tests_by_repo():
        with driver.session() as s:
            return {
                (r["lbl"], r["repo"]): r["n"] for r in s.run(
                    """
                    MATCH (t {odoo_version: $v, module: $m})
                    WHERE t:TestClass OR t:TestMethod
                    RETURN labels(t)[0] AS lbl, t.repo AS repo, count(*) AS n
                    """,
                    v=V, m=fx.RETIRED,
                ).data()
            }

    before = _tests_by_repo()
    departed = sum(n for (_lbl, repo), n in before.items() if repo != "saas_addons_b")
    assert departed >= 3, f"positive control: A's tests + legacy node seeded: {before}"

    result = writer.drop_module_owner(
        V, fx.RETIRED, [ModuleOwner(profile_name="pb_99", repo_basename="saas_addons_b")],
    )

    after = _tests_by_repo()
    assert after == {k: n for k, n in before.items() if k[1] == "saas_addons_b"}
    assert result["tests_deleted"] == departed
    assert result["module"] == 1


def test_drop_owner_with_two_survivors_unions_them_exactly(writer, moved, clean_neo4j):
    """viin_meeting_room style: a third repo drops out, two remain; every node gets the
    two survivors' profiles, sorted, and nothing else."""
    from src.indexer.models import ModuleOwner

    with clean_neo4j.session() as s:
        s.run(
            "MATCH (n) WHERE n.odoo_version = $v AND (n.module = $m OR "
            "(n:Module AND n.name = $m)) SET n.profile = n.profile + ['pc_99']",
            v=V, m=fx.RETIRED,
        )

    writer.drop_module_owner(
        V, fx.RETIRED,
        [ModuleOwner("pb_99", "saas_addons_b"), ModuleOwner("pa_99", "saas_addons_a")],
    )

    assert _profiles_of_module_nodes(clean_neo4j, fx.RETIRED) == {("pa_99", "pb_99")}


def test_drop_owner_without_survivors_is_refused(writer, moved, clean_neo4j):
    """No survivor means retirement, which has its own guarded primitive."""
    with pytest.raises(ValueError):
        writer.drop_module_owner(V, fx.RETIRED, [])
    assert _module_exists(clean_neo4j, fx.RETIRED)


def test_drop_owner_on_sentinel_name_changes_nothing(writer, moved, clean_neo4j):
    from src.indexer.models import ModuleOwner

    with clean_neo4j.session() as s:
        before = s.run(
            "MATCH (h:TestHelper {module: '@framework', odoo_version: $v}) "
            "RETURN collect(h.profile) AS p", v=V,
        ).single()["p"]

    writer.drop_module_owner(V, "@framework", [ModuleOwner("pb_99", "saas_addons_b")])

    with clean_neo4j.session() as s:
        after = s.run(
            "MATCH (h:TestHelper {module: '@framework', odoo_version: $v}) "
            "RETURN collect(h.profile) AS p", v=V,
        ).single()["p"]
    assert sorted(map(tuple, after)) == sorted(map(tuple, before))


# ---------------------------------------------------------------------------
# A plain index run after the module's deletion removes its whole subtree
# ---------------------------------------------------------------------------


@pytest.mark.postgres
def test_gc_run_after_module_deletion_removes_the_module_subtree(
    clean_pg, clean_neo4j, tmp_path,
):
    """The viin_ai_rag directory is deleted in a commit; the next index run must
    retire the module AND its children (the old Module-only GC stranded models,
    views, tests, stylesheets ... as ghosts), while viin_ai stays intact.

    PORTED (ADR-0056 B8/B9): this test drove the transitional per-repo ``--gc``
    shim of ``_index_repo``, removed by design (F1: the per-repo path never
    deletes - it could retire a module another repo still ships). The same
    outcome is now produced by a PLAIN ``index_profile`` run (the per-version
    reconcile), so the protection - every artifact kind of the deleted module
    goes, the survivor is untouched - is kept, driven through that run.
    """
    from src.db.migrate import run_migrations
    from tests._lifecycle_repo import GitRepo, register, run

    driver = clean_neo4j
    run_migrations(clean_pg)
    repo = GitRepo(tmp_path, "viindoo_addons")
    fx.write_viin_ai(repo.path)
    fx.write_viin_ai_rag(repo.path)
    # The RelaxNG schemas index_profile looks for in an Odoo core checkout, so
    # the invalid list view yields a LintViolation as in the fixture's own run.
    shutil.copytree(fx.RNG_DIR, repo.path / "odoo" / "addons" / "base" / "rng")
    repo.commit("viin_ai + viin_ai_rag")
    register("viindoo_99", repo)
    run(clean_pg, "viindoo_99", embedder=None)
    assert _retired_subtree_counts(driver), "precondition: viin_ai_rag wrote children"
    assert fx.lint_violations_of(driver, fx.RETIRED) > 0, "precondition: a LintViolation"
    survivor_before = fx.labels_with_module(driver, fx.SURVIVOR)
    repo.rm(fx.RETIRED)
    repo.commit("merge viin_ai_rag into viin_ai")

    run(clean_pg, "viindoo_99", embedder=None)

    assert not _module_exists(driver, fx.RETIRED)
    assert _retired_subtree_counts(driver) == {}, (
        f"ghost children left: {_retired_subtree_counts(driver)}"
    )
    assert fx.lint_violations_of(driver, fx.RETIRED) == 0
    assert fx.labels_with_module(driver, fx.SURVIVOR) == survivor_before
    assert _module_exists(driver, fx.SURVIVOR)
