# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_mcp_server.py
import asyncio
import os

import pytest

from src.indexer.models import FieldInfo, MethodInfo, ModelInfo, ModuleInfo, ParseResult
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j


@pytest.fixture(scope="module")
def seeded_neo4j(neo4j_driver):
    """Seed Neo4j with test data for MCP server tests."""
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=TEST_VERSION)

    base_mod = ModuleInfo("account", TEST_VERSION, "odoo_test", "/tmp", [], "")
    base_model = ModelInfo(
        name="account.move", module="account", odoo_version=TEST_VERSION,
        fields=[FieldInfo("name", "char", required=True),
                FieldInfo("amount_total", "float", compute="_compute_amount", stored=True)],
        methods=[MethodInfo("action_post", has_super_call=False)],
    )

    ext_mod = ModuleInfo("viin_account", TEST_VERSION, "acme_addons_test", "/tmp",
                          ["account"], "")
    ext_model = ModelInfo(
        name="account.move", module="viin_account", odoo_version=TEST_VERSION,
        inherit=["account.move"],
        fields=[FieldInfo("x_approval_state", "selection")],
        methods=[MethodInfo("action_post", has_super_call=True)],
    )

    writer.write_results([
        ParseResult(module=base_mod, models=[base_model]),
        ParseResult(module=ext_mod, models=[ext_model]),
    ])
    writer.close()
    yield
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=TEST_VERSION)


@pytest.fixture
def mcp_tools(seeded_neo4j):
    """Import MCP business logic functions after seeding data."""
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    import sys
    sys.modules.pop("src.mcp.server", None)
    from src.mcp.server import _resolve_field, _resolve_method, _resolve_model
    return _resolve_model, _resolve_field, _resolve_method


def test_resolve_model_found(mcp_tools):
    resolve_model, _, _ = mcp_tools
    result = resolve_model("account.move", TEST_VERSION)
    assert "account.move" in result
    assert TEST_VERSION in result


def test_resolve_model_shows_module(mcp_tools):
    resolve_model, _, _ = mcp_tools
    result = resolve_model("account.move", TEST_VERSION)
    assert "account" in result


def test_resolve_model_not_found(mcp_tools):
    resolve_model, _, _ = mcp_tools
    result = resolve_model("nonexistent.model", TEST_VERSION)
    assert "not found" in result


def test_resolve_field_found(mcp_tools):
    _, resolve_field, _ = mcp_tools
    result = resolve_field("account.move", "amount_total", TEST_VERSION)
    assert "amount_total" in result
    assert "float" in result.lower()


def test_resolve_field_shows_compute(mcp_tools):
    _, resolve_field, _ = mcp_tools
    result = resolve_field("account.move", "amount_total", TEST_VERSION)
    assert "_compute_amount" in result


def test_resolve_field_not_found(mcp_tools):
    _, resolve_field, _ = mcp_tools
    result = resolve_field("account.move", "nonexistent_field", TEST_VERSION)
    assert "not found" in result


def test_resolve_method_found(mcp_tools):
    _, _, resolve_method = mcp_tools
    result = resolve_method("account.move", "action_post", TEST_VERSION)
    assert "action_post" in result


def test_resolve_method_not_found(mcp_tools):
    _, _, resolve_method = mcp_tools
    result = resolve_method("account.move", "nonexistent_method", TEST_VERSION)
    assert "not found" in result


def test_resolve_model_excludes_unresolved_parents(neo4j_driver):
    """Unresolved parent (placeholder) must be filtered from 'Inherits from' output.

    Kept self-contained (NOT on ``ranking_seed``) on purpose: this test is
    collected high in the file, before the destructive
    ``test_latest_version_returns_none_when_db_empty`` (whole-Module wipe).  If
    it triggered the module-scoped ``ranking_seed`` setup here, that later wipe
    would corrupt the shared seed.  Its inline seed+wipe is cheap (1 module).
    """
    UNRESOLVED_VERSION = "98.0"
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=UNRESOLVED_VERSION)
    # sale.order inherits ghost.mixin (not indexed) → creates unresolved edge
    mod = ModuleInfo("sale", UNRESOLVED_VERSION, "odoo_test", "/tmp", [], "")
    model = ModelInfo(
        name="sale.order", module="sale", odoo_version=UNRESOLVED_VERSION,
        inherit=["ghost.mixin"],  # intentionally NOT seeded
    )
    writer.write_results([ParseResult(module=mod, models=[model])])
    writer.close()
    try:
        resolve_model = _make_ranking_tools(neo4j_driver)
        result = resolve_model("sale.order", UNRESOLVED_VERSION)
        assert "sale.order" in result
        assert "ghost.mixin" not in result  # unresolved parent filtered out
    finally:
        with neo4j_driver.session() as session:
            session.run(
                "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=UNRESOLVED_VERSION
            )


# --- resolve_view tests ---
from src.indexer.models import (  # noqa: E402,I001
    ViewInfo, ViewParseResult, XPathInfo,
)


@pytest.fixture(scope="module")
def seeded_views(neo4j_driver):
    """Seed Neo4j with view data for resolve_view tests."""
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    VIEW_VERSION = "97.0"  # dedicated version — avoids conflict with seeded_neo4j (99.0, 98.0)

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=VIEW_VERSION)

    base_mod = ModuleInfo("sale", VIEW_VERSION, "odoo_test", "/tmp", [], "")
    ext_mod = ModuleInfo("viin_sale", VIEW_VERSION, "tvtma_test", "/tmp", ["sale"], "")

    base_view = ViewInfo(
        xmlid="sale.view_sale_order_form",
        name="sale.order.form",
        model="sale.order",
        module="sale",
        odoo_version=VIEW_VERSION,
        view_type="form",
        mode="primary",
        inherit_xmlid=None,
    )
    ext_view = ViewInfo(
        xmlid="viin_sale.view_sale_order_form_inherit",
        name="viin sale form inherit",
        model="sale.order",
        module="viin_sale",
        odoo_version=VIEW_VERSION,
        view_type="form",
        mode="extension",
        inherit_xmlid="sale.view_sale_order_form",
        xpaths=[
            XPathInfo(expr="//field[@name='partner_id']", position="after"),
            XPathInfo(expr="//button[@name='action_confirm']", position="attributes"),
        ],
    )

    writer.write_view_results([
        ViewParseResult(module=base_mod, views=[base_view]),
        ViewParseResult(module=ext_mod, views=[ext_view]),
    ])
    writer.close()

    yield VIEW_VERSION

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=VIEW_VERSION)


@pytest.fixture
def view_tools(seeded_views):
    """Import _resolve_view after seeding data."""
    view_version = seeded_views
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    import sys
    sys.modules.pop("src.mcp.server", None)
    from src.mcp.server import _resolve_view
    return _resolve_view, view_version


def test_resolve_view_found(view_tools):
    resolve_view, version = view_tools
    result = resolve_view("sale.view_sale_order_form", version)
    assert "sale.view_sale_order_form" in result
    assert version in result
    assert "form" in result


def test_resolve_view_shows_model(view_tools):
    resolve_view, version = view_tools
    result = resolve_view("sale.view_sale_order_form", version)
    assert "sale.order" in result


def test_resolve_view_shows_extensions(view_tools):
    resolve_view, version = view_tools
    result = resolve_view("sale.view_sale_order_form", version)
    assert "viin_sale.view_sale_order_form_inherit" in result


def test_resolve_view_shows_xpaths(view_tools):
    resolve_view, version = view_tools
    result = resolve_view("sale.view_sale_order_form", version)
    assert "//field[@name='partner_id']" in result
    assert "after" in result


def test_resolve_view_extension_shows_parent(view_tools):
    resolve_view, version = view_tools
    result = resolve_view("viin_sale.view_sale_order_form_inherit", version)
    assert "sale.view_sale_order_form" in result
    assert "Inherits from" in result


def test_resolve_view_extension_shows_own_xpaths(view_tools):
    resolve_view, version = view_tools
    result = resolve_view("viin_sale.view_sale_order_form_inherit", version)
    assert "//field[@name='partner_id']" in result
    assert "//button[@name='action_confirm']" in result


def test_resolve_view_not_found(view_tools):
    resolve_view, version = view_tools
    result = resolve_view("nonexistent.view", version)
    assert "not found" in result


# --- _latest_version numeric compare tests (M4.5 WI1.3) -------------------

def test_latest_version_numeric_compare_picks_17_over_9(neo4j_driver):
    """DB has 9.0 + 17.0 → numeric compare returns 17.0, not 9.0 lexicographic."""
    from src.mcp.server import _latest_version

    LV_VERSION_LO = "9.0"
    LV_VERSION_HI = "17.0"
    with neo4j_driver.session() as session:
        # Cleanup
        for v in (LV_VERSION_LO, LV_VERSION_HI):
            session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=v)

        # Seed two Module nodes at numerically distant versions.
        session.run(
            "MERGE (m:Module {name: $n, odoo_version: $v})",
            n="base", v=LV_VERSION_LO,
        )
        session.run(
            "MERGE (m:Module {name: $n, odoo_version: $v})",
            n="base", v=LV_VERSION_HI,
        )

        # _latest_version filters out non-numeric and unknown — but here we
        # need it to ignore real production data. Filter by a marker via
        # property; alternative: only count modules from these two versions.
        # The function as implemented ranks across ALL modules, so we cannot
        # isolate test data without an extra filter. Instead assert the result
        # is not "9.0" given that data older than test 17.0 also exists in DB.
        result = _latest_version(session)
        # Result must be parseable as int(major).int(minor) and >= 17
        assert result is not None
        major = int(result.split(".")[0])
        assert major >= int(LV_VERSION_HI.split(".")[0]), (
            f"_latest_version returned {result!r}; expected numeric latest >= 17.0"
        )

        # Cleanup
        for v in (LV_VERSION_LO, LV_VERSION_HI):
            session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=v)


def test_latest_version_returns_none_when_db_empty(neo4j_driver):
    """All Module nodes deleted → _latest_version returns None (no hardcoded fallback)."""
    from src.mcp.server import _latest_version

    with neo4j_driver.session() as session:
        # Snapshot existing data, delete, run, restore.
        existing = session.run(
            "MATCH (m:Module) RETURN m.name AS name, m.odoo_version AS v"
        ).data()
        session.run("MATCH (m:Module) DETACH DELETE m")
        try:
            result = _latest_version(session)
            assert result is None, f"expected None on empty DB, got {result!r}"
        finally:
            for row in existing:
                session.run(
                    "MERGE (m:Module {name: $n, odoo_version: $v})",
                    n=row["name"], v=row["v"],
                )


def test_latest_version_skips_unknown_and_malformed(neo4j_driver):
    """Module nodes with odoo_version='unknown' or 'foo' are filtered out."""
    from src.mcp.server import _latest_version

    JUNK_VERSIONS = ["unknown", "foo", "abc"]
    GOOD_VERSION = "16.0"
    with neo4j_driver.session() as session:
        # Cleanup junk + good
        for v in JUNK_VERSIONS + [GOOD_VERSION]:
            session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=v)

        for v in JUNK_VERSIONS:
            session.run(
                "MERGE (m:Module {name: $n, odoo_version: $v})", n="weird", v=v,
            )
        session.run(
            "MERGE (m:Module {name: $n, odoo_version: $v})",
            n="good", v=GOOD_VERSION,
        )

        result = _latest_version(session)
        # Must skip junk strings; result must be a real semver-shaped version.
        assert result is not None
        assert result not in JUNK_VERSIONS, (
            f"_latest_version returned junk {result!r}"
        )
        # Format `<int>.<int>`
        parts = result.split(".")
        assert len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit()

        for v in JUNK_VERSIONS + [GOOD_VERSION]:
            session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=v)


# --- _resolve_model 4-tier ranking tests ------------------------------------


def _make_ranking_tools(neo4j_driver):
    """Return _resolve_model pointing at the test Neo4j."""
    import sys
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    sys.modules.pop("src.mcp.server", None)
    from src.mcp.server import _resolve_model
    return _resolve_model


# ---------------------------------------------------------------------------
# ADR-0013 ranking suite — seed-once module fixture (WS-C / DD1 §5a).
#
# The 11 ranking tests below (Cluster A model + Cluster B field/method) are
# pure READ-after-seed: each calls a resolver against a fixed graph and never
# mutates it post-seed.  They previously seeded + wiped their own version slot
# in every function body (11 seed+wipe cycles).  This fixture seeds ALL 11
# datasets ONCE at module setup and safety-wipes them ONCE at module teardown.
# Tests just read.
#
# NOTE: the Cluster-D ``excludes_unresolved`` test deliberately stays
# self-contained (NOT on this fixture).  It is collected ABOVE the destructive
# ``test_latest_version_returns_none_when_db_empty`` (whole-Module wipe); were
# it to set this module fixture up early, that wipe would corrupt the seed.
#
# Version slots are renumbered with a ``.1`` suffix where the legacy slot
# collided with a W6 constant once promoted to module scope (per DD1 §5a
# collision map: 86→86.1, 85→85.1, 84→84.1, 83→83.1, 88→88.1, 93→93.1).
# A guard test (test_ranking_versions_no_collision) enforces no overlap with
# any other module-scope version constant in this file.
# ---------------------------------------------------------------------------

_RANK_V = {
    "sixty_ext": "93.1",       # was 93.0 (collided with PROF_VERSION/seeded_views_with_profile)
    "orphan": "92.0",
    "edition": "91.0",
    "mixin_base": "86.1",      # was 86.0 (collided with W6_DESCRIBE_NO_MODELS_VERSION)
    "sub_mixin": "85.1",       # was 85.0 (collided with W6_DESCRIBE_VERSION)
    "transient": "84.1",       # was 84.0 (collided with W6_LIST_FIELDS_VERSION)
    "redeclare_mixin": "83.1",  # was 83.0 (collided with W6_LIST_METHODS_VERSION)
    "field_tie": "90.0",
    "field_redef": "89.0",
    "method_tie": "88.1",      # was 88.0 (collided with W6_EDITION_LABEL_VERSION)
    "method_redef": "87.0",
}


