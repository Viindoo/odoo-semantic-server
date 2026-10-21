# SPDX-License-Identifier: AGPL-3.0-or-later
"""auto_install closure against a real Neo4j (#380): writer, read helpers,
module_inspect / impact_analysis output, tenant scope, and the backfill.

Graph (TEST_VERSION, profile ``p_core``)::

    base <- web (auto_install, triggers [base])  -> installed with every new DB
    sale    -> web
    stock   -> web
    bridge  -> sale, stock   (auto_install, list triggers [stock])
    sale_stock -> sale, stock (auto_install True, triggers [sale, stock])
    chain   -> bridge        (auto_install True, triggers [bridge])
    always  -> web           (auto_install [], installed with every new DB)
    private -> sale          (auto_install True, triggers [sale]) - profile ``p_other``
"""
from __future__ import annotations

import os

import pytest

from src.indexer.models import ModelInfo, ModuleInfo, ParseResult
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j


def _mod(name, depends, triggers=None):
    return ModuleInfo(
        name=name, odoo_version=TEST_VERSION, repo="r", path=f"/tmp/{name}",
        depends=list(depends), version_raw="99.0.1.0.0",
        auto_install=triggers is not None, auto_install_triggers=triggers,
    )


@pytest.fixture
def seeded(clean_neo4j, neo4j_driver):
    w = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    w.setup_indexes()
    sale_model = ModelInfo(name="ai.sale.doc", module="sale", odoo_version=TEST_VERSION,
                           had_explicit_name=True)
    w.write_results([
        ParseResult(module=_mod("base", []), models=[]),
        ParseResult(module=_mod("web", ["base"], ["base"]), models=[]),
        ParseResult(module=_mod("sale", ["web"]), models=[sale_model]),
        ParseResult(module=_mod("stock", ["web"]), models=[]),
        ParseResult(module=_mod("bridge", ["sale", "stock"], ["stock"]), models=[]),
        ParseResult(module=_mod("sale_stock", ["sale", "stock"], ["sale", "stock"]),
                    models=[]),
        ParseResult(module=_mod("chain", ["bridge"], ["bridge"]), models=[]),
        ParseResult(module=_mod("always", ["web"], []), models=[]),
    ], profiles=["p_core"])
    w.write_results([
        ParseResult(module=_mod("private", ["sale"], ["sale"]), models=[]),
    ], profiles=["p_other"])
    w.close()
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    import sys
    sys.modules.pop("src.mcp.server", None)
    import src.mcp.server as srv  # noqa: PLC0415
    return srv


def test_writer_persists_list_triggers_and_the_empty_form(seeded, neo4j_driver):
    with neo4j_driver.session() as s:
        rows = {r["n"]: r["t"] for r in s.run(
            "MATCH (m:Module {odoo_version: $v}) RETURN m.name AS n,"
            " m.auto_install_triggers AS t", v=TEST_VERSION)}
    assert rows["bridge"] == ["stock"]
    assert rows["web"] == ["base"]
    assert rows["always"] == []
    assert rows["sale"] is None


def test_module_inspect_lists_what_auto_install_brings(seeded):
    """Round 1: stock triggers bridge, which brings its other depend sale.
    Round 2: bridge triggers chain; sale (now installed) triggers private and,
    with stock, sale_stock. web is on every fresh database: not reported."""
    lines = seeded._module_dep_closure("stock", TEST_VERSION).splitlines()
    start = lines.index("├─ Auto-installed with this module (5):")
    assert lines[start + 1:start + 6] == [
        "│   ├─ bridge (triggered by: stock)",
        "│   ├─ sale (dependency of bridge)",
        "│   ├─ chain (triggered by: bridge)",
        "│   ├─ private (triggered by: sale)",
        "│   └─ sale_stock (triggered by: sale, stock)",
    ]
    assert lines[-1].startswith("└─ Next:")
    assert not any(ln.lstrip("│ ├└─").startswith("web (") for ln in lines)


def test_a_list_trigger_is_not_satisfied_by_a_non_trigger_dependency(seeded):
    """bridge triggers on stock only: installing sale (its other depend) does
    not pull it."""
    out = seeded._module_dep_closure("sale", TEST_VERSION)
    assert "bridge (" not in out
    assert "sale_stock (" not in out


def test_another_tenants_auto_install_module_is_never_named(seeded, monkeypatch):
    scope = {"own": ["p_core"], "shared": []}
    monkeypatch.setattr(seeded, "_scope", lambda *a, **k: dict(scope))
    out = seeded._module_dep_closure("sale", TEST_VERSION)
    assert "private" not in out
    monkeypatch.setattr(seeded, "_scope", lambda *a, **k: {"own": None, "shared": []})
    assert "private (triggered by: sale)" in seeded._module_dep_closure("sale", TEST_VERSION)


