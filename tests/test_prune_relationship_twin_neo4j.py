# SPDX-License-Identifier: AGPL-3.0-or-later
"""``Neo4jWriter.module_stale_children`` flags a re-pointed relationship.

The writers link ONE of several same-identity end nodes by ranking: INHERITS /
DELEGATES_TO / REPORTS_ON pick one Model node of a name (one per module, C1
schema), PATCHES one OWLComp, USES_CORE_SYMBOL a CoreSymbol. When the ranking
winner changes, the run writes E -> winner2 and E -> winner1 goes stale while
the source still names the target, so the text oracle alone would keep the old
edge for ever (two parents for one ``_inherit``). The read must flag such an
edge ``twin`` - a fresh edge of the same type from the same start node to
another node of the same identity - and only such an edge, and hand the
oracle the end node's ``status`` (USES_CORE_SYMBOL exists only towards a
``deprecated`` CoreSymbol).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.indexer import prune_evidence as pe
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j

V = TEST_VERSION
MOD = "viin_twin_ext"


@pytest.fixture
def writer(clean_neo4j, neo4j_driver):
    w = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    yield w
    w.close()


_GRAPH = """
CREATE (m:Module {name: $mod, odoo_version: $v, written_run: 'run-2'})
CREATE (e:Model {name: 'viin.twin.rec', module: $mod, odoo_version: $v, written_run: 'run-2'})
CREATE (mth:Method {name: 'action', model: 'viin.twin.rec', module: $mod, odoo_version: $v,
                    written_run: 'run-2'})
CREATE (mix_a:Model {name: 'some.mixin', module: 'mod_a', odoo_version: $v})
CREATE (mix_b:Model {name: 'some.mixin', module: 'mod_b', odoo_version: $v})
CREATE (oth_a:Model {name: 'other.mixin', module: 'mod_a', odoo_version: $v})
CREATE (par_a:Model {name: 'res.partner', module: 'mod_a', odoo_version: $v})
CREATE (par_b:Model {name: 'res.partner', module: 'mod_b', odoo_version: $v})
CREATE (cs:CoreSymbol {qualified_name: 'odoo.models.old_api', odoo_version: $v,
                       status: 'active'})
CREATE (e)-[:INHERITS {written_run: 'run-1'}]->(mix_a)
CREATE (e)-[:INHERITS {written_run: 'run-2'}]->(mix_b)
CREATE (e)-[:INHERITS {written_run: 'run-1'}]->(oth_a)
CREATE (e)-[:DELEGATES_TO {via_field: 'partner_id', written_run: 'run-1'}]->(par_a)
CREATE (e)-[:DELEGATES_TO {via_field: 'partner_id', written_run: 'run-2'}]->(par_b)
CREATE (e)-[:DELEGATES_TO {via_field: 'other_id', written_run: 'run-1'}]->(par_a)
CREATE (mth)-[:USES_CORE_SYMBOL {written_run: 'run-1'}]->(cs)
"""


def test_only_a_relationship_with_a_fresh_same_identity_twin_is_flagged(writer, neo4j_driver):
    with neo4j_driver.session() as s:
        s.run(_GRAPH, mod=MOD, v=V).consume()

    rows = writer.module_stale_children(V, MOD, run_id="run-2")["rels"]
    by_kind: dict[tuple, list[dict]] = {}
    for row in rows:
        key = (row["type"], row["end_name"] or row["end_qualified_name"])
        by_kind.setdefault(key, []).append(row)

    [inherits_old] = by_kind[("INHERITS", "some.mixin")]
    assert inherits_old["twin"] is True, "re-pointed from mod_a to mod_b"
    [inherits_other] = by_kind[("INHERITS", "other.mixin")]
    assert inherits_other["twin"] is False, "no fresh edge to an other.mixin node"
    delegates = sorted(by_kind[("DELEGATES_TO", "res.partner")], key=lambda r: r["twin"])
    assert [r["twin"] for r in delegates] == [False, True], (
        "only the same via_field is the same delegation")
    [core] = by_kind[("USES_CORE_SYMBOL", "odoo.models.old_api")]
    assert core["twin"] is False and core["end_status"] == "active"

    verdicts = pe.classify(
        pe.ModuleSource(MOD, Path("."), ("",), True),
        [], [pe.stale_rel_from_row(r) for r in rows],
    ).rels
    assert verdicts[inherits_old["id"]] == pe.VERDICT_SUPERSEDED
    assert verdicts[core["id"]] == pe.VERDICT_SUPERSEDED
    assert verdicts[inherits_other["id"]] == pe.VERDICT_GONE, "nothing names it any more"