@pytest.fixture(scope="module")
def ranking_seed(neo4j_driver):
    """Seed all 11 ADR-0013 ranking datasets once; safety-wipe all once at end.

    Yields the ``_RANK_V`` version map.  Tests are read-only against this seed.
    Module-scope honours the seed-once contract; the teardown wipe (the
    ``finally``-equivalent here) keeps the before+after invariant at the
    fixture boundary — we never drop the post-seed wipe (M3 BLOCKER).
    """
    def _wipe(session):
        for v in _RANK_V.values():
            session.run(
                "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=v
            )

    with neo4j_driver.session() as session:
        _wipe(session)  # before: defensive clean of these slots

        # --- sixty_ext: base + 60 tied extensions ---
        v = _RANK_V["sixty_ext"]
        session.run(
            "MERGE (mod:Module {name: 'core', odoo_version: $v}) "
            "SET mod.repo = 'odoo_test', mod.edition = 'community'",
            v=v,
        )
        session.run(
            "MERGE (m:Model {name: 'sale.order', module: 'core', odoo_version: $v}) "
            "MERGE (mod:Module {name: 'core', odoo_version: $v}) "
            "MERGE (m)-[:DEFINED_IN]->(mod)",
            v=v,
        )
        for i in range(60):
            ext_mod = f"ext_{i:02d}"
            session.run(
                "MERGE (mod:Module {name: $mod, odoo_version: $v}) "
                "SET mod.repo = 'ext_repo', mod.edition = 'community'",
                mod=ext_mod, v=v,
            )
            session.run(
                "MERGE (ext:Model {name: 'sale.order', module: $mod, odoo_version: $v}) "
                "MERGE (base:Model {name: 'sale.order', module: 'core', odoo_version: $v}) "
                "MERGE (ext)-[:INHERITS]->(base) "
                "MERGE (extmod:Module {name: $mod, odoo_version: $v}) "
                "MERGE (ext)-[:DEFINED_IN]->(extmod)",
                mod=ext_mod, v=v,
            )

        # --- orphan: base is_definition=true beats orphan extension ---
        v = _RANK_V["orphan"]
        session.run(
            "MERGE (mod:Module {name: 'base_mod', odoo_version: $v}) "
            "SET mod.repo = 'odoo_test', mod.edition = 'community'",
            v=v,
        )
        session.run(
            "MERGE (m:Model {name: 'res.partner', module: 'base_mod', odoo_version: $v}) "
            "SET m.is_definition = true "
            "MERGE (mod:Module {name: 'base_mod', odoo_version: $v}) "
            "MERGE (m)-[:DEFINED_IN]->(mod)",
            v=v,
        )
        session.run(
            "MERGE (mod:Module {name: 'orphan_mod', odoo_version: $v}) "
            "SET mod.repo = 'ext_repo', mod.edition = 'community'",
            v=v,
        )
        session.run(
            "MERGE (m:Model {name: 'res.partner', module: 'orphan_mod', odoo_version: $v}) "
            "MERGE (mod:Module {name: 'orphan_mod', odoo_version: $v}) "
            "MERGE (m)-[:DEFINED_IN]->(mod)",
            v=v,
        )

        # --- edition: community < enterprise < custom ---
        v = _RANK_V["edition"]
        for mod_name, edition in [
            ("custom_mod", "customer"),
            ("enterprise_mod", "enterprise"),
            ("community_mod", "community"),
        ]:
            session.run(
                "MERGE (mod:Module {name: $mod, odoo_version: $v}) "
                "SET mod.repo = 'test_repo', mod.edition = $edition",
                mod=mod_name, v=v, edition=edition,
            )
            session.run(
                "MERGE (m:Model {name: 'mail.thread', module: $mod, odoo_version: $v}) "
                "MERGE (mod:Module {name: $mod, odoo_version: $v}) "
                "MERGE (m)-[:DEFINED_IN]->(mod)",
                mod=mod_name, v=v,
            )

        # --- mixin_base: abstract mixin is_definition=true is base ---
        v = _RANK_V["mixin_base"]
        session.run(
            "MERGE (mod:Module {name: 'mixin_core', odoo_version: $v}) "
            "SET mod.repo = 'test_repo', mod.edition = 'community'",
            v=v,
        )
        session.run(
            "MERGE (m:Model {name: 'test.mixin', module: 'mixin_core', odoo_version: $v}) "
            "SET m.is_definition = true, m.is_abstract = true "
            "MERGE (mod:Module {name: 'mixin_core', odoo_version: $v}) "
            "MERGE (m)-[:DEFINED_IN]->(mod)",
            v=v,
        )
        for i in range(5):
            consumer_name = f"consumer.model.{i}"
            consumer_mod = f"consumer_mod_{i}"
            session.run(
                "MERGE (mod:Module {name: $mod, odoo_version: $v}) "
                "SET mod.repo = 'consumer_repo', mod.edition = 'community'",
                mod=consumer_mod, v=v,
            )
            session.run(
                "MERGE (c:Model {name: $cname, module: $mod, odoo_version: $v}) "
                "SET c.is_definition = true "
                "MERGE (mx:Model {name: 'test.mixin', module: 'mixin_core', odoo_version: $v}) "
                "MERGE (c)-[:INHERITS]->(mx) "
                "MERGE (cmod:Module {name: $mod, odoo_version: $v}) "
                "MERGE (c)-[:DEFINED_IN]->(cmod)",
                cname=consumer_name, mod=consumer_mod, v=v,
            )

        # --- sub_mixin: _name != _inherit treated as own base ---
        v = _RANK_V["sub_mixin"]
        session.run(
            "MERGE (mod:Module {name: 'base_mixin_mod', odoo_version: $v}) "
            "SET mod.repo = 'test_repo', mod.edition = 'community'",
            v=v,
        )
        session.run(
            "MERGE (m:Model {name: 'base.mixin', module: 'base_mixin_mod', odoo_version: $v}) "
            "SET m.is_definition = true "
            "MERGE (mod:Module {name: 'base_mixin_mod', odoo_version: $v}) "
            "MERGE (m)-[:DEFINED_IN]->(mod)",
            v=v,
        )
        session.run(
            "MERGE (mod:Module {name: 'mixin_alpha_mod', odoo_version: $v}) "
            "SET mod.repo = 'test_repo', mod.edition = 'community'",
            v=v,
        )
        session.run(
            "MERGE (alpha:Model {name: 'mixin.alpha', module: 'mixin_alpha_mod', "
            "odoo_version: $v}) "
            "SET alpha.is_definition = true "
            "MERGE (parent:Model {name: 'base.mixin', module: 'base_mixin_mod', "
            "odoo_version: $v}) "
            "MERGE (alpha)-[:INHERITS]->(parent) "
            "MERGE (mod:Module {name: 'mixin_alpha_mod', odoo_version: $v}) "
            "MERGE (alpha)-[:DEFINED_IN]->(mod)",
            v=v,
        )

        # --- transient: single-node wizard resolves ---
        v = _RANK_V["transient"]
        session.run(
            "MERGE (mod:Module {name: 'wizard_mod', odoo_version: $v}) "
            "SET mod.repo = 'test_repo', mod.edition = 'community'",
            v=v,
        )
        session.run(
            "MERGE (m:Model {name: 'wizard.confirm', module: 'wizard_mod', odoo_version: $v}) "
            "SET m.is_transient = true, m.is_definition = true "
            "MERGE (mod:Module {name: 'wizard_mod', odoo_version: $v}) "
            "MERGE (m)-[:DEFINED_IN]->(mod)",
            v=v,
        )

        # --- redeclare_mixin: base beats redeclare w/ mixin injection ---
        v = _RANK_V["redeclare_mixin"]
        session.run(
            "MERGE (mod:Module {name: 'doc_base_mod', odoo_version: $v}) "
            "SET mod.repo = 'test_repo', mod.edition = 'community'",
            v=v,
        )
        session.run(
            "MERGE (m:Model {name: 'doc.order', module: 'doc_base_mod', odoo_version: $v}) "
            "SET m.is_definition = true "
            "MERGE (mod:Module {name: 'doc_base_mod', odoo_version: $v}) "
            "MERGE (m)-[:DEFINED_IN]->(mod)",
            v=v,
        )
        session.run(
            "MERGE (mod:Module {name: 'mail_mod', odoo_version: $v}) "
            "SET mod.repo = 'test_repo', mod.edition = 'community'",
            v=v,
        )
        session.run(
            "MERGE (mt:Model {name: 'mail.thread', module: 'mail_mod', odoo_version: $v}) "
            "SET mt.is_definition = true "
            "MERGE (mod:Module {name: 'mail_mod', odoo_version: $v}) "
            "MERGE (mt)-[:DEFINED_IN]->(mod)",
            v=v,
        )
        session.run(
            "MERGE (mod:Module {name: 'doc_mixin_mod', odoo_version: $v}) "
            "SET mod.repo = 'ext_repo', mod.edition = 'community'",
            v=v,
        )
        session.run(
            "MERGE (ext:Model {name: 'doc.order', module: 'doc_mixin_mod', odoo_version: $v}) "
            "SET ext.is_definition = false "
            "MERGE (base:Model {name: 'doc.order', module: 'doc_base_mod', odoo_version: $v}) "
            "MERGE (ext)-[:INHERITS]->(base) "
            "MERGE (mt:Model {name: 'mail.thread', module: 'mail_mod', odoo_version: $v}) "
            "MERGE (ext)-[:INHERITS]->(mt) "
            "MERGE (extmod:Module {name: 'doc_mixin_mod', odoo_version: $v}) "
            "MERGE (ext)-[:DEFINED_IN]->(extmod)",
            v=v,
        )

        # --- field_tie: base wins when 3 extensions tie ---
        v = _RANK_V["field_tie"]
        session.run(
            "MERGE (mod:Module {name: 'test_mod', odoo_version: $v}) "
            "SET mod.repo = 'test_repo', mod.edition = 'community'",
            v=v,
        )
        session.run(
            "MERGE (m:Model {name: 'test.order', module: 'test_mod', odoo_version: $v}) "
            "SET m.is_definition = true "
            "MERGE (mod:Module {name: 'test_mod', odoo_version: $v}) "
            "MERGE (m)-[:DEFINED_IN]->(mod)",
            v=v,
        )
        session.run(
            "MERGE (f:Field {name: 'state', model: 'test.order', "
            "module: 'test_mod', odoo_version: $v}) "
            "SET f.ttype = 'selection'",
            v=v,
        )
        for i in range(3):
            ext_mod = f"ext_mod_{i}"
            session.run(
                "MERGE (mod:Module {name: $mod, odoo_version: $v}) "
                "SET mod.repo = 'ext_repo', mod.edition = 'community'",
                mod=ext_mod, v=v,
            )
            session.run(
                "MERGE (ext:Model {name: 'test.order', module: $mod, odoo_version: $v}) "
                "MERGE (base:Model {name: 'test.order', module: 'test_mod', odoo_version: $v}) "
                "MERGE (ext)-[:INHERITS]->(base) "
                "MERGE (extmod:Module {name: $mod, odoo_version: $v}) "
                "MERGE (ext)-[:DEFINED_IN]->(extmod)",
                mod=ext_mod, v=v,
            )
            session.run(
                "MERGE (f:Field {name: 'state', model: 'test.order', "
                "module: $mod, odoo_version: $v}) "
                "SET f.ttype = 'selection'",
                mod=ext_mod, v=v,
            )

        # --- field_redef: base is_definition=true beats redeclare ---
        v = _RANK_V["field_redef"]
        session.run(
            "MERGE (mod:Module {name: 'base_field_mod', odoo_version: $v}) "
            "SET mod.repo = 'test_repo', mod.edition = 'community'",
            v=v,
        )
        session.run(
            "MERGE (m:Model {name: 'test.alpha', module: 'base_field_mod', odoo_version: $v}) "
            "SET m.is_definition = true "
            "MERGE (mod:Module {name: 'base_field_mod', odoo_version: $v}) "
            "MERGE (m)-[:DEFINED_IN]->(mod)",
            v=v,
        )
        session.run(
            "MERGE (f:Field {name: 'status', model: 'test.alpha', "
            "module: 'base_field_mod', odoo_version: $v}) "
            "SET f.ttype = 'char', f.required = true",
            v=v,
        )
        session.run(
            "MERGE (mod:Module {name: 'ext_field_mod', odoo_version: $v}) "
            "SET mod.repo = 'ext_repo', mod.edition = 'community'",
            v=v,
        )
        session.run(
            "MERGE (ext:Model {name: 'test.alpha', module: 'ext_field_mod', "
            "odoo_version: $v}) "
            "SET ext.is_definition = false "
            "MERGE (base:Model {name: 'test.alpha', "
            "module: 'base_field_mod', odoo_version: $v}) "
            "MERGE (ext)-[:INHERITS]->(base) "
            "MERGE (extmod:Module {name: 'ext_field_mod', odoo_version: $v}) "
            "MERGE (ext)-[:DEFINED_IN]->(extmod)",
            v=v,
        )
        session.run(
            "MERGE (f:Field {name: 'status', model: 'test.alpha', "
            "module: 'ext_field_mod', odoo_version: $v}) "
            "SET f.ttype = 'char'",
            v=v,
        )

        # --- method_tie: base wins when 3 extensions tie ---
        v = _RANK_V["method_tie"]
        session.run(
            "MERGE (mod:Module {name: 'test_mod_m', odoo_version: $v}) "
            "SET mod.repo = 'test_repo', mod.edition = 'community'",
            v=v,
        )
        session.run(
            "MERGE (m:Model {name: 'test.order', module: 'test_mod_m', odoo_version: $v}) "
            "SET m.is_definition = true "
            "MERGE (mod:Module {name: 'test_mod_m', odoo_version: $v}) "
            "MERGE (m)-[:DEFINED_IN]->(mod)",
            v=v,
        )
        session.run(
            "MERGE (mth:Method {name: 'action_confirm', model: 'test.order', "
            "module: 'test_mod_m', odoo_version: $v}) "
            "SET mth.has_super_call = false",
            v=v,
        )
        for i in range(3):
            ext_mod = f"ext_mod_m_{i}"
            session.run(
                "MERGE (mod:Module {name: $mod, odoo_version: $v}) "
                "SET mod.repo = 'ext_repo', mod.edition = 'community'",
                mod=ext_mod, v=v,
            )
            session.run(
                "MERGE (ext:Model {name: 'test.order', module: $mod, "
                "odoo_version: $v}) "
                "MERGE (base:Model {name: 'test.order', "
                "module: 'test_mod_m', odoo_version: $v}) "
                "MERGE (ext)-[:INHERITS]->(base) "
                "MERGE (extmod:Module {name: $mod, odoo_version: $v}) "
                "MERGE (ext)-[:DEFINED_IN]->(extmod)",
                mod=ext_mod, v=v,
            )
            session.run(
                "MERGE (mth:Method {name: 'action_confirm', model: 'test.order', "
                "module: $mod, odoo_version: $v}) "
                "SET mth.has_super_call = true",
                mod=ext_mod, v=v,
            )

        # --- method_redef: base is_definition=true beats redeclare ---
        v = _RANK_V["method_redef"]
        session.run(
            "MERGE (mod:Module {name: 'base_method_mod', odoo_version: $v}) "
            "SET mod.repo = 'test_repo', mod.edition = 'community'",
            v=v,
        )
        session.run(
            "MERGE (m:Model {name: 'test.beta', module: 'base_method_mod', odoo_version: $v}) "
            "SET m.is_definition = true "
            "MERGE (mod:Module {name: 'base_method_mod', odoo_version: $v}) "
            "MERGE (m)-[:DEFINED_IN]->(mod)",
            v=v,
        )
        session.run(
            "MERGE (mth:Method {name: 'do_something', model: 'test.beta', "
            "module: 'base_method_mod', odoo_version: $v}) "
            "SET mth.has_super_call = false",
            v=v,
        )
        session.run(
            "MERGE (mod:Module {name: 'ext_method_mod', odoo_version: $v}) "
            "SET mod.repo = 'ext_repo', mod.edition = 'community'",
            v=v,
        )
        session.run(
            "MERGE (ext:Model {name: 'test.beta', module: 'ext_method_mod', "
            "odoo_version: $v}) "
            "SET ext.is_definition = false "
            "MERGE (base:Model {name: 'test.beta', "
            "module: 'base_method_mod', odoo_version: $v}) "
            "MERGE (ext)-[:INHERITS]->(base) "
            "MERGE (extmod:Module {name: 'ext_method_mod', odoo_version: $v}) "
            "MERGE (ext)-[:DEFINED_IN]->(extmod)",
            v=v,
        )
        session.run(
            "MERGE (mth:Method {name: 'do_something', model: 'test.beta', "
            "module: 'ext_method_mod', odoo_version: $v}) "
            "SET mth.has_super_call = true",
            v=v,
        )

    yield _RANK_V

    with neo4j_driver.session() as session:
        _wipe(session)  # after: honour before+after invariant (M3)


def test_ranking_versions_no_collision():
    """Guard: renumbered ranking slots must not collide with any other
    module-scope version constant in this file (DD1 §8 mitigation)."""
    other_module_scope = {
        TEST_VERSION,           # 99.0 (seeded_neo4j)
        "97.0",                 # VIEW_VERSION (fixture-local in seeded_views)
        "93.0",                 # PROF_VERSION (fixture-local in seeded_views_with_profile)
        MULTI_EXT_VERSION, MULTI_MTH_VERSION,
        MULTI_VIEW_VERSION, W6_GRAMMAR_VERSION,
        W6_DESCRIBE_VERSION, W6_DESCRIBE_NO_MODELS_VERSION,
        W6_LIST_FIELDS_VERSION, W6_LIST_METHODS_VERSION, W6_LIST_VIEWS_VERSION,
        W6_LIST_OWL_VERSION, W6_LIST_QWEB_VERSION, W6_LIST_JS_VERSION,
        W6_EDITION_LABEL_VERSION, W6_LIST_FIELDS_PAGER_VERSION,
    }
    overlap = set(_RANK_V.values()) & other_module_scope
    assert not overlap, (
        f"Ranking version slots collide with other module-scope versions: {overlap}"
    )


def test_resolve_model_picks_base_when_60_extensions_tie_inbound(ranking_seed, neo4j_driver):
    """Base module wins when 60 extension Models all have inbound=1 (tie).

    Tier 1 (is_ext): extensions have outgoing INHERITS to base → is_ext=1.
    Base has no outgoing INHERITS to its own name → is_ext=0 → ranks first.
    Data seeded once by ``ranking_seed`` (slot ``sixty_ext``).
    """
    resolve_model = _make_ranking_tools(neo4j_driver)
    result = resolve_model("sale.order", ranking_seed["sixty_ext"])

    assert "Defined in:" in result
    first_defined_in_line = result.split("Defined in:")[1].split("\n")[0]
    assert "core" in first_defined_in_line, (
        f"Expected 'core' as Defined-in module; got:\n{result}"
    )


def test_resolve_model_picks_base_when_extension_orphan_no_outgoing_edge(
    ranking_seed, neo4j_driver
):
    """Base with is_definition=true beats an orphan extension with no INHERITS edge.

    Simulates parser-miss: extension Model node exists but has no outgoing INHERITS.
    Tier 1: base has is_definition=true → is_ext=0 via CASE 1.
    Orphan: no outgoing INHERITS to same-name node → ELSE 0 as well, tie at is_ext.
    Tier 4 (mod_name): 'base_mod' < 'orphan_mod' alphabetically → base wins.
    Data seeded once by ``ranking_seed`` (slot ``orphan``).
    """
    resolve_model = _make_ranking_tools(neo4j_driver)
    result = resolve_model("res.partner", ranking_seed["orphan"])

    assert "Defined in:" in result
    first_defined_in_line = result.split("Defined in:")[1].split("\n")[0]
    assert "base_mod" in first_defined_in_line, (
        f"Expected 'base_mod' as Defined-in; got:\n{result}"
    )


def test_resolve_model_edition_rank_orders_community_then_enterprise_then_custom(
    ranking_seed, neo4j_driver
):
    """Edition rank: community (0) < enterprise (1) < custom/unknown (4).

    Three Model nodes same name, same inbound=0, same is_ext=0 (no outgoing INHERITS).
    Differs only in Module.edition → edition_rank decides order.
    community module must appear as Defined-in (first), enterprise and custom in Extended-by.
    Data seeded once by ``ranking_seed`` (slot ``edition``).
    """
    resolve_model = _make_ranking_tools(neo4j_driver)
    result = resolve_model("mail.thread", ranking_seed["edition"])

    assert "Defined in:" in result
    first_defined_in_line = result.split("Defined in:")[1].split("\n")[0]
    assert "community_mod" in first_defined_in_line, (
        f"Expected 'community_mod' (community edition) as Defined-in; got:\n{result}"
    )
    # enterprise_mod must appear before custom_mod in the output
    assert result.index("enterprise_mod") < result.index("custom_mod"), (
        f"Expected enterprise_mod before custom_mod in Extended-by; got:\n{result}"
    )


def test_resolve_model_abstract_mixin_is_base(ranking_seed, neo4j_driver):
    """Mixin model with is_definition=true is correctly identified as Defined-in.

    Synthetic mixin 'test.mixin' has 1 Model node (is_definition=true) and 5
    consumer models that inherit from it under different model names.
    The mixin itself has no INHERITS edge going outward to *its own* name, so
    is_ext=0 → it ranks as the definition even though it has many inbound edges.
    Data seeded once by ``ranking_seed`` (slot ``mixin_base``).
    """
    resolve_model = _make_ranking_tools(neo4j_driver)
    result = resolve_model("test.mixin", ranking_seed["mixin_base"])

    assert "Defined in:" in result
    first_defined_in_line = result.split("Defined in:")[1].split("\n")[0]
    assert "mixin_core" in first_defined_in_line, (
        f"Expected 'mixin_core' as Defined-in for mixin model; got:\n{result}"
    )


def test_resolve_model_sub_mixin_with_different_name(ranking_seed, neo4j_driver):
    """Sub-mixin with _name != _inherit is treated as a new base definition.

    'mixin.alpha' has _name='mixin.alpha' and _inherit='base.mixin' (different names).
    Because the INHERITS edge goes to 'base.mixin' (a different model name), the
    is_ext heuristic treats 'mixin.alpha' as is_ext=0 → it is its own base.
    Assert Defined-in is 'mixin_alpha_mod', not 'base_mixin_mod'.
    Data seeded once by ``ranking_seed`` (slot ``sub_mixin``).
    """
    resolve_model = _make_ranking_tools(neo4j_driver)
    result = resolve_model("mixin.alpha", ranking_seed["sub_mixin"])

    assert "Defined in:" in result
    first_defined_in_line = result.split("Defined in:")[1].split("\n")[0]
    assert "mixin_alpha_mod" in first_defined_in_line, (
        f"Expected 'mixin_alpha_mod' as Defined-in for sub-mixin; got:\n{result}"
    )