def test_nothing_is_installed_through_another_tenants_module(seeded, neo4j_driver, monkeypatch):
    """`acme` (p_core) depends on `foo` (p_other, invisible), which
    depends on `hidden_trig` (p_core). `hbridge` (p_core) triggers on
    `hidden_trig`, reachable only through `foo`: it must not be reported, and
    the Cypher must not hand back `foo`'s dependencies."""
    w = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    w.write_results([
        ParseResult(module=_mod("acme", ["web", "foo"]), models=[]),
        ParseResult(module=_mod("hidden_trig", ["web"]), models=[]),
        ParseResult(module=_mod("hbridge", ["hidden_trig"], ["hidden_trig"]), models=[]),
    ], profiles=["p_core"])
    w.write_results([
        ParseResult(module=_mod("foo", ["hidden_trig"]), models=[]),
    ], profiles=["p_other"])
    w.close()
    monkeypatch.setattr(seeded, "_scope", lambda *a, **k: {"own": ["p_core"], "shared": []})
    out = seeded._module_dep_closure("acme", TEST_VERSION)
    assert "hbridge" not in out and "hidden_trig" not in out, out
    # admin sees foo, so the same graph does pull hbridge in
    monkeypatch.setattr(seeded, "_scope", lambda *a, **k: {"own": None, "shared": []})
    assert "hbridge (triggered by: hidden_trig)" in seeded._module_dep_closure(
        "acme", TEST_VERSION)


def test_a_country_gated_candidate_is_listed_apart(seeded, neo4j_driver):
    """TEST_VERSION (99.0) follows the newest rules: the v18+ countries gate."""
    with neo4j_driver.session() as s:
        s.run("MATCH (m:Module {name: 'sale_stock', odoo_version: $v})"
              " SET m.countries = ['be', 'lu']", v=TEST_VERSION)
    out = seeded._module_dep_closure("stock", TEST_VERSION)
    assert "sale_stock (triggered by" not in out, out
    assert ("├─ Country-conditional, auto-installed only when a company is in one"
            " of its countries (1): sale_stock (BE, LU)") in out.splitlines()


def test_impact_analysis_lists_modules_auto_installed_with_the_definer(seeded):
    out = seeded._impact_analysis("model", "ai.sale.doc", TEST_VERSION)
    assert "Auto-installed with the defining module sale (1): private (triggered by: sale)" in out
    # listed apart: the risk score counts views/methods/JS only
    assert "Risk: LOW (0 affected entities)" in out


def test_backfill_sets_triggers_on_indexed_nodes_only_and_is_idempotent(
    seeded, neo4j_driver,
):
    from ops.backfill_auto_install import ModuleTriggers, apply_version

    with neo4j_driver.session() as s:
        s.run("MATCH (m:Module {name: 'bridge', odoo_version: $v})"
              " REMOVE m.auto_install_triggers", v=TEST_VERSION)
    rows = [
        ModuleTriggers("bridge", TEST_VERSION, True, ["stock"]),
        ModuleTriggers("sale", TEST_VERSION, False, None),
        ModuleTriggers("never_indexed", TEST_VERSION, True, ["sale"]),
    ]
    dry = apply_version(neo4j_driver, TEST_VERSION, rows, dry_run=True)
    assert dry["changed"] == ["bridge"] and dry["not_indexed"] == ["never_indexed"]
    with neo4j_driver.session() as s:
        assert s.run("MATCH (m:Module {name: 'bridge', odoo_version: $v})"
                     " RETURN m.auto_install_triggers AS t", v=TEST_VERSION
                     ).single()["t"] is None
    assert apply_version(neo4j_driver, TEST_VERSION, rows, dry_run=False)["changed"] == [
        "bridge",
    ]
    with neo4j_driver.session() as s:
        assert s.run("MATCH (m:Module {name: 'bridge', odoo_version: $v})"
                     " RETURN m.auto_install_triggers AS t", v=TEST_VERSION
                     ).single()["t"] == ["stock"]
        assert s.run("MATCH (m:Module {name: 'never_indexed'}) RETURN count(m) AS c"
                     ).single()["c"] == 0
    assert apply_version(neo4j_driver, TEST_VERSION, rows, dry_run=False)["changed"] == []


def test_a_node_indexed_before_the_property_falls_back_to_its_depends(seeded, neo4j_driver):
    """auto_install = true with no triggers (pre-backfill) = every DEPENDS_ON target."""
    with neo4j_driver.session() as s:
        s.run("MATCH (m:Module {name: 'bridge', odoo_version: $v})"
              " REMOVE m.auto_install_triggers", v=TEST_VERSION)
    out = seeded._module_dep_closure("stock", TEST_VERSION)
    assert "bridge (" not in out  # now needs sale AND stock
    out = seeded._module_dep_closure("sale_stock", TEST_VERSION)
    assert "bridge (triggered by: sale, stock)" in out