def test_resolve_model_transient_wizard_single_node(ranking_seed, neo4j_driver):
    """Transient wizard with a single node resolves without error.

    A wizard model (is_transient=true) with exactly 1 Model node and no INHERITS
    edges. The resolver must return a valid result (no crash, no 'not found') and
    correctly identify that single node as Defined-in.
    Data seeded once by ``ranking_seed`` (slot ``transient``).
    """
    resolve_model = _make_ranking_tools(neo4j_driver)
    result = resolve_model("wizard.confirm", ranking_seed["transient"])

    assert "not found" not in result.lower(), (
        f"Single-node transient model should resolve; got:\n{result}"
    )
    assert "wizard.confirm" in result
    assert "Defined in:" in result
    first_defined_in_line = result.split("Defined in:")[1].split("\n")[0]
    assert "wizard_mod" in first_defined_in_line, (
        f"Expected 'wizard_mod' as Defined-in; got:\n{result}"
    )


def test_resolve_model_redeclare_with_mixin_injection(ranking_seed, neo4j_driver):
    """Redeclare pattern (_name=X, _inherit=[X, mail.thread]) is ranked as extension.

    The redeclare module has both:
      - An INHERITS edge to 'doc.order' (same name → is_ext=1 via CASE 2)
      - An INHERITS edge to 'mail.thread' (mixin injection, different name)
    The base module (is_definition=true) must win Defined-in over the redeclare module.
    Data seeded once by ``ranking_seed`` (slot ``redeclare_mixin``).
    """
    resolve_model = _make_ranking_tools(neo4j_driver)
    result = resolve_model("doc.order", ranking_seed["redeclare_mixin"])

    assert "Defined in:" in result
    first_defined_in_line = result.split("Defined in:")[1].split("\n")[0]
    assert "doc_base_mod" in first_defined_in_line, (
        f"Expected 'doc_base_mod' (base) as Defined-in; "
        f"redeclare module must not win; got:\n{result}"
    )
    # The redeclare module must appear in Extended-by (not Defined-in)
    assert "doc_mixin_mod" in result, (
        f"Expected redeclare module 'doc_mixin_mod' somewhere in output; got:\n{result}"
    )


# --- _resolve_field 4-tier ranking tests ------

def _make_field_tools(neo4j_driver):
    """Return _resolve_field pointing at the test Neo4j."""
    import sys
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    sys.modules.pop("src.mcp.server", None)
    from src.mcp.server import _resolve_field
    return _resolve_field


def test_resolve_field_picks_base_module_when_extensions_tie(ranking_seed, neo4j_driver):
    """Base module wins when field exists in base + 3 extensions with same inbound.

    Tier 1 (is_ext): extensions have outgoing INHERITS to base model → is_ext=1.
    Base has is_definition=true or no outgoing INHERITS → is_ext=0 → ranks first.
    Data seeded once by ``ranking_seed`` (slot ``field_tie``).
    """
    resolve_field = _make_field_tools(neo4j_driver)
    result = resolve_field("test.order", "state", ranking_seed["field_tie"])

    assert "Declared in:" in result
    # First declared module should be test_mod (base)
    lines = result.split("\n")
    declared_section = False
    first_declared_module = None
    for line in lines:
        if "Declared in:" in line:
            declared_section = True
            continue
        if declared_section and "test_mod" in line:
            first_declared_module = "test_mod"
            break

    assert first_declared_module == "test_mod", (
        f"Expected 'test_mod' as first Declared-in module; got:\n{result}"
    )


def test_resolve_field_redeclare_extension_demoted(ranking_seed, neo4j_driver):
    """Base with is_definition=true beats extension redeclare with is_definition=false.

    Base model has is_definition=true; extension redeclares same field with is_definition=false.
    Tier 1: base is_ext=0, extension is_ext=1 → base wins.
    Data seeded once by ``ranking_seed`` (slot ``field_redef``).
    """
    resolve_field = _make_field_tools(neo4j_driver)
    result = resolve_field("test.alpha", "status", ranking_seed["field_redef"])

    assert "Declared in:" in result
    lines = result.split("\n")
    declared_section = False
    first_declared_module = None
    for line in lines:
        if "Declared in:" in line:
            declared_section = True
            continue
        if declared_section and "base_field_mod" in line:
            first_declared_module = "base_field_mod"
            break

    assert first_declared_module == "base_field_mod", (
        f"Expected 'base_field_mod' as first Declared-in module; got:\n{result}"
    )


# --- _resolve_method 4-tier ranking tests ------

def _make_method_tools(neo4j_driver):
    """Return _resolve_method pointing at the test Neo4j."""
    import sys
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    sys.modules.pop("src.mcp.server", None)
    from src.mcp.server import _resolve_method
    return _resolve_method


def test_resolve_method_picks_base_module_when_extensions_tie(ranking_seed, neo4j_driver):
    """Base module wins when method exists in base + 3 extensions with same inbound.

    Tier 1 (is_ext): extensions have outgoing INHERITS to base model → is_ext=1.
    Base has is_definition=true or no outgoing INHERITS → is_ext=0 → ranks first.
    Data seeded once by ``ranking_seed`` (slot ``method_tie``).
    """
    resolve_method = _make_method_tools(neo4j_driver)
    result = resolve_method("test.order", "action_confirm", ranking_seed["method_tie"])

    assert "Override chain" in result
    # First method in override chain should be from test_mod_m (base)
    lines = result.split("\n")
    # Skip the header line "Override chain (N):", take the first actual entry
    for line in lines[1:]:
        if "test_mod_m" in line:
            # This should be the first occurrence
            assert "test_mod_m" in lines[2], (
                f"Expected 'test_mod_m' as first in Override chain; got:\n{result}"
            )
            break


def test_resolve_method_redeclare_extension_demoted(ranking_seed, neo4j_driver):
    """Base with is_definition=true beats extension redeclare with is_definition=false.

    Base model has is_definition=true; extension redeclares same method with is_definition=false.
    Tier 1: base is_ext=0, extension is_ext=1 → base wins in Override chain.
    Data seeded once by ``ranking_seed`` (slot ``method_redef``).
    """
    resolve_method = _make_method_tools(neo4j_driver)
    result = resolve_method("test.beta", "do_something", ranking_seed["method_redef"])

    assert "Override chain" in result
    # First method in override chain should be from base_method_mod
    lines = result.split("\n")
    for line in lines[1:]:
        if "base_method_mod" in line:
            assert "base_method_mod" in lines[2], (
                f"Expected 'base_method_mod' as first in Override chain; got:\n{result}"
            )
            break
# ---------------------------------------------------------------------------
# Regression tests for PR #26 (fix/resolve-output-polish)
# Covers: DISTINCT dedup on parent names, tree-format ├─/└─ connectors
# ---------------------------------------------------------------------------

MULTI_EXT_VERSION = "96.0"  # purchase.order with ≥2 extensions + mail.thread mixin
MULTI_MTH_VERSION = "95.0"  # account.move with 3 action_post overrides
MULTI_VIEW_VERSION = "94.0"  # sale view with 2 extension views


@pytest.fixture(scope="module")
def seeded_multi_extension(neo4j_driver):
    """Seed purchase.order across 3 modules; both extensions inherit mail.thread.

    This creates 2 INHERITS edges pointing to mail.mail.thread from different
    purchase.order nodes — the DISTINCT dedup fix prevents duplicate parents.
    It also creates ≥2 entries in the 'Extended by' block to exercise ├─/└─.
    """
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=MULTI_EXT_VERSION)

    mail_mod = ModuleInfo("mail", MULTI_EXT_VERSION, "odoo_test", "/tmp", [], "")
    mail_model = ModelInfo(name="mail.thread", module="mail", odoo_version=MULTI_EXT_VERSION)

    base_mod = ModuleInfo("purchase", MULTI_EXT_VERSION, "odoo_test", "/tmp", [], "")
    base_model = ModelInfo(
        name="purchase.order", module="purchase", odoo_version=MULTI_EXT_VERSION,
        fields=[FieldInfo("name", "char")],
    )

    ext1_mod = ModuleInfo("viin_purchase", MULTI_EXT_VERSION, "acme_addons_test", "/tmp",
                          ["purchase"], "")
    ext1_model = ModelInfo(
        name="purchase.order", module="viin_purchase", odoo_version=MULTI_EXT_VERSION,
        inherit=["purchase.order", "mail.thread"],
        fields=[FieldInfo("x_approval_state", "selection")],
    )

    ext2_mod = ModuleInfo("custom_purchase", MULTI_EXT_VERSION, "custom_test", "/tmp",
                          ["purchase"], "")
    ext2_model = ModelInfo(
        name="purchase.order", module="custom_purchase", odoo_version=MULTI_EXT_VERSION,
        inherit=["purchase.order", "mail.thread"],
        fields=[FieldInfo("x_custom_ref", "char")],
    )

    writer.write_results([
        ParseResult(module=mail_mod, models=[mail_model]),
        ParseResult(module=base_mod, models=[base_model]),
        ParseResult(module=ext1_mod, models=[ext1_model]),
        ParseResult(module=ext2_mod, models=[ext2_model]),
    ])
    writer.close()
    yield MULTI_EXT_VERSION

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=MULTI_EXT_VERSION)


@pytest.fixture(scope="module")
def seeded_multi_method(neo4j_driver):
    """Seed account.move with action_post in 3 modules to test Override chain connectors."""
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=MULTI_MTH_VERSION)

    base_mod = ModuleInfo("account", MULTI_MTH_VERSION, "odoo_test", "/tmp", [], "")
    base_model = ModelInfo(
        name="account.move", module="account", odoo_version=MULTI_MTH_VERSION,
        methods=[MethodInfo("action_post", has_super_call=False)],
    )

    ext1_mod = ModuleInfo("viin_account", MULTI_MTH_VERSION, "acme_addons_test", "/tmp",
                          ["account"], "")
    ext1_model = ModelInfo(
        name="account.move", module="viin_account", odoo_version=MULTI_MTH_VERSION,
        inherit=["account.move"],
        methods=[MethodInfo("action_post", has_super_call=True)],
    )

    ext2_mod = ModuleInfo("custom_account", MULTI_MTH_VERSION, "custom_test", "/tmp",
                          ["account"], "")
    ext2_model = ModelInfo(
        name="account.move", module="custom_account", odoo_version=MULTI_MTH_VERSION,
        inherit=["account.move"],
        methods=[MethodInfo("action_post", has_super_call=True)],
    )

    writer.write_results([
        ParseResult(module=base_mod, models=[base_model]),
        ParseResult(module=ext1_mod, models=[ext1_model]),
        ParseResult(module=ext2_mod, models=[ext2_model]),
    ])
    writer.close()
    yield MULTI_MTH_VERSION

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=MULTI_MTH_VERSION)


@pytest.fixture(scope="module")
def seeded_multi_view_ext(neo4j_driver):
    """Seed sale view with 2 extension views to test Extended-by ├─/└─ connectors."""
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=MULTI_VIEW_VERSION)

    base_mod = ModuleInfo("sale", MULTI_VIEW_VERSION, "odoo_test", "/tmp", [], "")
    ext1_mod = ModuleInfo("viin_sale", MULTI_VIEW_VERSION, "acme_addons_test", "/tmp",
                          ["sale"], "")
    ext2_mod = ModuleInfo("custom_sale", MULTI_VIEW_VERSION, "custom_test", "/tmp",
                          ["sale"], "")

    base_view = ViewInfo(
        xmlid="sale.view_sale_order_form",
        name="sale.order.form",
        model="sale.order",
        module="sale",
        odoo_version=MULTI_VIEW_VERSION,
        view_type="form",
        mode="primary",
        inherit_xmlid=None,
    )
    ext1_view = ViewInfo(
        xmlid="viin_sale.view_inherit_1",
        name="viin sale inherit 1",
        model="sale.order",
        module="viin_sale",
        odoo_version=MULTI_VIEW_VERSION,
        view_type="form",
        mode="extension",
        inherit_xmlid="sale.view_sale_order_form",
        xpaths=[XPathInfo(expr="//field[@name='partner_id']", position="after")],
    )
    ext2_view = ViewInfo(
        xmlid="custom_sale.view_inherit_2",
        name="custom sale inherit 2",
        model="sale.order",
        module="custom_sale",
        odoo_version=MULTI_VIEW_VERSION,
        view_type="form",
        mode="extension",
        inherit_xmlid="sale.view_sale_order_form",
        xpaths=[XPathInfo(expr="//field[@name='amount_total']", position="before")],
    )

    writer.write_view_results([
        ViewParseResult(module=base_mod, views=[base_view]),
        ViewParseResult(module=ext1_mod, views=[ext1_view]),
        ViewParseResult(module=ext2_mod, views=[ext2_view]),
    ])
    writer.close()
    yield MULTI_VIEW_VERSION

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=MULTI_VIEW_VERSION)


@pytest.fixture
def multi_ext_tools(seeded_multi_extension):
    version = seeded_multi_extension
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    import sys
    sys.modules.pop("src.mcp.server", None)
    from src.mcp.server import _resolve_model
    return _resolve_model, version


@pytest.fixture
def multi_mth_tools(seeded_multi_method):
    version = seeded_multi_method
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    import sys
    sys.modules.pop("src.mcp.server", None)
    from src.mcp.server import _resolve_method
    return _resolve_method, version


@pytest.fixture
def multi_view_tools(seeded_multi_view_ext):
    version = seeded_multi_view_ext
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    import sys
    sys.modules.pop("src.mcp.server", None)
    from src.mcp.server import _resolve_view
    return _resolve_view, version


def test_resolve_model_dedup_inherited_parents(multi_ext_tools):
    """Two extensions both inherit mail.thread → parent appears exactly once (DISTINCT fix)."""
    resolve_model, version = multi_ext_tools
    result = resolve_model("purchase.order", version)
    assert "mail.thread" in result, "mail.thread should be listed as parent"
    assert result.count("mail.thread") == 1, (
        f"mail.thread must appear once (DISTINCT dedup), got:\n{result}"
    )


def test_resolve_model_extended_by_tree_format(multi_ext_tools):
    """Model with ≥2 extensions uses ├─ for non-last entries and └─ for the last."""
    resolve_model, version = multi_ext_tools
    result = resolve_model("purchase.order", version)
    assert "Extended by:" in result

    lines = result.splitlines()
    ext_start = next(i for i, line in enumerate(lines) if "Extended by:" in line)
    ext_block = [line for line in lines[ext_start + 1:] if line.startswith("│   ")]

    assert len(ext_block) >= 2, (
        f"Expected ≥2 extensions in 'Extended by' block, got {len(ext_block)}:\n{result}"
    )
    assert "└─" in ext_block[-1], f"Last extension must use └─:\n{ext_block[-1]}"
    assert all("├─" in line for line in ext_block[:-1]), (
        f"Non-last extensions must use ├─:\n{ext_block[:-1]}"
    )


def test_resolve_method_override_chain_tree_format(multi_mth_tools):
    """Method with 3 overrides: first two use ├─, last uses └─ in Override chain.

    Wave 5 (ADR-0023 §1.3 + §4): Override chain parent demoted to ``├─`` (the
    new last branch is the ``└─ Next:`` footer). Sublist indent therefore uses
    ``│   `` (pipe + 3 spaces) rather than the prior flat ``    `` indent so
    the vertical line continues past the sublist to the Next: footer.
    """
    resolve_method, version = multi_mth_tools
    result = resolve_method("account.move", "action_post", version)
    assert "Override chain (3)" in result, f"Expected 3-override chain:\n{result}"

    lines = result.splitlines()
    chain_start = next(i for i, line in enumerate(lines) if "Override chain" in line)
    chain_lines = [line for line in lines[chain_start + 1:] if line.startswith("│   ")]

    assert len(chain_lines) == 3, (
        f"Expected 3 override entries, got {len(chain_lines)}:\n{result}"
    )
    assert "└─" in chain_lines[-1], f"Last override must use └─:\n{chain_lines[-1]}"
    assert all("├─" in line for line in chain_lines[:-1]), (
        f"Non-last overrides must use ├─:\n{chain_lines[:-1]}"
    )
    # Wave 5 ADR-0023 §4: drill-down tools terminate with a Next: footer.
    assert lines[-1].startswith("└─ Next:"), (
        f"resolve_method must end with '└─ Next:' footer, got:\n{lines[-1]}"
    )


def test_resolve_view_extended_by_tree_format(multi_view_tools):
    """View with 2 extensions uses ├─ for first entry and └─ for the last."""
    resolve_view, version = multi_view_tools
    result = resolve_view("sale.view_sale_order_form", version)
    assert "Extended by (2 modules)" in result, f"Expected 2-extension block:\n{result}"

    lines = result.splitlines()
    ext_lines = [line for line in lines if "view_inherit" in line]

    assert len(ext_lines) == 2, (
        f"Expected 2 extension lines containing 'view_inherit', got {len(ext_lines)}:\n{result}"
    )
    assert "└─" in ext_lines[-1], f"Last extension must use └─:\n{ext_lines[-1]}"
    assert "├─" in ext_lines[0], f"First extension must use ├─:\n{ext_lines[0]}"


# --- profile_name filter tests for resolve_view ---


@pytest.fixture(scope="module")
def seeded_views_with_profile(neo4j_driver):
    """Seed View nodes with distinct profile arrays for profile_name filter tests."""
    from src.indexer.models import (
        ModuleInfo,
        ViewInfo,
        ViewParseResult,
    )
    from src.indexer.writer_neo4j import Neo4jWriter

    PROF_VERSION = "93.0"  # distinct — avoids collision with other suites

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=PROF_VERSION)

    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    # Module in profile "alpha_93"; view belongs to alpha_93 profile
    mod_alpha = ModuleInfo("mod_alpha", PROF_VERSION, "repo_alpha", "/tmp", [], "")
    view_alpha = ViewInfo(
        xmlid="mod_alpha.view_alpha_form",
        name="alpha form",
        model="alpha.model",
        module="mod_alpha",
        odoo_version=PROF_VERSION,
        view_type="form",
        mode="primary",
        inherit_xmlid=None,
    )
    writer.write_view_results(
        [ViewParseResult(module=mod_alpha, views=[view_alpha])],
        profiles=["alpha_93"],
    )

    # Module in profile "beta_93"; separate view
    mod_beta = ModuleInfo("mod_beta", PROF_VERSION, "repo_beta", "/tmp", [], "")
    view_beta = ViewInfo(
        xmlid="mod_beta.view_beta_form",
        name="beta form",
        model="beta.model",
        module="mod_beta",
        odoo_version=PROF_VERSION,
        view_type="form",
        mode="primary",
        inherit_xmlid=None,
    )
    writer.write_view_results(
        [ViewParseResult(module=mod_beta, views=[view_beta])],
        profiles=["beta_93"],
    )

    writer.close()
    yield PROF_VERSION

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=PROF_VERSION)


@pytest.fixture
def view_profile_tools(seeded_views_with_profile):
    """Import _resolve_view for profile filter tests."""
    ver = seeded_views_with_profile
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    import sys
    sys.modules.pop("src.mcp.server", None)
    from src.mcp.server import _resolve_view
    return _resolve_view, ver


def test_resolve_view_profile_none_returns_all(view_profile_tools):
    """profile_name=None (default) returns views from all profiles (backward compat)."""
    resolve_view, ver = view_profile_tools
    result_alpha = resolve_view("mod_alpha.view_alpha_form", ver, profile_name=None)
    result_beta = resolve_view("mod_beta.view_beta_form", ver, profile_name=None)
    assert "mod_alpha.view_alpha_form" in result_alpha
    assert "mod_beta.view_beta_form" in result_beta


def test_resolve_view_profile_name_narrows_non_escalating_for_admin(view_profile_tools):
    """WG-3t T3 (ADR-0034): profile_name is a NON-ESCALATING narrowing filter,
    consistent across the Neo4j and pgvector paths (fixes the split-brain).

    Pre-WG-3t the Neo4j path treated admin's profile_name as advisory (the alpha view
    stayed visible when asking under 'beta_93') while pgvector narrowed — a split-brain.
    Under T3 BOTH paths narrow: admin asking for 'beta_93' narrows to that profile, so
    the alpha view (under 'alpha_93') is NOT found, while the beta view still is. The
    tenant boundary remains the isolation guarantee (test_cross_tenant_isolation).
    """
    resolve_view, ver = view_profile_tools
    result_beta = resolve_view("mod_beta.view_beta_form", ver, profile_name="beta_93")
    result_alpha = resolve_view("mod_alpha.view_alpha_form", ver, profile_name="beta_93")
    # matching profile still surfaces its view (rendered detail, not just the echoed id).
    assert "beta form" in result_beta, result_beta
    # non-matching profile is narrowed away → not-found message (strong assertion: the
    # rendered view detail 'alpha form' must be ABSENT, since the id is echoed in the
    # not-found text).
    assert "not found" in result_alpha.lower(), result_alpha
    assert "alpha form" not in result_alpha, result_alpha


# ===========================================================================
# Wave 6 (ADR-0023) — tests for the 7 new tools + grammar / footer / language
# policy enforcement. Each new tool gets happy/empty/truncation coverage; the
# grammar test runs against all 21 tools; the language-policy test parses
# server.py via ast and asserts no Vietnamese diacritics in static template
# strings (docstrings exempt).
# ===========================================================================

from tests.conftest import (  # noqa: E402,I001
    seed_js_patches, seed_owl_components, seed_qweb_templates, seed_stylesheets,
)


def _import_server_module():
    """Re-import src.mcp.server with NEO4J_* pointing at the test Neo4j."""
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    import sys
    sys.modules.pop("src.mcp.server", None)
    import src.mcp.server as srv  # noqa: PLC0415
    return srv


# --- per-test version slots — keep distinct from existing suite versions ----

W6_DESCRIBE_VERSION = "85.0"
W6_DESCRIBE_NO_MODELS_VERSION = "86.0"
W6_LIST_FIELDS_VERSION = "84.0"
W6_LIST_METHODS_VERSION = "83.0"
W6_LIST_VIEWS_VERSION = "82.0"
W6_LIST_OWL_VERSION = "81.0"
W6_LIST_OWL_LEGACY_VERSION = "13.99"  # era guard sentinel (major=13 ≤ 13 fires guard)
W6_LIST_QWEB_VERSION = "80.0"
W6_LIST_JS_VERSION = "79.0"
W6_GRAMMAR_VERSION = "78.0"
W6_FOOTER_VERSION = "77.0"


def _cleanup_version(driver, version: str) -> None:
    with driver.session() as session:
        session.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=version,
        )


# --- describe_module --------------------------------------------------------


def test_describe_module_happy(neo4j_driver):
    """Module with manifest + defined model + extended model + JS patch."""
    _cleanup_version(neo4j_driver, W6_DESCRIBE_VERSION)
    try:
        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        writer.setup_indexes()
        viin = ModuleInfo(
            "viin_sale", W6_DESCRIBE_VERSION, "viindoo", "/tmp", ["sale"],
            "17.0.1.0.0",
        )
        viin.edition = "viindoo"
        sale_model = ModelInfo(
            name="sale.report.custom", module="viin_sale",
            odoo_version=W6_DESCRIBE_VERSION,
            fields=[FieldInfo("name", "char")],
        )
        sale_model.had_explicit_name = True
        # Extension model (had_explicit_name=False by default + inherit list)
        # → writer sets is_definition=false → appears under "Extends models:".
        ext_model = ModelInfo(
            name="sale.order", module="viin_sale",
            odoo_version=W6_DESCRIBE_VERSION,
            inherit=["sale.order"],
            fields=[FieldInfo("x_viin_field", "char")],
        )
        # had_explicit_name defaults to False — no override needed.
        writer.write_results([ParseResult(module=viin, models=[sale_model, ext_model])])
        # is_definition flag is needed for "Defines models" Cypher in
        # _describe_module — set it explicitly for the definition model only.
        with neo4j_driver.session() as session:
            session.run(
                "MATCH (m:Model {name:'sale.report.custom', module:'viin_sale',"
                "                odoo_version:$v}) SET m.is_definition = true",
                v=W6_DESCRIBE_VERSION,
            )
            # Ensure extension model explicitly has is_definition=false.
            session.run(
                "MATCH (m:Model {name:'sale.order', module:'viin_sale',"
                "                odoo_version:$v}) SET m.is_definition = false",
                v=W6_DESCRIBE_VERSION,
            )
        writer.close()

        srv = _import_server_module()
        out = srv._describe_module("viin_sale", W6_DESCRIBE_VERSION)
        assert out.startswith(f"viin_sale (Odoo {W6_DESCRIBE_VERSION})")
        assert "├─ Manifest:" in out
        assert "Depends:" in out
        assert "├─ Defines models:" in out
        assert "sale.report.custom" in out
        assert "├─ Extends models:" in out
        assert "sale.order" in out
        assert "├─ JS patches:" in out
        assert out.rstrip().splitlines()[-1].startswith("└─ Next:")
    finally:
        _cleanup_version(neo4j_driver, W6_DESCRIBE_VERSION)


def test_describe_module_empty(neo4j_driver):
    """Unknown module → English error string (ADR-0023 §2)."""
    _cleanup_version(neo4j_driver, W6_DESCRIBE_VERSION)
    try:
        srv = _import_server_module()
        out = srv._describe_module("no_such_module", W6_DESCRIBE_VERSION)
        assert "No module named 'no_such_module'" in out
        assert W6_DESCRIBE_VERSION in out
    finally:
        _cleanup_version(neo4j_driver, W6_DESCRIBE_VERSION)


def test_describe_module_truncation(neo4j_driver):
    """≥21 defined models → inline preview shows '... and K more (use list_fields(...))'."""
    _cleanup_version(neo4j_driver, W6_DESCRIBE_VERSION)
    try:
        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        writer.setup_indexes()
        mega_mod = ModuleInfo(
            "mega_mod", W6_DESCRIBE_VERSION, "test_repo", "/tmp", [], "17.0",
        )
        # describe_module uses LIST_PREVIEW_MAX_ITEMS=20 cap — seed 22 models so cap fires.
        models = [
            ModelInfo(
                name=f"mega.model.{i:02d}", module="mega_mod",
                odoo_version=W6_DESCRIBE_VERSION,
                fields=[FieldInfo("name", "char")],
            )
            for i in range(22)
        ]
        writer.write_results([ParseResult(module=mega_mod, models=models)])
        with neo4j_driver.session() as session:
            session.run(
                "MATCH (m:Model {module:'mega_mod', odoo_version:$v}) "
                "SET m.is_definition = true",
                v=W6_DESCRIBE_VERSION,
            )
        writer.close()

        srv = _import_server_module()
        out = srv._describe_module("mega_mod", W6_DESCRIBE_VERSION)
        assert "Defines models: 22" in out
        # describe_module inlines top-20 with "... and K more (use list_fields(...))" tail.
        assert "and 2 more" in out
        assert "use model_inspect(" in out
    finally:
        _cleanup_version(neo4j_driver, W6_DESCRIBE_VERSION)


def test_describe_module_no_models_skips_footer(neo4j_driver):
    """ADR-0023 §4.4: describe_module with zero models defined/extended emits no Next: footer.

    When a module has 0 defined models AND 0 extended models the server should
    not emit the drill-down Next: hint, because there is nothing to drill into.
    """
    _cleanup_version(neo4j_driver, W6_DESCRIBE_NO_MODELS_VERSION)
    try:
        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        writer.setup_indexes()
        # Seed Module only — no models at all.
        empty_mod = ModuleInfo(
            "ww_empty_module", W6_DESCRIBE_NO_MODELS_VERSION,
            "test_repo", "/tmp", [], "17.0",
        )
        writer.write_results([ParseResult(module=empty_mod, models=[])])
        writer.close()

        srv = _import_server_module()
        out = srv._describe_module("ww_empty_module", W6_DESCRIBE_NO_MODELS_VERSION)
        assert "Next:" not in out, (
            f"Expected no Next: footer for a module with 0 models, got:\n{out!r}"
        )
    finally:
        _cleanup_version(neo4j_driver, W6_DESCRIBE_NO_MODELS_VERSION)


# --- WG-5 T1: _edition_label unit tests ------------------------------------
# The 5 pure ``_edition_label`` mapping tests (no Neo4j) were demoted out of
# this neo4j-marked module to tests/test_mcp_server_unit.py (WS-C / DD2): a
# module-level pytestmark cannot be subtracted per-test, so genuinely-pure
# tests live in an unmarked sibling module and run in the unit tier.


W6_EDITION_LABEL_VERSION = "88.0"


def test_describe_module_edition_label_opl1_firstparty(neo4j_driver):
    """describe_module: OPL-1 is the Odoo Proprietary License for third-party /
    proprietary apps (ADR-0036) — NOT Odoo Enterprise (that is OEEL-1). A Viindoo
    OPL-1 module (edition='viindoo') must render as 'Viindoo Enterprise (EE)', NOT
    'Odoo Enterprise (EE)'. Regression guard for #263 (PR #165 mislabeled
    tvtmaaddons as Odoo Enterprise)."""
    _cleanup_version(neo4j_driver, W6_EDITION_LABEL_VERSION)
    try:
        with neo4j_driver.session() as session:
            session.run(
                "MERGE (m:Module {name: $n, odoo_version: $v}) "
                "SET m.repo = 'tvtmaaddons', m.edition = 'viindoo', m.license = 'OPL-1', "
                "    m.profile = ['default']",
                n="viin_firstparty_test", v=W6_EDITION_LABEL_VERSION,
            )
        srv = _import_server_module()
        out = srv._describe_module("viin_firstparty_test", W6_EDITION_LABEL_VERSION)
        assert "Viindoo Enterprise (EE)" in out, (
            f"Expected 'Viindoo Enterprise (EE)' in Edition line, got:\n{out}"
        )
        assert "Odoo Enterprise (EE)" not in out, (
            f"OPL-1 first-party module must NOT be labeled Odoo Enterprise, got:\n{out}"
        )
    finally:
        _cleanup_version(neo4j_driver, W6_EDITION_LABEL_VERSION)


def test_describe_module_edition_label_lgpl3(neo4j_driver):
    """describe_module: LGPL-3 license → Edition shows 'Community (CE)'."""
    _cleanup_version(neo4j_driver, W6_EDITION_LABEL_VERSION)
    try:
        with neo4j_driver.session() as session:
            session.run(
                "MERGE (m:Module {name: $n, odoo_version: $v}) "
                "SET m.repo = 'odoo_ce', m.edition = 'community', m.license = 'LGPL-3', "
                "    m.profile = ['default']",
                n="sale_ce_test", v=W6_EDITION_LABEL_VERSION,
            )
        srv = _import_server_module()
        out = srv._describe_module("sale_ce_test", W6_EDITION_LABEL_VERSION)
        assert "Community (CE)" in out, (
            f"Expected 'Community (CE)' in Edition line, got:\n{out}"
        )
    finally:
        _cleanup_version(neo4j_driver, W6_EDITION_LABEL_VERSION)


def test_check_module_exists_firstparty_viindoo_not_ee_confusion(neo4j_driver):
    """check_module_exists: a Viindoo OPL-1 addon (edition='viindoo',
    license='OPL-1', repo tvtmaaddons) must report 'Is EE confusion: No' and emit
    NO Odoo-Enterprise GPL-violation warning. OPL-1 is the Odoo Proprietary License
    for third-party/proprietary apps (ADR-0036), NOT Odoo Enterprise (OEEL-1).
    Regression guard for #263 — PR #165 mislabeled to_base/viin_hr as Odoo
    Enterprise via the OPL-1-in-EE-set bug. The `all()` tenant choke and the dict
    guard list are untouched."""
    _cleanup_version(neo4j_driver, W6_EDITION_LABEL_VERSION)
    try:
        with neo4j_driver.session() as session:
            session.run(
                "MERGE (m:Module {name: $n, odoo_version: $v}) "
                "SET m.repo = 'tvtmaaddons', m.edition = 'viindoo', "
                "    m.license = 'OPL-1', m.profile = ['default']",
                n="to_base", v=W6_EDITION_LABEL_VERSION,
            )
        srv = _import_server_module()
        out = srv._check_module_exists("to_base", W6_EDITION_LABEL_VERSION)
        assert "Is EE confusion: No" in out, (
            f"First-party Viindoo OPL-1 module must NOT be EE confusion, got:\n{out}"
        )
        assert "Viindoo Enterprise (EE)" in out, (
            f"Expected Viindoo edition label, got:\n{out}"
        )
        assert "GPL" not in out and "Odoo Enterprise" not in out, (
            f"No false Odoo-Enterprise/GPL warning expected, got:\n{out}"
        )
        assert "not in Viindoo stack" not in out, (
            f"No self-contradictory 'not in Viindoo stack' line expected, got:\n{out}"
        )
    finally:
        _cleanup_version(neo4j_driver, W6_EDITION_LABEL_VERSION)


def test_describe_module_summary_rendered(neo4j_driver):
    """describe_module: manifest summary stored on node → rendered as 'Summary: ...' line."""
    _W6_SUMMARY_VERSION = "99.54"
    _cleanup_version(neo4j_driver, _W6_SUMMARY_VERSION)
    try:
        with neo4j_driver.session() as session:
            session.run(
                "MERGE (m:Module {name: $n, odoo_version: $v}) "
                "SET m.repo = 'test_repo', m.edition = 'community', "
                "    m.summary = $summary, m.profile = ['default']",
                n="sale_sum_test", v=_W6_SUMMARY_VERSION,
                summary="Manage sales orders",
            )
        srv = _import_server_module()
        out = srv._describe_module("sale_sum_test", _W6_SUMMARY_VERSION)
        assert "Summary: Manage sales orders" in out, (
            f"Expected 'Summary: Manage sales orders' in describe_module output, got:\n{out}"
        )
    finally:
        _cleanup_version(neo4j_driver, _W6_SUMMARY_VERSION)


def test_describe_module_summary_absent_no_line(neo4j_driver):
    """describe_module: node without summary → no 'Summary:' line in output."""
    _W6_NOSUMMARY_VERSION = "99.55"
    _cleanup_version(neo4j_driver, _W6_NOSUMMARY_VERSION)
    try:
        with neo4j_driver.session() as session:
            session.run(
                "MERGE (m:Module {name: $n, odoo_version: $v}) "
                "SET m.repo = 'test_repo', m.edition = 'community', "
                "    m.profile = ['default']",
                n="sale_nosum_test", v=_W6_NOSUMMARY_VERSION,
            )
        srv = _import_server_module()
        out = srv._describe_module("sale_nosum_test", _W6_NOSUMMARY_VERSION)
        assert "Summary:" not in out, (
            f"Expected no 'Summary:' line when absent, got:\n{out}"
        )
    finally:
        _cleanup_version(neo4j_driver, _W6_NOSUMMARY_VERSION)


# --- list_fields ------------------------------------------------------------


def test_list_fields_happy(neo4j_driver):
    _cleanup_version(neo4j_driver, W6_LIST_FIELDS_VERSION)
    try:
        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        writer.setup_indexes()
        sale_mod = ModuleInfo(
            "sale", W6_LIST_FIELDS_VERSION, "odoo_test", "/tmp", [], "17.0",
        )
        sale_model = ModelInfo(
            name="sale.order", module="sale",
            odoo_version=W6_LIST_FIELDS_VERSION,
            fields=[
                FieldInfo("partner_id", "many2one"),
                FieldInfo("amount_total", "monetary"),
            ],
        )
        writer.write_results([ParseResult(module=sale_mod, models=[sale_model])])
        writer.close()

        srv = _import_server_module()
        out = srv._list_fields("sale.order", W6_LIST_FIELDS_VERSION)
        assert out.startswith(
            f"Fields of sale.order (Odoo {W6_LIST_FIELDS_VERSION})",
        )
        assert "partner_id : many2one" in out
        assert "amount_total : monetary" in out
        assert out.rstrip().splitlines()[-1].startswith("└─ Next:")
    finally:
        _cleanup_version(neo4j_driver, W6_LIST_FIELDS_VERSION)


def test_list_fields_empty(neo4j_driver):
    """Model with 0 declared fields (ghost.model) still shows the builtin magic block.

    Changed in M10A D2: magic fields (id, display_name, create_uid, create_date,
    write_uid, write_date) are always injected as a synthetic <builtin> block when
    no module filter is active.  A model that has never been indexed will have 0
    declared fields, but the magic block makes the output non-empty.
    ADR-0023 §1.6: "(none)" means "empty IS the answer"; with magic rows present
    the answer is NOT empty — so "(none)" is NOT emitted.  A truly-empty result
    (all fields filtered out by kind/module/profile with no magic match) still
    emits "(none)" — see test_list_fields_truly_empty_with_kind_filter below.
    """
    _cleanup_version(neo4j_driver, W6_LIST_FIELDS_VERSION)
    try:
        srv = _import_server_module()
        out = srv._list_fields("ghost.model", W6_LIST_FIELDS_VERSION)
        assert out.startswith(
            f"Fields of ghost.model (Odoo {W6_LIST_FIELDS_VERSION})",
        )
        # Magic block is present — no "(none)" when magic fields are shown.
        assert "(none)" not in out
        assert "<builtin>" in out, "Expected '<builtin>' marker (magic fields always shown)"
        assert "id : integer" in out, "Magic field 'id' must be present"
        # Empty declared fields still get a Next: hint per Wave 5.
        assert "└─ Next:" in out
    finally:
        _cleanup_version(neo4j_driver, W6_LIST_FIELDS_VERSION)


def test_list_fields_truly_empty_with_kind_filter(neo4j_driver):
    """When kind filter matches no real field AND no magic field, emit '(none)'.

    Use kind='monetary' on ghost.model — ghost.model has 0 real fields, and
    no magic field has ttype='monetary', so magic_prelude_rows is also empty.
    This is the truly-empty case where ADR-0023 §1.6 '(none)' IS the answer.
    """
    _cleanup_version(neo4j_driver, W6_LIST_FIELDS_VERSION)
    try:
        srv = _import_server_module()
        # MAGIC_FIELDS: id=integer, display_name=char, create_uid/write_uid=many2one,
        # create_date/write_date=datetime — none are 'monetary'.
        # ghost.model has 0 real fields. So total==0 AND magic_prelude_rows is [].
        out = srv._list_fields("ghost.model", W6_LIST_FIELDS_VERSION, kind="monetary")
        assert out.startswith(
            f"Fields of ghost.model (Odoo {W6_LIST_FIELDS_VERSION})",
        )
        # Truly empty — "(none)" is the correct sentinel.
        assert "(none)" in out
        # Next: hint still emitted even for truly-empty result.
        assert "└─ Next:" in out
        # Magic block must NOT appear (kind filter excluded all magic fields).
        assert "<builtin>" not in out
    finally:
        _cleanup_version(neo4j_driver, W6_LIST_FIELDS_VERSION)


def test_list_fields_truncation(neo4j_driver):
    """>50 fields (LIST_PREVIEW_FIELDS_MAX) → cap disclosure appears."""
    _cleanup_version(neo4j_driver, W6_LIST_FIELDS_VERSION)
    try:
        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        writer.setup_indexes()
        mod = ModuleInfo(
            "big_mod", W6_LIST_FIELDS_VERSION, "odoo_test", "/tmp", [], "17.0",
        )
        big_model = ModelInfo(
            name="big.model", module="big_mod",
            odoo_version=W6_LIST_FIELDS_VERSION,
            fields=[FieldInfo(f"field_{i:03d}", "char") for i in range(60)],
        )
        writer.write_results([ParseResult(module=mod, models=[big_model])])
        writer.close()

        srv = _import_server_module()
        out = srv._list_fields("big.model", W6_LIST_FIELDS_VERSION)
        # cap = LIST_PREVIEW_FIELDS_MAX (50); 60 total → continuation hint appears.
        # Pagination hint format: "Showing rows 1–50 of 60. Call list_fields(...)"
        assert "Showing rows 1–50 of 60" in out
        assert "model_inspect" in out  # continuation hint references the superset tool
        assert "start_index=50" in out  # next-page cursor is disclosed
    finally:
        _cleanup_version(neo4j_driver, W6_LIST_FIELDS_VERSION)


# AC-C2-7: pagination smoke — two pages cover all 247 rows without gaps.
W6_LIST_FIELDS_PAGER_VERSION = "85.0"


def test_list_fields_pagination_smoke(neo4j_driver):
    """Two consecutive start_index calls cover all 247 fixture rows without gaps.

    AC-C2-7: list_fields(limit=50, start_index=0) + list_fields(limit=50, start_index=50)
    both include refs; second call does NOT include a continuation hint (start_index+shown==total).
    """
    _cleanup_version(neo4j_driver, W6_LIST_FIELDS_PAGER_VERSION)
    try:
        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        writer.setup_indexes()
        mod = ModuleInfo(
            "pager_mod", W6_LIST_FIELDS_PAGER_VERSION, "odoo_test", "/tmp", [], "17.0",
        )
        pager_model = ModelInfo(
            name="pager.model", module="pager_mod",
            odoo_version=W6_LIST_FIELDS_PAGER_VERSION,
            fields=[FieldInfo(f"field_{i:03d}", "char") for i in range(247)],
        )
        writer.write_results([ParseResult(module=mod, models=[pager_model])])
        writer.close()

        srv = _import_server_module()

        # Page 1: rows 0-49 of 247.
        out1 = srv._list_fields(
            "pager.model", W6_LIST_FIELDS_PAGER_VERSION, limit=50, start_index=0,
        )
        assert "[ref=f" in out1, "Page 1 must include refs"
        assert "Showing rows 1–50 of 247" in out1, "Page 1 must show continuation hint"
        assert "start_index=50" in out1, "Page 1 continuation hint must point to start_index=50"
        # Extract field names from page 1.
        page1_lines = [ln for ln in out1.splitlines() if "[ref=" in ln]
        assert len(page1_lines) == 50, f"Page 1 should render 50 field rows, got {len(page1_lines)}"

        # Page 2: rows 50-99 of 247.
        out2 = srv._list_fields(
            "pager.model", W6_LIST_FIELDS_PAGER_VERSION, limit=50, start_index=50,
        )
        assert "[ref=f" in out2, "Page 2 must include refs"
        # Page 2 still has more (247 > 100) → continuation hint.
        assert "Showing rows 51–100 of 247" in out2
        assert "start_index=100" in out2

        # Final page: rows 200-246 (47 items) of 247 — no continuation hint.
        out_final = srv._list_fields(
            "pager.model", W6_LIST_FIELDS_PAGER_VERSION, limit=50, start_index=200,
        )
        assert "[ref=f" in out_final, "Final page must include refs"
        # Final page: shown=47, end_index=247 == total → no "Showing rows ... Call" hint.
        assert "Showing rows 201–247 of 247 (last page)" in out_final
        assert "start_index=247" not in out_final, "Final page must NOT include a continuation hint"

        # Verify no gaps: collect all field names across three pages.
        def _extract_field_names(out: str) -> set[str]:
            names = set()
            for line in out.splitlines():
                if "[ref=f" in line and "field_" in line:
                    # Extract "field_NNN" from lines like "│   ├─ [ref=f1] field_000 : char"
                    for part in line.split():
                        if part.startswith("field_"):
                            names.add(part.rstrip(":"))
            return names

        # Collect all 247 names by paginating 50 at a time.
        all_names: set[str] = set()
        for page_start in range(0, 247, 50):
            page_out = srv._list_fields(
                "pager.model", W6_LIST_FIELDS_PAGER_VERSION,
                limit=50, start_index=page_start,
            )
            all_names |= _extract_field_names(page_out)

        expected_names = {f"field_{i:03d}" for i in range(247)}
        assert all_names == expected_names, (
            f"Pagination gap detected: got {len(all_names)} unique field names, "
            f"expected 247. Missing: {expected_names - all_names}"
        )
    finally:
        _cleanup_version(neo4j_driver, W6_LIST_FIELDS_PAGER_VERSION)


# --- list_methods -----------------------------------------------------------


def test_list_methods_happy(neo4j_driver):
    _cleanup_version(neo4j_driver, W6_LIST_METHODS_VERSION)
    try:
        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        writer.setup_indexes()
        mod = ModuleInfo(
            "sale", W6_LIST_METHODS_VERSION, "odoo_test", "/tmp", [], "17.0",
        )
        model = ModelInfo(
            name="sale.order", module="sale",
            odoo_version=W6_LIST_METHODS_VERSION,
            methods=[
                MethodInfo("action_confirm"),
                MethodInfo("_compute_amount_total"),
            ],
        )
        writer.write_results([ParseResult(module=mod, models=[model])])
        writer.close()

        srv = _import_server_module()
        out = srv._list_methods("sale.order", W6_LIST_METHODS_VERSION)
        assert out.startswith(
            f"Methods of sale.order (Odoo {W6_LIST_METHODS_VERSION})",
        )
        assert "action_confirm" in out
        assert "_compute_amount_total" in out
        assert out.rstrip().splitlines()[-1].startswith("└─ Next:")
    finally:
        _cleanup_version(neo4j_driver, W6_LIST_METHODS_VERSION)


def test_list_methods_empty(neo4j_driver):
    _cleanup_version(neo4j_driver, W6_LIST_METHODS_VERSION)
    try:
        srv = _import_server_module()
        out = srv._list_methods("ghost.model", W6_LIST_METHODS_VERSION)
        assert "(none)" in out
        assert "└─ Next:" in out
    finally:
        _cleanup_version(neo4j_driver, W6_LIST_METHODS_VERSION)


def test_list_methods_truncation(neo4j_driver):
    """>20 methods → cap disclosure appears (LIST_PREVIEW_MAX_ITEMS)."""
    _cleanup_version(neo4j_driver, W6_LIST_METHODS_VERSION)
    try:
        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        writer.setup_indexes()
        mod = ModuleInfo(
            "many_methods_mod", W6_LIST_METHODS_VERSION, "odoo_test", "/tmp",
            [], "17.0",
        )
        model = ModelInfo(
            name="many.model", module="many_methods_mod",
            odoo_version=W6_LIST_METHODS_VERSION,
            methods=[MethodInfo(f"method_{i:03d}") for i in range(30)],
        )
        writer.write_results([ParseResult(module=mod, models=[model])])
        writer.close()

        srv = _import_server_module()
        out = srv._list_methods("many.model", W6_LIST_METHODS_VERSION)
        # cap = 20; 30 total → continuation hint appears.
        # Pagination hint format: "Showing rows 1–20 of 30. Call list_methods(...)"
        assert "Showing rows 1–20 of 30" in out
        assert "model_inspect" in out  # continuation hint references the superset tool
        assert "start_index=20" in out
    finally:
        _cleanup_version(neo4j_driver, W6_LIST_METHODS_VERSION)


# --- list_views -------------------------------------------------------------


def test_list_views_happy(neo4j_driver):
    _cleanup_version(neo4j_driver, W6_LIST_VIEWS_VERSION)
    try:
        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        writer.setup_indexes()
        sale_mod = ModuleInfo(
            "sale", W6_LIST_VIEWS_VERSION, "odoo_test", "/tmp", [], "17.0",
        )
        views = [
            ViewInfo(
                xmlid="sale.view_order_form", name="form",
                model="sale.order", module="sale",
                odoo_version=W6_LIST_VIEWS_VERSION,
                view_type="form", mode="primary", inherit_xmlid=None,
            ),
            ViewInfo(
                xmlid="sale.view_order_tree", name="tree",
                model="sale.order", module="sale",
                odoo_version=W6_LIST_VIEWS_VERSION,
                view_type="tree", mode="primary", inherit_xmlid=None,
            ),
        ]
        writer.write_view_results(
            [ViewParseResult(module=sale_mod, views=views)],
        )
        writer.close()

        srv = _import_server_module()
        out = srv._list_views("sale.order", W6_LIST_VIEWS_VERSION)
        assert out.startswith(
            f"Views of sale.order (Odoo {W6_LIST_VIEWS_VERSION})",
        )
        assert "sale.view_order_form : form" in out
        assert "sale.view_order_tree : tree" in out
        assert out.rstrip().splitlines()[-1].startswith("└─ Next:")
    finally:
        _cleanup_version(neo4j_driver, W6_LIST_VIEWS_VERSION)


def test_list_views_empty(neo4j_driver):
    _cleanup_version(neo4j_driver, W6_LIST_VIEWS_VERSION)
    try:
        srv = _import_server_module()
        out = srv._list_views("ghost.model", W6_LIST_VIEWS_VERSION)
        assert "(none)" in out
        assert "└─ Next:" in out
    finally:
        _cleanup_version(neo4j_driver, W6_LIST_VIEWS_VERSION)


def test_list_views_truncation(neo4j_driver):
    """>20 views → cap disclosure appears."""
    _cleanup_version(neo4j_driver, W6_LIST_VIEWS_VERSION)
    try:
        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        writer.setup_indexes()
        mod = ModuleInfo(
            "view_mod", W6_LIST_VIEWS_VERSION, "odoo_test", "/tmp", [], "17.0",
        )
        views = [
            ViewInfo(
                xmlid=f"view_mod.view_{i:03d}", name=f"v{i}",
                model="big.model", module="view_mod",
                odoo_version=W6_LIST_VIEWS_VERSION,
                view_type="form", mode="primary", inherit_xmlid=None,
            )
            for i in range(25)
        ]
        writer.write_view_results(
            [ViewParseResult(module=mod, views=views)],
        )
        writer.close()

        srv = _import_server_module()
        out = srv._list_views("big.model", W6_LIST_VIEWS_VERSION)
        # cap = 20; 25 total → continuation hint appears.
        assert "Showing rows 1–20 of 25" in out
        assert "model_inspect" in out  # continuation hint references the superset tool
        assert "start_index=20" in out
    finally:
        _cleanup_version(neo4j_driver, W6_LIST_VIEWS_VERSION)


W6_LIST_VIEWS_BY_MODULE_VERSION = "82.1"


def test_list_views_by_module_smoke(neo4j_driver):
    """_list_views_by_module returns views grouped by module, with ref markers."""
    _cleanup_version(neo4j_driver, W6_LIST_VIEWS_BY_MODULE_VERSION)
    try:
        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        writer.setup_indexes()
        sale_mod = ModuleInfo(
            "sale", W6_LIST_VIEWS_BY_MODULE_VERSION, "odoo_test", "/tmp", [], "17.0",
        )
        views = [
            ViewInfo(
                xmlid="sale.view_order_form", name="form",
                model="sale.order", module="sale",
                odoo_version=W6_LIST_VIEWS_BY_MODULE_VERSION,
                view_type="form", mode="primary", inherit_xmlid=None,
            ),
            ViewInfo(
                xmlid="sale.view_order_tree", name="tree",
                model="sale.order", module="sale",
                odoo_version=W6_LIST_VIEWS_BY_MODULE_VERSION,
                view_type="tree", mode="primary", inherit_xmlid=None,
            ),
        ]
        writer.write_view_results(
            [ViewParseResult(module=sale_mod, views=views)],
        )
        writer.close()

        srv = _import_server_module()
        out = srv._list_views_by_module("sale", W6_LIST_VIEWS_BY_MODULE_VERSION)

        # AC-D2-2: header uses "Views in module 'X'" form.
        assert out.startswith(
            f"Views in module 'sale' (Odoo {W6_LIST_VIEWS_BY_MODULE_VERSION})",
        )
        # Both seeded views appear.
        assert "sale.view_order_form : form" in out
        assert "sale.view_order_tree : tree" in out
        # AC-D2-4: ref markers present.
        assert "[ref=" in out
        # AC-D2-5: Next-step footer present.
        assert out.rstrip().splitlines()[-1].startswith("└─ Next:")
    finally:
        _cleanup_version(neo4j_driver, W6_LIST_VIEWS_BY_MODULE_VERSION)


# --- list_owl_components ----------------------------------------------------


def test_list_owl_components_happy(neo4j_driver):
    _cleanup_version(neo4j_driver, W6_LIST_OWL_VERSION)
    try:
        seed_owl_components(
            neo4j_driver, module="sale_management",
            odoo_version=W6_LIST_OWL_VERSION,
            components=[
                {"name": "SaleOrderKanban", "bound_model": "sale.order",
                 "template": "tmpl_a"},
                {"name": "SaleSidebar", "bound_model": None,
                 "template": "tmpl_b"},
            ],
        )
        srv = _import_server_module()
        out = srv._list_owl_components(
            "sale_management", W6_LIST_OWL_VERSION,
        )
        assert out.startswith(
            f"OWL components of sale_management (Odoo {W6_LIST_OWL_VERSION})",
        )
        assert "SaleOrderKanban : sale.order" in out
        assert "SaleSidebar : (unbound)" in out
        assert out.rstrip().splitlines()[-1].startswith("└─ Next:")
    finally:
        _cleanup_version(neo4j_driver, W6_LIST_OWL_VERSION)


def test_list_owl_components_empty(neo4j_driver):
    _cleanup_version(neo4j_driver, W6_LIST_OWL_VERSION)
    try:
        srv = _import_server_module()
        out = srv._list_owl_components(
            "empty_module", W6_LIST_OWL_VERSION,
        )
        assert "(none)" in out
        assert "└─ Next:" in out
    finally:
        _cleanup_version(neo4j_driver, W6_LIST_OWL_VERSION)


def test_list_owl_components_truncation(neo4j_driver):
    """>20 OWL components → cap disclosure appears."""
    _cleanup_version(neo4j_driver, W6_LIST_OWL_VERSION)
    try:
        seed_owl_components(
            neo4j_driver, module="big_owl_mod",
            odoo_version=W6_LIST_OWL_VERSION,
            components=[
                {"name": f"Component{i:03d}", "bound_model": None,
                 "template": None}
                for i in range(25)
            ],
        )
        srv = _import_server_module()
        out = srv._list_owl_components(
            "big_owl_mod", W6_LIST_OWL_VERSION,
        )
        # cap = LIST_PREVIEW_MAX_ITEMS (20); 25 total → continuation hint appears.
        assert "Showing rows 1–20 of 25" in out
        assert "module_inspect" in out  # continuation hint references the superset tool
        assert "start_index=20" in out
    finally:
        _cleanup_version(neo4j_driver, W6_LIST_OWL_VERSION)


def test_list_owl_components_era_guard_v13(neo4j_driver):
    """Odoo v8–v13 (Widget era): empty + warning, suggest list_js_patches."""
    _cleanup_version(neo4j_driver, W6_LIST_OWL_LEGACY_VERSION)
    try:
        srv = _import_server_module()
        out = srv._list_owl_components(
            "any_mod", W6_LIST_OWL_LEGACY_VERSION,
        )
        # Era-guard text is the canonical v8–v13 message per ADR-0023 §1.7.
        assert "(none)" in out
        assert "Widget era" in out
        assert "module_inspect" in out  # era-guard suggests module_inspect(method='js')
    finally:
        _cleanup_version(neo4j_driver, W6_LIST_OWL_LEGACY_VERSION)


# --- list_qweb_templates ----------------------------------------------------


def test_list_qweb_templates_happy(neo4j_driver):
    _cleanup_version(neo4j_driver, W6_LIST_QWEB_VERSION)
    try:
        seed_qweb_templates(
            neo4j_driver, module="website_sale",
            odoo_version=W6_LIST_QWEB_VERSION,
            templates=[
                {"xmlid": "website_sale.product", "inherit_xmlid": None},
                {"xmlid": "website_sale.cart_lines",
                 "inherit_xmlid": "website_sale.cart"},
            ],
        )
        srv = _import_server_module()
        out = srv._list_qweb_templates(
            "website_sale", W6_LIST_QWEB_VERSION,
        )
        assert out.startswith(
            f"QWeb templates of website_sale (Odoo {W6_LIST_QWEB_VERSION})",
        )
        assert "website_sale.product : t-inherit=(root)" in out
        assert "website_sale.cart_lines : t-inherit=website_sale.cart" in out
        assert out.rstrip().splitlines()[-1].startswith("└─ Next:")
    finally:
        _cleanup_version(neo4j_driver, W6_LIST_QWEB_VERSION)


def test_list_qweb_templates_empty(neo4j_driver):
    _cleanup_version(neo4j_driver, W6_LIST_QWEB_VERSION)
    try:
        srv = _import_server_module()
        out = srv._list_qweb_templates(
            "empty_module", W6_LIST_QWEB_VERSION,
        )
        assert "(none)" in out
        assert "└─ Next:" in out
    finally:
        _cleanup_version(neo4j_driver, W6_LIST_QWEB_VERSION)


def test_list_qweb_templates_truncation(neo4j_driver):
    """>20 QWeb templates → cap disclosure appears."""
    _cleanup_version(neo4j_driver, W6_LIST_QWEB_VERSION)
    try:
        seed_qweb_templates(
            neo4j_driver, module="big_qweb_mod",
            odoo_version=W6_LIST_QWEB_VERSION,
            templates=[
                {"xmlid": f"big_qweb_mod.tmpl_{i:03d}",
                 "inherit_xmlid": None}
                for i in range(25)
            ],
        )
        srv = _import_server_module()
        out = srv._list_qweb_templates(
            "big_qweb_mod", W6_LIST_QWEB_VERSION,
        )
        # cap = LIST_PREVIEW_MAX_ITEMS (20); 25 total → continuation hint appears.
        assert "Showing rows 1–20 of 25" in out
        assert "module_inspect" in out  # continuation hint references the superset tool
        assert "start_index=20" in out
    finally:
        _cleanup_version(neo4j_driver, W6_LIST_QWEB_VERSION)


# --- list_js_patches --------------------------------------------------------


def test_list_js_patches_happy(neo4j_driver):
    _cleanup_version(neo4j_driver, W6_LIST_JS_VERSION)
    try:
        seed_js_patches(
            neo4j_driver, module="sale_management",
            odoo_version=W6_LIST_JS_VERSION,
            patches=[
                {"target": "ListController", "patch_name": "applyFilters",
                 "era": "patch"},
                {"target": "FormView", "patch_name": "onLoad",
                 "era": "patch"},
            ],
        )
        srv = _import_server_module()
        out = srv._list_js_patches(W6_LIST_JS_VERSION, module="sale_management")
        assert "JS patches on sale_management" in out
        assert "ListController.applyFilters : era=patch" in out
        assert "FormView.onLoad : era=patch" in out
        assert out.rstrip().splitlines()[-1].startswith("└─ Next:")
    finally:
        _cleanup_version(neo4j_driver, W6_LIST_JS_VERSION)


def test_list_js_patches_empty(neo4j_driver):
    _cleanup_version(neo4j_driver, W6_LIST_JS_VERSION)
    try:
        srv = _import_server_module()
        out = srv._list_js_patches(W6_LIST_JS_VERSION, module="empty_module")
        assert "(none)" in out
        assert "└─ Next:" in out
    finally:
        _cleanup_version(neo4j_driver, W6_LIST_JS_VERSION)


def test_list_js_patches_truncation(neo4j_driver):
    """>10 JS patches (LIST_PREVIEW_PATCHES_MAX) → cap disclosure appears."""
    _cleanup_version(neo4j_driver, W6_LIST_JS_VERSION)
    try:
        seed_js_patches(
            neo4j_driver, module="patchy_mod",
            odoo_version=W6_LIST_JS_VERSION,
            patches=[
                {"target": f"Target{i:03d}", "patch_name": "go",
                 "era": "patch"}
                for i in range(15)
            ],
        )
        srv = _import_server_module()
        out = srv._list_js_patches(W6_LIST_JS_VERSION, module="patchy_mod")
        # cap = LIST_PREVIEW_PATCHES_MAX (10); 15 total → continuation hint appears.
        assert "Showing rows 1–10 of 15" in out
        assert "module_inspect" in out  # continuation hint references the superset tool
        assert "start_index=10" in out
    finally:
        _cleanup_version(neo4j_driver, W6_LIST_JS_VERSION)


def test_list_js_patches_era_filter(neo4j_driver):
    """era='era1' filter keeps only legacy extend patches."""
    _cleanup_version(neo4j_driver, W6_LIST_JS_VERSION)
    try:
        seed_js_patches(
            neo4j_driver, module="mixed_eras",
            odoo_version=W6_LIST_JS_VERSION,
            patches=[
                {"target": "WidgetA", "patch_name": "legacy",
                 "era": "extend"},
                {"target": "ComponentB", "patch_name": "modern",
                 "era": "patch"},
            ],
        )
        srv = _import_server_module()
        out_era1 = srv._list_js_patches(
            W6_LIST_JS_VERSION, module="mixed_eras", era="era1",
        )
        assert "WidgetA.legacy" in out_era1
        assert "ComponentB.modern" not in out_era1

        out_era3 = srv._list_js_patches(
            W6_LIST_JS_VERSION, module="mixed_eras", era="era3",
        )
        assert "ComponentB.modern" in out_era3
        assert "WidgetA.legacy" not in out_era3
    finally:
        _cleanup_version(neo4j_driver, W6_LIST_JS_VERSION)


# ===========================================================================
# Grammar consistency test — runs against all 21 tools.
# ===========================================================================


@pytest.fixture(scope="module")
def grammar_seed(neo4j_driver):
    """Seed a minimal but complete dataset so every tool returns content.

    All 23 tools either render content or a deterministic empty/error string;
    every output must obey the ADR-0023 §1 tree grammar.
    """
    _cleanup_version(neo4j_driver, W6_GRAMMAR_VERSION)
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()
    sale_mod = ModuleInfo(
        "sale", W6_GRAMMAR_VERSION, "odoo_test", "/tmp", [], "17.0",
    )
    sale_model = ModelInfo(
        name="sale.order", module="sale", odoo_version=W6_GRAMMAR_VERSION,
        fields=[FieldInfo("name", "char"),
                FieldInfo("amount_total", "monetary", compute="_compute_amount")],
        methods=[MethodInfo("action_confirm")],
    )
    writer.write_results(
        [ParseResult(module=sale_mod, models=[sale_model])],
    )
    view = ViewInfo(
        xmlid="sale.view_order_form", name="form",
        model="sale.order", module="sale",
        odoo_version=W6_GRAMMAR_VERSION,
        view_type="form", mode="primary", inherit_xmlid=None,
    )
    writer.write_view_results(
        [ViewParseResult(module=sale_mod, views=[view])],
    )
    writer.close()
    # Seed UI nodes via direct helpers.
    seed_owl_components(
        neo4j_driver, module="sale", odoo_version=W6_GRAMMAR_VERSION,
        components=[{"name": "SaleOrderKanban",
                     "bound_model": "sale.order", "template": "t"}],
    )
    seed_qweb_templates(
        neo4j_driver, module="sale", odoo_version=W6_GRAMMAR_VERSION,
        templates=[{"xmlid": "sale.qweb_x", "inherit_xmlid": None}],
    )
    seed_js_patches(
        neo4j_driver, module="sale", odoo_version=W6_GRAMMAR_VERSION,
        patches=[{"target": "ListController",
                  "patch_name": "x", "era": "patch"}],
    )
    # Seed :Stylesheet nodes with an :IMPORTS edge so resolve_stylesheet exercises
    # the import-chain branch in test_grammar_consistency_all_tools.
    # Two files: main.scss (imports variables.scss) and variables.scss.
    # This ensures both the "has imports" branch and "imports: none" branch are hit
    # within a single tool invocation against grammar_seed data.
    seed_stylesheets(
        neo4j_driver, module="sale", odoo_version=W6_GRAMMAR_VERSION,
        stylesheets=[
            {
                "file_path": "/tmp/sale/static/src/scss/main.scss",
                "language": "scss",
                "selector_count": 5,
                "variable_count": 0,
                "import_count": 1,
                "mixin_count": 0,
            },
            {
                "file_path": "/tmp/sale/static/src/scss/variables.scss",
                "language": "scss",
                "selector_count": 0,
                "variable_count": 3,
                "import_count": 0,
                "mixin_count": 0,
            },
        ],
        imports=[
            (
                "/tmp/sale/static/src/scss/main.scss",
                "/tmp/sale/static/src/scss/variables.scss",
            ),
        ],
    )
    yield W6_GRAMMAR_VERSION
    _cleanup_version(neo4j_driver, W6_GRAMMAR_VERSION)


def _all_tool_invocations(version: str):
    """Return list of (tool_name, callable) for every public MCP tool.

    Each callable returns the tree-text string when invoked with the right
    minimal arguments against the grammar_seed dataset.
    """
    srv = _import_server_module()
    return [
        ("resolve_model",
         lambda: srv._resolve_model("sale.order", version)),
        ("resolve_field",
         lambda: srv._resolve_field("sale.order", "amount_total", version)),
        ("resolve_method",
         lambda: srv._resolve_method("sale.order", "action_confirm", version)),
        ("resolve_view",
         lambda: srv._resolve_view("sale.view_order_form", version)),
        ("describe_module",
         lambda: srv._describe_module("sale", version)),
        ("list_fields",
         lambda: srv._list_fields("sale.order", version)),
        ("list_methods",
         lambda: srv._list_methods("sale.order", version)),
        ("list_views",
         lambda: srv._list_views("sale.order", version)),
        ("list_owl_components",
         lambda: srv._list_owl_components("sale", version)),
        ("list_qweb_templates",
         lambda: srv._list_qweb_templates("sale", version)),
        ("list_js_patches",
         lambda: srv._list_js_patches(version, module="sale")),
        ("check_module_exists",
         lambda: srv._check_module_exists("sale", version)),
        ("find_override_point",
         lambda: srv._find_override_point("sale.order", "action_confirm", version)),
        # Terminal tools (no Next: footer):
        ("lint_check",
         lambda: srv._lint_check("def f(): pass", version)),
        ("cli_help",
         lambda: srv._cli_help("server", version)),
        ("api_version_diff",
         lambda: srv._api_version_diff("nonexistent_symbol", version, version)),
        # Tools that depend on data we don't seed; they take the empty-result
        # branch which still emits a deterministic tree shape.
        ("lookup_core_api",
         lambda: srv._lookup_core_api("nonexistent_symbol", version)),
        ("find_deprecated_usage",
         lambda: srv._find_deprecated_usage(version)),
        ("impact_analysis",
         lambda: srv._impact_analysis("model", "sale.order", version)),
        # find_examples uses pg + embedder; empty-query path returns a sentinel.
        ("find_examples",
         lambda: srv._find_examples("", version)),
        ("suggest_pattern",
         lambda: srv._suggest_pattern("", version)),
        # M10A stylesheet tools (ADR-0025, D5/D6)
        ("resolve_stylesheet",
         lambda: srv._resolve_stylesheet("sale", version)),
        ("find_style_override",
         lambda: srv._find_style_override("", version)),
        # M10.5 P2 ORM-validation tools
        ("resolve_orm_chain",
         lambda: srv._resolve_orm_chain("sale.order", "amount_total", version)),
        ("validate_domain",
         lambda: srv._validate_domain("sale.order", "[('amount_total', '>', 0)]", version)),
        ("validate_depends",
         lambda: srv._validate_depends("sale.order", "action_confirm", version)),
        ("validate_relation",
         lambda: srv._validate_relation("sale.order", "amount_total", "res.partner", version)),
    ]


TERMINAL_TOOLS = {"lint_check", "cli_help", "api_version_diff"}

# `find_examples`, `suggest_pattern`, and `find_style_override` empty-input
# sentinels are intentional user-error messages that bypass the tree grammar —
# exclude them from the strict grammar / next-step tests. Their normal-path
# output IS tree-shaped but reproducing it here would require seeded pgvector.
SENTINEL_EDGE_CASES = {"find_examples", "suggest_pattern", "find_style_override"}


def _tool_invocation_count() -> int:
    """Return the number of (tool_name, callable) pairs in _all_tool_invocations.

    Used to drive parametrize so the count stays in sync with the actual tool
    list — no hardcoded magic number.  We pass a dummy version here; only the
    list length matters at collection time (no DB call is made).
    """
    # Use a dummy version string; _all_tool_invocations builds lambdas that
    # capture the version but doesn't execute any queries at construction time.
    return len(_all_tool_invocations("_count_only_"))


@pytest.mark.parametrize("idx", range(_tool_invocation_count()))
def test_grammar_consistency_all_tools(grammar_seed, idx):
    """For every MCP tool: header line + valid connector positions.

    Grammar rules (ADR-0023 §1):
    - The first non-banner line is the header (no ├─ / └─).
    - Every subsequent line either starts with a connector at column 0, or
      with a sublist indent (``│   `` or ``    ``) followed by a connector,
      or is the truncation tail (``... and N more``), or is a known
      informational banner (V0 lint matcher / spec curation status).
    - No two consecutive ``└─`` branches at column 0.

    SENTINEL_EDGE_CASES are exempt: their empty-input branches return plain
    error messages that intentionally fall outside the tree contract.
    """
    version = grammar_seed
    invocations = _all_tool_invocations(version)
    tool_name, fn = invocations[idx]
    if tool_name in SENTINEL_EDGE_CASES:
        pytest.skip(
            f"{tool_name} empty-input returns a user-error sentinel — exempt"
        )
    out = fn()
    lines = out.splitlines()
    assert lines, f"{tool_name}: output is empty"

    # Strip leading informational banners (V0 lint matcher / V0.5 hybrid matcher,
    # curate status). Updated for WI-6: banner wording changed from "V0 fuzzy
    # matcher" to "Hybrid matcher (V0.5)" - both substrings must be recognised so
    # the grammar check sees the header as the first non-banner line.
    def _is_banner(line: str) -> bool:
        return (
            "V0 fuzzy matcher" in line
            or "Hybrid matcher" in line
            or "V0.5" in line
            or "Spec data" in line
            and "pending curation" in line
        )

    header_idx = 0
    while header_idx < len(lines) and _is_banner(lines[header_idx]):
        header_idx += 1
    assert header_idx < len(lines), (
        f"{tool_name}: no header after banner lines:\n{out}"
    )
    # Header — must NOT start with a tree connector.
    assert not lines[header_idx].lstrip().startswith(("├─", "└─")), (
        f"{tool_name}: header line uses tree connector — got"
        f" {lines[header_idx]!r}"
    )

    # Allowed prefixes for non-header lines.
    allowed_starts = ("├─", "└─", "│   ├─", "│   └─", "    ├─", "    └─",
                      "│   │   ├─", "│   │   └─", "│   │   │   ├─",
                      "│   │   │   └─", "│   │   │   │   ├─",
                      "│   │   │   │   └─", "│   │   │       ├─",
                      "│   │   │       └─", "│       ├─", "│       └─",
                      "        ├─", "        └─")

    for i, line in enumerate(lines[header_idx + 1:], start=header_idx + 1):
        if not line.strip():
            continue
        if _is_banner(line):
            continue
        # Truncation tail produced by _render_capped — still a valid grammar
        # element (it is rendered inside a tree branch so the surrounding
        # branch carries the connector). Allow as bare line.
        if line.lstrip().startswith("..."):
            continue
        # Indented data continuation lines (suggest_pattern snippet, etc.).
        if line.startswith("        ") and not line.lstrip().startswith(
            ("├─", "└─"),
        ):
            continue
        # The bulk of well-formed lines start with one of the allowed prefixes.
        assert line.startswith(allowed_starts), (
            f"{tool_name} line {i} has bad prefix:\n  {line!r}\n"
            f"Full output:\n{out}"
        )

    # No two consecutive lines like ``└─ X`` / ``└─ Y`` at column 0
    # (a └─ closes its parent; another └─ at the same level breaks shape).
    prev_was_root_last = False
    for line in lines[header_idx + 1:]:
        is_root_last = line.startswith("└─")
        if prev_was_root_last and is_root_last:
            raise AssertionError(
                f"{tool_name}: two consecutive '└─' branches at column 0 — "
                f"invalid tree shape:\n{out}"
            )
        prev_was_root_last = is_root_last


# ===========================================================================
# Next-step footer test — 20 drill-down MUST emit, 3 terminal MUST NOT emit.
# ===========================================================================


def test_next_step_footer_present(grammar_seed):
    """Each drill-down tool's normal output MUST contain ``└─ Next:``.

    Per ADR-0023 §4.3 the 20 drill-down tools emit ``└─ Next:`` either as the
    last line or, on empty-result branches, somewhere in the output. The two
    sentinel edge cases (``find_examples`` / ``suggest_pattern`` empty input)
    are exempt — they emit a user-error message, not a drill-down tree.
    """
    version = grammar_seed
    invocations = dict(_all_tool_invocations(version))
    must_emit = [
        name for name, _ in _all_tool_invocations(version)
        if name not in TERMINAL_TOOLS and name not in SENTINEL_EDGE_CASES
    ]
    failures: list[str] = []
    for name in must_emit:
        out = invocations[name]()
        last = out.rstrip().splitlines()[-1]
        # Either the last line is ``└─ Next:`` OR the output contains a
        # ``└─ Next:`` line (some tools emit Next: as a non-final branch when
        # they append closing warning/banner lines).
        if not (last.startswith("└─ Next:") or "└─ Next:" in out):
            failures.append(f"{name}: missing └─ Next: (last line: {last!r})")
    assert not failures, "\n".join(failures)


def test_next_step_footer_absent(grammar_seed):
    """Terminal tools (lint_check, cli_help, api_version_diff) MUST NOT emit
    a ``└─ Next:`` footer — they are pure terminal artifacts (ADR-0023 §4.4)."""
    version = grammar_seed
    invocations = dict(_all_tool_invocations(version))
    for name in TERMINAL_TOOLS:
        out = invocations[name]()
        assert "└─ Next:" not in out, (
            f"Terminal tool {name} unexpectedly emits '└─ Next:' footer:\n{out}"
        )


# ===========================================================================
# Language policy test (ADR-0023 §2) — the pure AST-walk of src/mcp/server.py
# (no Neo4j) was demoted to tests/test_mcp_server_unit.py (WS-C / DD2).
# ===========================================================================


# ===========================================================================
# ADR-0023 §4.2 — Next-step self-loop guard
# ===========================================================================


@pytest.mark.parametrize("idx", range(_tool_invocation_count()))
def test_next_step_no_loop(grammar_seed, idx):
    """ADR-0023 §4.2: a tool's ``Next:`` hint MUST NOT reference itself.

    Loop = self-reference (same tool name); drill-down = different tool. OK.
    Terminal tools and sentinel edge-cases are exempt (they have no Next:).
    """
    version = grammar_seed
    invocations = _all_tool_invocations(version)
    name, fn = invocations[idx]

    if name in TERMINAL_TOOLS or name in SENTINEL_EDGE_CASES:
        pytest.skip(f"{name} is terminal/sentinel — no Next: expected, exempt")

    out = fn()

    # Collect all lines that contain ``Next:``
    next_lines = [ln for ln in out.splitlines() if "Next:" in ln]
    if not next_lines:
        # Should not happen for drill-down tools, but don't double-fail here —
        # test_next_step_footer_present already covers that contract.
        return

    for line in next_lines:
        # Extract the hint payload after "Next: "
        if "Next:" in line:
            payload = line.split("Next:", 1)[1].strip()
        else:
            continue
        # Split on pipe (multiple hints) and check each
        for hint in payload.split("|"):
            hint = hint.strip()
            # Tool name is everything before the first '('
            if "(" in hint:
                tool_called = hint.split("(", 1)[0].strip()
            else:
                tool_called = hint.strip()
            assert tool_called != name, (
                f"Tool '{name}' suggests itself in Next: footer — "
                f"self-loop violates ADR-0023 §4.2.\n"
                f"  Offending line: {line!r}"
            )


# ===========================================================================
# ADR-0023 §5.3 — list_owl_components bound_model warning footer
# ===========================================================================


def test_list_owl_components_bound_model_warning(neo4j_driver):
    """``_list_owl_components(bound_model=X)`` emits the heuristic warning.

    Per ADR-0023 §5.3: when ``bound_model`` filter is applied, the tool must
    emit ``Warning: bound_model resolution is heuristic`` because
    parser_js.py:415 uses a fuzzy heuristic that may miss components with
    dynamic ``this.props.resModel``.
    """
    _cleanup_version(neo4j_driver, W6_LIST_OWL_VERSION)
    try:
        seed_owl_components(
            neo4j_driver, module="sale_management",
            odoo_version=W6_LIST_OWL_VERSION,
            components=[
                {"name": "SaleOrderKanban", "bound_model": "sale.order",
                 "template": "tmpl_a"},
            ],
        )
        srv = _import_server_module()
        out = srv._list_owl_components(
            "sale_management", W6_LIST_OWL_VERSION,
            bound_model="sale.order",
        )
        assert "Warning: bound_model resolution is heuristic" in out, (
            f"Expected heuristic warning when bound_model filter is set.\n"
            f"Got:\n{out}"
        )
    finally:
        _cleanup_version(neo4j_driver, W6_LIST_OWL_VERSION)


# ---------------------------------------------------------------------------
# WI-D3: @mcp.tool wrappers — model_inspect / module_inspect / entity_lookup
# ---------------------------------------------------------------------------


def test_model_inspect_routes_to_resolve_model(seeded_neo4j):
    """model_inspect(method='summary') output must start with same header as
    _resolve_model for the same model + version (AC-D3-5)."""
    srv = _import_server_module()
    direct = srv._resolve_model("account.move", TEST_VERSION)
    result = asyncio.run(srv.model_inspect.fn(
        model="account.move",
        method="summary",
        odoo_version=TEST_VERSION,
    ))
    # Wrapper returns ToolResult; extract text content
    assert result.content, "model_inspect returned empty content"
    wrapper_text = result.content[0].text
    # The first line (header) must match between direct call and wrapper
    direct_header = direct.split("\n")[0]
    wrapper_header = wrapper_text.split("\n")[0]
    assert direct_header == wrapper_header, (
        f"model_inspect header mismatch.\n"
        f"  direct:  {direct_header!r}\n"
        f"  wrapper: {wrapper_header!r}"
    )
    assert "account.move" in wrapper_text
    assert TEST_VERSION in wrapper_text


def test_model_inspect_invalid_method(seeded_neo4j):
    """model_inspect with an unknown method returns an Error: string."""
    srv = _import_server_module()
    result = asyncio.run(srv.model_inspect.fn(
        model="account.move",
        method="nonexistent",
        odoo_version=TEST_VERSION,
    ))
    text = result.content[0].text
    assert text.startswith("Error:"), f"Expected Error:, got: {text[:80]!r}"
    assert "nonexistent" in text


def test_entity_lookup_routes_to_resolve_model(seeded_neo4j):
    """entity_lookup(kind='model') output must match _resolve_model (AC-D3-5)."""
    import asyncio

    srv = _import_server_module()
    direct = srv._resolve_model("account.move", TEST_VERSION)
    # entity_lookup is async (#227 — offloads the blocking body off the event
    # loop); drive it via asyncio.run to exercise the real tool contract.
    result = asyncio.run(srv.entity_lookup.fn(
        kind="model",
        model="account.move",
        odoo_version=TEST_VERSION,
    ))
    wrapper_text = result.content[0].text
    direct_header = direct.split("\n")[0]
    wrapper_header = wrapper_text.split("\n")[0]
    assert direct_header == wrapper_header, (
        f"entity_lookup header mismatch.\n"
        f"  direct:  {direct_header!r}\n"
        f"  wrapper: {wrapper_header!r}"
    )


def test_entity_lookup_invalid_kind(seeded_neo4j):
    """entity_lookup with an unknown kind returns an Error: string."""
    import asyncio

    srv = _import_server_module()
    # WI-4: odoo_version is now hard-required on entity_lookup; pass it
    # explicitly (the bogus-kind error path is what we are exercising).
    result = asyncio.run(srv.entity_lookup.fn(kind="bogus", odoo_version=TEST_VERSION))
    text = result.content[0].text
    assert text.startswith("Error:"), f"Expected Error:, got: {text[:80]!r}"
    assert "bogus" in text


# ===========================================================================
# Wave E — Session-context tools smoke tests (AC-E3-7)
# ===========================================================================


def test_set_active_version_sentinel_rejected(seeded_neo4j):
    """set_active_version with a sentinel string returns an Error: message (AC-E3-5)."""
    srv = _import_server_module()
    for sentinel in ("auto", "default", "latest", "version", "any", ""):
        result = asyncio.run(srv.set_active_version.fn(odoo_version=sentinel))
        text = result.content[0].text
        assert "Error" in text or "sentinel" in text.lower(), (
            f"Expected sentinel rejection for {sentinel!r}, got: {text[:120]!r}"
        )


def test_set_active_version_persists_then_resolve_model_uses_it(seeded_neo4j):
    """AC-E3-7: set_active_version('99.0') + resolve_model(odoo_version='auto') → '99.0'.

    Mocks PG write (set_active_version_db) and PG/cache read (get_session_state)
    so the round-trip exercises _resolve_version delegation without real DB.
    """
    from unittest.mock import patch

    from src.mcp.session import SessionState

    srv = _import_server_module()

    # --- Phase 1: set_active_version via wrapper --------------------------------
    with patch("src.mcp.session.set_active_version_db") as mock_set:
        result = asyncio.run(srv.set_active_version.fn(odoo_version=TEST_VERSION))
        text = result.content[0].text
        assert TEST_VERSION in text, (
            f"set_active_version confirmation should contain the version. Got: {text!r}"
        )
        # #251: the wrapper now threads the per-session mcp_session_id as a 3rd
        # positional. No HTTP request is bound in this test, so it resolves to
        # the single-session sentinel ('_nosession').
        mock_set.assert_called_once_with(
            srv._get_api_key_id(), TEST_VERSION, srv._get_mcp_session_id()
        )

    # --- Phase 2: resolve_model with odoo_version='auto' uses session state -----
    # Simulate the DB returning the stored session (as if set_active_version had
    # written it). resolve_model → _resolve_version → resolve_version_v2 → Tier 2.
    stored_state = SessionState(
        api_key_id=srv._get_api_key_id(),
        odoo_version=TEST_VERSION,
        profile_name=None,
    )
    with patch("src.mcp.session.get_session_state", return_value=stored_state):
        text = srv._resolve_model("account.move", "auto")

    assert TEST_VERSION in text, (
        f"resolve_model with 'auto' should use session-stored version {TEST_VERSION!r}.\n"
        f"Output: {text[:300]!r}"
    )
    assert "account.move" in text


def _make_profile_found_pg_checkout():
    """Return a context manager that simulates a Postgres connection with the profile present.

    fetchone() returns (1,) — profile found; fetchall() returns [] (unused in happy path).
    """
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    @contextmanager
    def _mock():
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = lambda s: s
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = (1,)   # profile exists → happy path
        cur.fetchall.return_value = []
        conn.cursor.return_value = cur
        yield conn
    return _mock


def test_set_active_profile_returns_confirmation(seeded_neo4j):
    """set_active_profile stores profile name and returns confirmation."""
    from unittest.mock import patch

    srv = _import_server_module()

    checkout = _make_profile_found_pg_checkout()
    with (
        patch("src.mcp.server._checkout_pg", checkout),
        patch("src.mcp.session.set_active_profile_db") as mock_set,
    ):
        result = asyncio.run(srv.set_active_profile.fn(profile_name="my-erp-prod"))
        text = result.content[0].text
        assert "my-erp-prod" in text, f"Expected profile name in confirmation: {text!r}"
        # #251: 3rd positional mcp_session_id (no HTTP request → '_nosession').
        mock_set.assert_called_once_with(
            srv._get_api_key_id(), "my-erp-prod", srv._get_mcp_session_id()
        )


def test_set_active_profile_clear(seeded_neo4j):
    """set_active_profile(None) clears the active profile."""
    from unittest.mock import patch

    srv = _import_server_module()

    with patch("src.mcp.session.set_active_profile_db") as mock_set:
        result = asyncio.run(srv.set_active_profile.fn(profile_name=None))
        text = result.content[0].text
        assert "cleared" in text.lower(), f"Expected 'cleared' in response: {text!r}"
        # #251: 3rd positional mcp_session_id (no HTTP request → '_nosession').
        mock_set.assert_called_once_with(
            srv._get_api_key_id(), None, srv._get_mcp_session_id()
        )


def test_list_available_versions_returns_tree(seeded_neo4j):
    """list_available_versions returns a tree of indexed versions (AC-E3-1)."""
    srv = _import_server_module()
    result = asyncio.run(srv.list_available_versions.fn())
    text = result.content[0].text
    # seeded_neo4j uses TEST_VERSION = "99.0" — it must appear in the list
    assert TEST_VERSION in text, (
        f"Expected {TEST_VERSION!r} in list_available_versions output.\n"
        f"Got: {text[:300]!r}"
    )
    assert "Indexed Odoo versions" in text


def test_mcp_resources_registered(seeded_neo4j):
    """The 7 documented odoo:// resource URI templates are registered (WI-F3, ADR-0030).

    Asserts the public, behaviour-level contract — the exact set of registered
    resource-template URIs an MCP client can list — via FastMCP's supported
    ``get_resource_templates()`` API.  We deliberately do NOT poke the internal
    ``_resource_manager._templates`` attribute: the business invariant is which
    URI templates the server exposes, not how FastMCP stores them.
    """
    srv = _import_server_module()
    templates = asyncio.run(srv.mcp.get_resource_templates())
    expected_uris = {
        "odoo://{version}/model/{name}",
        "odoo://{version}/field/{model}/{field}",
        "odoo://{version}/method/{model}/{method}",
        "odoo://{version}/module/{name}",
        "odoo://{version}/view/{xmlid}",
        "odoo://{version}/pattern/{pattern_id}",
        "odoo://{version}/stylesheet/{module}/{file_path*}",
    }
    registered_uris = set(templates.keys())
    assert registered_uris == expected_uris, (
        "Registered resource URI templates do not match the documented set.\n"
        f"  missing: {expected_uris - registered_uris}\n"
        f"  unexpected: {registered_uris - expected_uris}"
    )


# ===========================================================================
# M10A D2 — Magic-fields auto-injection
# ===========================================================================

_MAGIC_FIELD_NAMES = ("id", "display_name", "create_uid", "create_date",
                      "write_uid", "write_date")
_M10A_D2_VERSION = "74.0"


def _cleanup_d2(driver):
    _cleanup_version(driver, _M10A_D2_VERSION)


def test_list_fields_includes_magic_fields(neo4j_driver):
    """_list_fields returns all 6 magic fields as synthetic rows (D2)."""
    _cleanup_d2(neo4j_driver)
    try:
        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        writer.setup_indexes()
        mod = ModuleInfo("sale", _M10A_D2_VERSION, "odoo_test", "/tmp", [], "")
        mdl = ModelInfo(
            name="sale.order", module="sale", odoo_version=_M10A_D2_VERSION,
            fields=[FieldInfo("name", "char")],
            methods=[],
        )
        writer.write_results([ParseResult(module=mod, models=[mdl])])
        writer.close()

        srv = _import_server_module()
        out = srv._list_fields("sale.order", _M10A_D2_VERSION)
        for magic in _MAGIC_FIELD_NAMES:
            assert magic in out, (
                f"Magic field '{magic}' missing from _list_fields output.\n{out}"
            )
        assert "<builtin>" in out, "Expected '<builtin>' module marker in output"
    finally:
        _cleanup_d2(neo4j_driver)


def test_list_fields_dedup_magic_field_overridden(neo4j_driver):
    """When model declares 'id' itself, synthetic magic 'id' must not be duplicated (D2)."""
    _cleanup_d2(neo4j_driver)
    try:
        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        writer.setup_indexes()
        mod = ModuleInfo("sale", _M10A_D2_VERSION, "odoo_test", "/tmp", [], "")
        mdl = ModelInfo(
            name="sale.order", module="sale", odoo_version=_M10A_D2_VERSION,
            fields=[FieldInfo("id", "integer")],
            methods=[],
        )
        writer.write_results([ParseResult(module=mod, models=[mdl])])
        writer.close()

        srv = _import_server_module()
        out = srv._list_fields("sale.order", _M10A_D2_VERSION)
        # Count occurrences of the field name in output — must be exactly 1.
        # Use " id :" as sentinel so tree-connector prefixes (│, ├─, └─) do not
        # interfere with startswith-based checks (U+2502 is not stripped by str.strip()).
        id_occurrences = sum(
            1 for line in out.splitlines()
            if " id :" in line
        )
        assert id_occurrences == 1, (
            f"'id' appears {id_occurrences} times — expected exactly 1 (dedup).\n{out}"
        )
    finally:
        _cleanup_d2(neo4j_driver)


def test_resolve_field_magic_field_synthetic(neo4j_driver):
    """_resolve_field for magic field 'id' returns synthetic builtin info (D2)."""
    _cleanup_d2(neo4j_driver)
    try:
        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        writer.setup_indexes()
        mod = ModuleInfo("sale", _M10A_D2_VERSION, "odoo_test", "/tmp", [], "")
        mdl = ModelInfo(
            name="sale.order", module="sale", odoo_version=_M10A_D2_VERSION,
            fields=[FieldInfo("name", "char")],
            methods=[],
        )
        writer.write_results([ParseResult(module=mod, models=[mdl])])
        writer.close()

        srv = _import_server_module()
        out = srv._resolve_field("sale.order", "id", _M10A_D2_VERSION)
        assert "integer" in out.lower(), f"Expected 'integer' type for magic 'id'.\n{out}"
        assert "<builtin>" in out, f"Expected '<builtin>' marker.\n{out}"
    finally:
        _cleanup_d2(neo4j_driver)


def test_resolve_field_magic_not_shown_when_from_module_set(neo4j_driver):
    """_resolve_field with from_module set suppresses magic-field synthetic rows (D2+D3)."""
    _cleanup_d2(neo4j_driver)
    try:
        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        writer.setup_indexes()
        mod = ModuleInfo("sale", _M10A_D2_VERSION, "odoo_test", "/tmp", [], "")
        mdl = ModelInfo(
            name="sale.order", module="sale", odoo_version=_M10A_D2_VERSION,
            fields=[FieldInfo("name", "char")],
            methods=[],
        )
        writer.write_results([ParseResult(module=mod, models=[mdl])])
        writer.close()

        srv = _import_server_module()
        # 'id' is only a magic field; from_module='sale' should not match '<builtin>'
        out = srv._resolve_field("sale.order", "id", _M10A_D2_VERSION, from_module="sale")
        assert "not found" in out.lower(), (
            f"Expected 'not found' when from_module='sale' for magic-only 'id'.\n{out}"
        )
    finally:
        _cleanup_d2(neo4j_driver)


# ===========================================================================
# M10A D3 — from_module param
# ===========================================================================

_M10A_D3_VERSION = "73.0"


def _cleanup_d3(driver):
    _cleanup_version(driver, _M10A_D3_VERSION)


def _seed_d3(neo4j_driver):
    """Seed two-module setup: base 'sale' + extension 'sale_ext'."""
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()
    base_mod = ModuleInfo("sale", _M10A_D3_VERSION, "odoo_test", "/tmp", [], "")
    base_mdl = ModelInfo(
        name="sale.order", module="sale", odoo_version=_M10A_D3_VERSION,
        fields=[FieldInfo("name", "char"), FieldInfo("amount_total", "float")],
        methods=[],
    )
    ext_mod = ModuleInfo("sale_ext", _M10A_D3_VERSION, "addons_test", "/tmp", ["sale"], "")
    ext_mdl = ModelInfo(
        name="sale.order", module="sale_ext", odoo_version=_M10A_D3_VERSION,
        inherit=["sale.order"],
        fields=[FieldInfo("x_note", "text")],
        methods=[],
    )
    writer.write_results([
        ParseResult(module=base_mod, models=[base_mdl]),
        ParseResult(module=ext_mod, models=[ext_mdl]),
    ])
    writer.close()


def test_resolve_model_from_module_filters(neo4j_driver):
    """_resolve_model(from_module='sale') shows only the sale module (D3)."""
    _cleanup_d3(neo4j_driver)
    try:
        _seed_d3(neo4j_driver)
        srv = _import_server_module()
        out = srv._resolve_model("sale.order", _M10A_D3_VERSION, from_module="sale")
        assert "sale" in out, f"Expected 'sale' in output.\n{out}"
        assert "sale_ext" not in out, (
            f"Expected 'sale_ext' to be filtered out when from_module='sale'.\n{out}"
        )
    finally:
        _cleanup_d3(neo4j_driver)


def test_resolve_model_from_module_nonexistent_returns_not_found(neo4j_driver):
    """_resolve_model with nonexistent from_module returns not-found hint (D3)."""
    _cleanup_d3(neo4j_driver)
    try:
        _seed_d3(neo4j_driver)
        srv = _import_server_module()
        out = srv._resolve_model("sale.order", _M10A_D3_VERSION, from_module="no_such_module")
        assert "not found" in out.lower(), (
            f"Expected 'not found' for nonexistent from_module.\n{out}"
        )
    finally:
        _cleanup_d3(neo4j_driver)


def test_resolve_model_from_module_none_returns_all(neo4j_driver):
    """_resolve_model(from_module=None) shows all modules — backward compat (D3)."""
    _cleanup_d3(neo4j_driver)
    try:
        _seed_d3(neo4j_driver)
        srv = _import_server_module()
        out = srv._resolve_model("sale.order", _M10A_D3_VERSION)
        assert "sale" in out, f"Expected 'sale' in output.\n{out}"
        # sale_ext shows under 'Extended by' when default None
        assert "sale_ext" in out, (
            f"Expected 'sale_ext' visible when from_module=None.\n{out}"
        )
    finally:
        _cleanup_d3(neo4j_driver)


def test_resolve_field_from_module_filters(neo4j_driver):
    """_resolve_field(from_module='sale_ext') shows only sale_ext declaration (D3)."""
    _cleanup_d3(neo4j_driver)
    try:
        _seed_d3(neo4j_driver)
        srv = _import_server_module()
        # amount_total is only in base 'sale' — from_module='sale_ext' yields not found
        out = srv._resolve_field(
            "sale.order", "amount_total", _M10A_D3_VERSION, from_module="sale_ext",
        )
        assert "not found" in out.lower(), (
            f"amount_total should not be visible under sale_ext.\n{out}"
        )
        # x_note is only in sale_ext
        out2 = srv._resolve_field(
            "sale.order", "x_note", _M10A_D3_VERSION, from_module="sale_ext",
        )
        assert "sale_ext" in out2, (
            f"x_note should be visible under from_module='sale_ext'.\n{out2}"
        )
    finally:
        _cleanup_d3(neo4j_driver)


# ===========================================================================
# B1 — render provenance/intent fields (WI-B1)
# One focused positive assertion per changed tool confirming the new rendered
# line appears when the property is set.  Uses dedicated version 76.0 to avoid
# conflict with all existing version slots.
# ===========================================================================

_B1_VERSION = "76.0"


def _cleanup_b1(neo4j_driver):
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n",
            v=_B1_VERSION,
        )


@pytest.fixture(scope="module")
def b1_seed(neo4j_driver):
    """Seed ALL B1 provenance/intent data once (WS-C / DD1 §1b Cluster G).

    Each B1 test is a pure READ of one rendered line and asserts only its own
    property; no test asserts the ABSENCE of another's data, so a single seed
    is safe.  Consolidated entities (all at ``_B1_VERSION``):
      * module ``sale`` (repo=odoo_community, path=/opt/odoo/addons/sale,
        is_definition) → describe_module Repo/Path + ADR-0037 relativization
      * model ``sale.order`` with fields ``partner_id``(many2one→res.partner),
        ``amount_total``(monetary), ``name``(char) → resolve_field Comodel +
        list_fields comodel/ttype
      * method ``sale.order.action_confirm`` (signature/convention) → resolve_method
      * view ``sale.b1_view_form`` (name) → resolve_view String
      * module ``web_sale`` OWL component ``SaleKanban`` (template) → list_owl
      * module ``sale`` JSPatch with non-empty file_path → list_js_patches

    Seed once at module setup, safety-wipe once at teardown (before+after at the
    fixture boundary — never before-only, M3).
    """
    from src.indexer.models import ViewInfo, ViewParseResult

    _cleanup_b1(neo4j_driver)  # before: defensive clean

    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()
    # sale module + sale.order model: repo/path for describe_module +
    # all fields + the action_confirm method.
    mod = ModuleInfo(
        "sale", _B1_VERSION, "odoo_community", "/opt/odoo/addons/sale", [], "17.0",
    )
    model = ModelInfo(
        name="sale.order", module="sale", odoo_version=_B1_VERSION,
        fields=[
            FieldInfo("partner_id", "many2one", comodel_name="res.partner"),
            FieldInfo("amount_total", "monetary"),
            FieldInfo("name", "char"),
        ],
        methods=[MethodInfo(
            "action_confirm", has_super_call=True,
            convention_kind="action",
            signature="self",
        )],
    )
    model.had_explicit_name = True
    writer.write_results([ParseResult(module=mod, models=[model])])

    base_mod = ModuleInfo("sale", _B1_VERSION, "odoo_community",
                          "/opt/odoo/addons/sale", [], "17.0")
    base_view = ViewInfo(
        xmlid="sale.b1_view_form",
        name="Sale Order Form",
        model="sale.order",
        module="sale",
        odoo_version=_B1_VERSION,
        view_type="form",
        mode="primary",
        inherit_xmlid=None,
    )
    writer.write_view_results([ViewParseResult(module=base_mod, views=[base_view])])
    writer.close()

    with neo4j_driver.session() as session:
        session.run(
            "MATCH (m:Model {name:'sale.order', module:'sale', odoo_version:$v})"
            " SET m.is_definition = true",
            v=_B1_VERSION,
        )
        # JSPatch with a non-empty file_path (writer/seed helper sets '').
        session.run(
            "MATCH (mod:Module {name: 'sale', odoo_version: $v}) "
            "MERGE (j:JSPatch {target: 'FormController', patch_name: 'onLoad',"
            "                  module: 'sale', odoo_version: $v}) "
            "SET j.era = 'patch',"
            "    j.file_path = 'sale/static/src/js/form_controller.js' "
            "MERGE (j)-[:DEFINED_IN]->(mod)",
            v=_B1_VERSION,
        )

    # OWL component in a distinct module (web_sale) so the sale-module reads
    # are not perturbed.
    seed_owl_components(
        neo4j_driver, module="web_sale",
        odoo_version=_B1_VERSION,
        components=[
            {"name": "SaleKanban", "bound_model": "sale.order",
             "template": "sale_management.SaleKanban"},
        ],
    )

    yield _B1_VERSION

    _cleanup_b1(neo4j_driver)  # after: honour before+after invariant (M3)


def test_b1_resolve_field_renders_comodel(b1_seed):
    """B1: resolve_field renders 'Comodel:' line for relational fields."""
    srv = _import_server_module()
    out = srv._resolve_field("sale.order", "partner_id", b1_seed)
    assert "Comodel:" in out, f"B1: expected 'Comodel:' line in resolve_field output.\n{out}"
    assert "res.partner" in out, f"B1: expected comodel name in output.\n{out}"


def test_b1_resolve_method_renders_signature_and_convention(b1_seed):
    """B1: resolve_method renders 'Signature:' and 'Convention:' from Method node."""
    srv = _import_server_module()
    out = srv._resolve_method("sale.order", "action_confirm", b1_seed)
    assert "Signature:" in out, (
        f"B1: expected 'Signature:' line in resolve_method output.\n{out}"
    )
    assert "Convention:" in out, (
        f"B1: expected 'Convention:' line in resolve_method output.\n{out}"
    )
    assert "action" in out, f"B1: expected convention_kind='action' in output.\n{out}"


def test_b1_resolve_view_renders_string(b1_seed):
    """B1: resolve_view renders 'String:' line when View.name is non-empty."""
    srv = _import_server_module()
    out = srv._resolve_view("sale.b1_view_form", b1_seed)
    assert "String:" in out, (
        f"B1: expected 'String:' line in resolve_view output.\n{out}"
    )
    assert "Sale Order Form" in out, (
        f"B1: expected view name in output.\n{out}"
    )


def test_b1_describe_module_renders_repo_and_path(b1_seed):
    """B1: describe_module renders 'Repo:' and 'Path:' lines — #1 agent navigation fix."""
    srv = _import_server_module()
    out = srv._describe_module("sale", b1_seed)
    assert "Repo:" in out, f"B1: expected 'Repo:' line in describe_module output.\n{out}"
    assert "odoo_community" in out, f"B1: expected repo value in output.\n{out}"
    assert "Path:" in out, f"B1: expected 'Path:' line in describe_module output.\n{out}"
    # ADR-0037: Path must be repo-relative — the server-absolute prefix must
    # NOT leak to the client, but the relative tail stays for navigation.
    assert "/opt/odoo/addons/sale" not in out, (
        f"B1: absolute server path must not leak to client.\n{out}"
    )
    assert "addons/sale" in out, (
        f"B1: expected repo-relative path tail in output.\n{out}"
    )


def test_b1_list_fields_renders_comodel(b1_seed):
    """B1: list_fields row formatter includes comodel for relational fields."""
    srv = _import_server_module()
    out = srv._list_fields("sale.order", b1_seed)
    # many2one field with comodel should include "-> res.partner" in the row
    assert "-> res.partner" in out, (
        f"B1: expected '-> res.partner' comodel in list_fields row.\n{out}"
    )
    # plain monetary field has no comodel — just ttype
    assert "amount_total : monetary" in out


def test_b1_list_owl_components_renders_template(b1_seed):
    """B1: list_owl_components includes template path per row when set."""
    srv = _import_server_module()
    out = srv._list_owl_components("web_sale", b1_seed)
    assert "template=sale_management.SaleKanban" in out, (
        f"B1: expected 'template=...' in list_owl_components row.\n{out}"
    )


def test_b1_list_js_patches_renders_file_path(b1_seed):
    """B1: list_js_patches includes file_path when non-empty."""
    srv = _import_server_module()
    out = srv._list_js_patches(b1_seed, module="sale")
    assert "sale/static/src/js/form_controller.js" in out, (
        f"B1: expected file_path in list_js_patches row.\n{out}"
    )


# ---------------------------------------------------------------------------
# WG-4: T1 — F-4 load-order fix (_module_dep_closure)
# ---------------------------------------------------------------------------

_WG4_DEP_VERSION = "75.0"


def _cleanup_wg4_dep(driver):
    with driver.session() as session:
        session.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n",
            v=_WG4_DEP_VERSION,
        )


def test_dep_closure_load_order_deepest_first(neo4j_driver):
    """T1 (F-4): load order places deepest transitive dep (C) before intermediate (B) before root
    requester.  Chain: A depends B, B depends C.  Odoo loads C first, then B.
    index 1 = C (deepest), index 2 = B (direct dep of A).
    """
    _cleanup_wg4_dep(neo4j_driver)
    try:
        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        writer.setup_indexes()

        # A depends on B; B depends on C.
        mod_c = ModuleInfo("wg4_mod_c", _WG4_DEP_VERSION, "test_repo", "/tmp", [], "")
        mod_b = ModuleInfo("wg4_mod_b", _WG4_DEP_VERSION, "test_repo", "/tmp", ["wg4_mod_c"], "")
        mod_a = ModuleInfo("wg4_mod_a", _WG4_DEP_VERSION, "test_repo", "/tmp", ["wg4_mod_b"], "")
        writer.write_results([
            ParseResult(module=mod_c, models=[]),
            ParseResult(module=mod_b, models=[]),
            ParseResult(module=mod_a, models=[]),
        ])
        writer.close()

        srv = _import_server_module()
        out = srv._module_dep_closure("wg4_mod_a", _WG4_DEP_VERSION)

        assert "wg4_mod_b" in out, f"Expected wg4_mod_b in output:\n{out}"
        assert "wg4_mod_c" in out, f"Expected wg4_mod_c in output:\n{out}"

        # Index 1 must be wg4_mod_c (deepest, loaded first); index 2 = wg4_mod_b.
        # The output format is "  1. wg4_mod_c" and "  2. wg4_mod_b".
        pos_b = out.index("wg4_mod_b")
        pos_c = out.index("wg4_mod_c")
        assert pos_c < pos_b, (
            f"T1 (F-4): deepest dep (wg4_mod_c) must appear BEFORE wg4_mod_b in load order.\n"
            f"wg4_mod_c at char {pos_c}, wg4_mod_b at char {pos_b}.\n{out}"
        )

        # Confirm index 1 is attached to wg4_mod_c, not wg4_mod_b.
        lines = out.splitlines()
        idx1_line = next((ln for ln in lines if "1." in ln), "")
        assert "wg4_mod_c" in idx1_line, (
            f"T1 (F-4): index 1 must be wg4_mod_c (deepest dep).  Got: {idx1_line!r}\n{out}"
        )
    finally:
        _cleanup_wg4_dep(neo4j_driver)


def test_dep_closure_diamond_no_duplicate(neo4j_driver):
    """T1 (F-4): diamond dependency A->B->D + A->C->D should list D once."""
    _cleanup_wg4_dep(neo4j_driver)
    try:
        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        writer.setup_indexes()

        mod_d = ModuleInfo("wg4_dmd_d", _WG4_DEP_VERSION, "test_repo", "/tmp", [], "")
        mod_b = ModuleInfo("wg4_dmd_b", _WG4_DEP_VERSION, "test_repo", "/tmp", ["wg4_dmd_d"], "")
        mod_c = ModuleInfo("wg4_dmd_c", _WG4_DEP_VERSION, "test_repo", "/tmp", ["wg4_dmd_d"], "")
        mod_a = ModuleInfo(
            "wg4_dmd_a", _WG4_DEP_VERSION, "test_repo", "/tmp",
            ["wg4_dmd_b", "wg4_dmd_c"], "",
        )
        writer.write_results([
            ParseResult(module=mod_d, models=[]),
            ParseResult(module=mod_b, models=[]),
            ParseResult(module=mod_c, models=[]),
            ParseResult(module=mod_a, models=[]),
        ])
        writer.close()

        srv = _import_server_module()
        out = srv._module_dep_closure("wg4_dmd_a", _WG4_DEP_VERSION)

        # wg4_dmd_d appears exactly once (DISTINCT in Cypher).
        assert out.count("wg4_dmd_d") == 1, (
            f"T1 (F-4): diamond dep wg4_dmd_d should appear exactly once.\n{out}"
        )
    finally:
        _cleanup_wg4_dep(neo4j_driver)


# ---------------------------------------------------------------------------
# WG-4: T2 — LIST list↔tree alias (_list_views_core)
# ---------------------------------------------------------------------------

_WG4_VIEWS_VERSION = "74.0"


def _cleanup_wg4_views(driver):
    with driver.session() as session:
        session.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n",
            v=_WG4_VIEWS_VERSION,
        )


def test_view_type_tree_query_matches_list_stored_node(neo4j_driver):
    """T2 (LIST): querying view_type='tree' must match View nodes with type='list' (v18 style)."""
    _cleanup_wg4_views(neo4j_driver)
    try:
        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        writer.setup_indexes()

        mod = ModuleInfo("wg4_sale", _WG4_VIEWS_VERSION, "odoo_test", "/tmp", [], "")
        # Store a view with type='list' (v18 DB value).
        list_view = ViewInfo(
            xmlid="wg4_sale.view_order_list", name="list view",
            model="wg4.sale.order", module="wg4_sale",
            odoo_version=_WG4_VIEWS_VERSION,
            view_type="list", mode="primary", inherit_xmlid=None,
        )
        # Store a view with type='tree' (v17 DB value).
        tree_view = ViewInfo(
            xmlid="wg4_sale.view_order_tree", name="tree view",
            model="wg4.sale.order", module="wg4_sale",
            odoo_version=_WG4_VIEWS_VERSION,
            view_type="tree", mode="primary", inherit_xmlid=None,
        )
        writer.write_view_results([
            ViewParseResult(module=mod, views=[list_view, tree_view]),
        ])
        writer.close()

        srv = _import_server_module()

        # Query with view_type='tree' — must see BOTH the 'tree' node AND the 'list' node.
        out_tree = srv._list_views("wg4.sale.order", _WG4_VIEWS_VERSION, view_type="tree")
        assert "wg4_sale.view_order_list" in out_tree, (
            f"T2: view_type='tree' must match nodes with type='list' (v18 alias).\n{out_tree}"
        )
        assert "wg4_sale.view_order_tree" in out_tree, (
            f"T2: view_type='tree' must still match nodes with type='tree'.\n{out_tree}"
        )

        # Query with view_type='list' — must see BOTH (symmetric alias).
        out_list = srv._list_views("wg4.sale.order", _WG4_VIEWS_VERSION, view_type="list")
        assert "wg4_sale.view_order_tree" in out_list, (
            f"T2: view_type='list' must match nodes with type='tree' (v17 alias).\n{out_list}"
        )
        assert "wg4_sale.view_order_list" in out_list, (
            f"T2: view_type='list' must still match nodes with type='list'.\n{out_list}"
        )

        # Other view types must NOT match (sanity check alias is not a wildcard).
        form_view = ViewInfo(
            xmlid="wg4_sale.view_order_form", name="form view",
            model="wg4.sale.order", module="wg4_sale",
            odoo_version=_WG4_VIEWS_VERSION,
            view_type="form", mode="primary", inherit_xmlid=None,
        )
        writer2 = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        writer2.write_view_results([ViewParseResult(module=mod, views=[form_view])])
        writer2.close()

        out_tree2 = srv._list_views("wg4.sale.order", _WG4_VIEWS_VERSION, view_type="tree")
        assert "wg4_sale.view_order_form" not in out_tree2, (
            f"T2: view_type='tree' must NOT match form views.\n{out_tree2}"
        )
    finally:
        _cleanup_wg4_views(neo4j_driver)
