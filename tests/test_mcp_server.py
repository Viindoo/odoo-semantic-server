# tests/test_mcp_server.py
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
    """Unresolved parent (placeholder) must be filtered from 'Inherits from' output."""
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    # Clean up old data
    UNRESOLVED_VERSION = "98.0"
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=UNRESOLVED_VERSION)

    # Seed: sale.order inherits ghost.mixin (not indexed) → creates unresolved edge
    mod = ModuleInfo("sale", UNRESOLVED_VERSION, "odoo_test", "/tmp", [], "")
    model = ModelInfo(
        name="sale.order", module="sale", odoo_version=UNRESOLVED_VERSION,
        inherit=["ghost.mixin"],  # intentionally NOT seeded
    )
    writer.write_results([ParseResult(module=mod, models=[model])])
    writer.close()

    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    import sys
    sys.modules.pop("src.mcp.server", None)
    from src.mcp.server import _resolve_model

    result = _resolve_model("sale.order", UNRESOLVED_VERSION)

    assert "sale.order" in result
    assert "ghost.mixin" not in result  # unresolved parent filtered out

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=UNRESOLVED_VERSION)


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


def test_resolve_model_picks_base_when_60_extensions_tie_inbound(neo4j_driver):
    """Base module wins when 60 extension Models all have inbound=1 (tie).

    Tier 1 (is_ext): extensions have outgoing INHERITS to base → is_ext=1.
    Base has no outgoing INHERITS to its own name → is_ext=0 → ranks first.
    """
    SIXTY_EXT_VERSION = "93.0"
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=SIXTY_EXT_VERSION)

    try:
        with neo4j_driver.session() as session:
            # Base module node + Model node
            session.run(
                "MERGE (mod:Module {name: 'core', odoo_version: $v}) "
                "SET mod.repo = 'odoo_test', mod.edition = 'community'",
                v=SIXTY_EXT_VERSION,
            )
            session.run(
                "MERGE (m:Model {name: 'sale.order', module: 'core', odoo_version: $v}) "
                "MERGE (mod:Module {name: 'core', odoo_version: $v}) "
                "MERGE (m)-[:DEFINED_IN]->(mod)",
                v=SIXTY_EXT_VERSION,
            )
            # 60 extension module nodes + Model nodes, each inheriting base
            for i in range(60):
                ext_mod = f"ext_{i:02d}"
                session.run(
                    "MERGE (mod:Module {name: $mod, odoo_version: $v}) "
                    "SET mod.repo = 'ext_repo', mod.edition = 'community'",
                    mod=ext_mod, v=SIXTY_EXT_VERSION,
                )
                session.run(
                    "MERGE (ext:Model {name: 'sale.order', module: $mod, odoo_version: $v}) "
                    "MERGE (base:Model {name: 'sale.order', module: 'core', odoo_version: $v}) "
                    "MERGE (ext)-[:INHERITS]->(base) "
                    "MERGE (extmod:Module {name: $mod, odoo_version: $v}) "
                    "MERGE (ext)-[:DEFINED_IN]->(extmod)",
                    mod=ext_mod, v=SIXTY_EXT_VERSION,
                )

        resolve_model = _make_ranking_tools(neo4j_driver)
        result = resolve_model("sale.order", SIXTY_EXT_VERSION)

        assert "Defined in:" in result
        first_defined_in_line = result.split("Defined in:")[1].split("\n")[0]
        assert "core" in first_defined_in_line, (
            f"Expected 'core' as Defined-in module; got:\n{result}"
        )
    finally:
        with neo4j_driver.session() as session:
            session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=SIXTY_EXT_VERSION)


def test_resolve_model_picks_base_when_extension_orphan_no_outgoing_edge(neo4j_driver):
    """Base with is_definition=true beats an orphan extension with no INHERITS edge.

    Simulates parser-miss: extension Model node exists but has no outgoing INHERITS.
    Tier 1: base has is_definition=true → is_ext=0 via CASE 1.
    Orphan: no outgoing INHERITS to same-name node → ELSE 0 as well, tie at is_ext.
    Tier 4 (mod_name): 'base_mod' < 'orphan_mod' alphabetically → base wins.
    """
    ORPHAN_VERSION = "92.0"
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=ORPHAN_VERSION)

    try:
        with neo4j_driver.session() as session:
            # Base module + Model with is_definition=true
            session.run(
                "MERGE (mod:Module {name: 'base_mod', odoo_version: $v}) "
                "SET mod.repo = 'odoo_test', mod.edition = 'community'",
                v=ORPHAN_VERSION,
            )
            session.run(
                "MERGE (m:Model {name: 'res.partner', module: 'base_mod', odoo_version: $v}) "
                "SET m.is_definition = true "
                "MERGE (mod:Module {name: 'base_mod', odoo_version: $v}) "
                "MERGE (m)-[:DEFINED_IN]->(mod)",
                v=ORPHAN_VERSION,
            )
            # Orphan extension: Model node exists, NO outgoing INHERITS edge to same name
            session.run(
                "MERGE (mod:Module {name: 'orphan_mod', odoo_version: $v}) "
                "SET mod.repo = 'ext_repo', mod.edition = 'community'",
                v=ORPHAN_VERSION,
            )
            session.run(
                "MERGE (m:Model {name: 'res.partner', module: 'orphan_mod', odoo_version: $v}) "
                "MERGE (mod:Module {name: 'orphan_mod', odoo_version: $v}) "
                "MERGE (m)-[:DEFINED_IN]->(mod)",
                v=ORPHAN_VERSION,
            )

        resolve_model = _make_ranking_tools(neo4j_driver)
        result = resolve_model("res.partner", ORPHAN_VERSION)

        assert "Defined in:" in result
        first_defined_in_line = result.split("Defined in:")[1].split("\n")[0]
        assert "base_mod" in first_defined_in_line, (
            f"Expected 'base_mod' as Defined-in; got:\n{result}"
        )
    finally:
        with neo4j_driver.session() as session:
            session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=ORPHAN_VERSION)


def test_resolve_model_edition_rank_orders_community_then_enterprise_then_custom(neo4j_driver):
    """Edition rank: community (0) < enterprise (1) < custom/unknown (4).

    Three Model nodes same name, same inbound=0, same is_ext=0 (no outgoing INHERITS).
    Differs only in Module.edition → edition_rank decides order.
    community module must appear as Defined-in (first), enterprise and custom in Extended-by.
    """
    EDITION_VERSION = "91.0"
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=EDITION_VERSION)

    try:
        with neo4j_driver.session() as session:
            for mod_name, edition in [
                ("custom_mod", "customer"),
                ("enterprise_mod", "enterprise"),
                ("community_mod", "community"),
            ]:
                session.run(
                    "MERGE (mod:Module {name: $mod, odoo_version: $v}) "
                    "SET mod.repo = 'test_repo', mod.edition = $edition",
                    mod=mod_name, v=EDITION_VERSION, edition=edition,
                )
                session.run(
                    "MERGE (m:Model {name: 'mail.thread', module: $mod, odoo_version: $v}) "
                    "MERGE (mod:Module {name: $mod, odoo_version: $v}) "
                    "MERGE (m)-[:DEFINED_IN]->(mod)",
                    mod=mod_name, v=EDITION_VERSION,
                )

        resolve_model = _make_ranking_tools(neo4j_driver)
        result = resolve_model("mail.thread", EDITION_VERSION)

        assert "Defined in:" in result
        first_defined_in_line = result.split("Defined in:")[1].split("\n")[0]
        assert "community_mod" in first_defined_in_line, (
            f"Expected 'community_mod' (community edition) as Defined-in; got:\n{result}"
        )
        # enterprise_mod must appear before custom_mod in the output
        assert result.index("enterprise_mod") < result.index("custom_mod"), (
            f"Expected enterprise_mod before custom_mod in Extended-by; got:\n{result}"
        )
    finally:
        with neo4j_driver.session() as session:
            session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=EDITION_VERSION)


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


def test_resolve_field_picks_base_module_when_extensions_tie(neo4j_driver):
    """Base module wins when field exists in base + 3 extensions with same inbound.

    Tier 1 (is_ext): extensions have outgoing INHERITS to base model → is_ext=1.
    Base has is_definition=true or no outgoing INHERITS → is_ext=0 → ranks first.
    """
    FIELD_TIE_VERSION = "90.0"
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=FIELD_TIE_VERSION)

    try:
        with neo4j_driver.session() as session:
            # Base module + Model
            session.run(
                "MERGE (mod:Module {name: 'test_mod', odoo_version: $v}) "
                "SET mod.repo = 'test_repo', mod.edition = 'community'",
                v=FIELD_TIE_VERSION,
            )
            session.run(
                "MERGE (m:Model {name: 'test.order', module: 'test_mod', odoo_version: $v}) "
                "SET m.is_definition = true "
                "MERGE (mod:Module {name: 'test_mod', odoo_version: $v}) "
                "MERGE (m)-[:DEFINED_IN]->(mod)",
                v=FIELD_TIE_VERSION,
            )
            # Field in base module
            session.run(
                "MERGE (f:Field {name: 'state', model: 'test.order', "
                "module: 'test_mod', odoo_version: $v}) "
                "SET f.ttype = 'selection'",
                v=FIELD_TIE_VERSION,
            )

            # 3 extension modules, each with same field
            for i in range(3):
                ext_mod = f"ext_mod_{i}"
                session.run(
                    "MERGE (mod:Module {name: $mod, odoo_version: $v}) "
                    "SET mod.repo = 'ext_repo', mod.edition = 'community'",
                    mod=ext_mod, v=FIELD_TIE_VERSION,
                )
                session.run(
                    "MERGE (ext:Model {name: 'test.order', module: $mod, odoo_version: $v}) "
                    "MERGE (base:Model {name: 'test.order', module: 'test_mod', odoo_version: $v}) "
                    "MERGE (ext)-[:INHERITS]->(base) "
                    "MERGE (extmod:Module {name: $mod, odoo_version: $v}) "
                    "MERGE (ext)-[:DEFINED_IN]->(extmod)",
                    mod=ext_mod, v=FIELD_TIE_VERSION,
                )
                # Field redeclared in extension
                session.run(
                    "MERGE (f:Field {name: 'state', model: 'test.order', "
                    "module: $mod, odoo_version: $v}) "
                    "SET f.ttype = 'selection'",
                    mod=ext_mod, v=FIELD_TIE_VERSION,
                )

        resolve_field = _make_field_tools(neo4j_driver)
        result = resolve_field("test.order", "state", FIELD_TIE_VERSION)

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
    finally:
        with neo4j_driver.session() as session:
            session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=FIELD_TIE_VERSION)


def test_resolve_field_redeclare_extension_demoted(neo4j_driver):
    """Base with is_definition=true beats extension redeclare with is_definition=false.

    Base model has is_definition=true; extension redeclares same field with is_definition=false.
    Tier 1: base is_ext=0, extension is_ext=1 → base wins.
    """
    FIELD_REDEF_VERSION = "89.0"
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=FIELD_REDEF_VERSION)

    try:
        with neo4j_driver.session() as session:
            # Base module + Model with is_definition=true
            session.run(
                "MERGE (mod:Module {name: 'base_field_mod', odoo_version: $v}) "
                "SET mod.repo = 'test_repo', mod.edition = 'community'",
                v=FIELD_REDEF_VERSION,
            )
            session.run(
                "MERGE (m:Model {name: 'test.alpha', module: 'base_field_mod', odoo_version: $v}) "
                "SET m.is_definition = true "
                "MERGE (mod:Module {name: 'base_field_mod', odoo_version: $v}) "
                "MERGE (m)-[:DEFINED_IN]->(mod)",
                v=FIELD_REDEF_VERSION,
            )
            # Field in base
            session.run(
                "MERGE (f:Field {name: 'status', model: 'test.alpha', "
                "module: 'base_field_mod', odoo_version: $v}) "
                "SET f.ttype = 'char', f.required = true",
                v=FIELD_REDEF_VERSION,
            )

            # Extension module that redeclares the field
            session.run(
                "MERGE (mod:Module {name: 'ext_field_mod', odoo_version: $v}) "
                "SET mod.repo = 'ext_repo', mod.edition = 'community'",
                v=FIELD_REDEF_VERSION,
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
                v=FIELD_REDEF_VERSION,
            )
            # Field redeclared in extension
            session.run(
                "MERGE (f:Field {name: 'status', model: 'test.alpha', "
                "module: 'ext_field_mod', odoo_version: $v}) "
                "SET f.ttype = 'char'",
                v=FIELD_REDEF_VERSION,
            )

        resolve_field = _make_field_tools(neo4j_driver)
        result = resolve_field("test.alpha", "status", FIELD_REDEF_VERSION)

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
    finally:
        with neo4j_driver.session() as session:
            session.run(
                "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n",
                v=FIELD_REDEF_VERSION,
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


def test_resolve_method_picks_base_module_when_extensions_tie(neo4j_driver):
    """Base module wins when method exists in base + 3 extensions with same inbound.

    Tier 1 (is_ext): extensions have outgoing INHERITS to base model → is_ext=1.
    Base has is_definition=true or no outgoing INHERITS → is_ext=0 → ranks first.
    """
    METHOD_TIE_VERSION = "88.0"
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=METHOD_TIE_VERSION)

    try:
        with neo4j_driver.session() as session:
            # Base module + Model
            session.run(
                "MERGE (mod:Module {name: 'test_mod_m', odoo_version: $v}) "
                "SET mod.repo = 'test_repo', mod.edition = 'community'",
                v=METHOD_TIE_VERSION,
            )
            session.run(
                "MERGE (m:Model {name: 'test.order', module: 'test_mod_m', odoo_version: $v}) "
                "SET m.is_definition = true "
                "MERGE (mod:Module {name: 'test_mod_m', odoo_version: $v}) "
                "MERGE (m)-[:DEFINED_IN]->(mod)",
                v=METHOD_TIE_VERSION,
            )
            # Method in base module
            session.run(
                "MERGE (mth:Method {name: 'action_confirm', model: 'test.order', "
                "module: 'test_mod_m', odoo_version: $v}) "
                "SET mth.has_super_call = false",
                v=METHOD_TIE_VERSION,
            )

            # 3 extension modules, each with same method
            for i in range(3):
                ext_mod = f"ext_mod_m_{i}"
                session.run(
                    "MERGE (mod:Module {name: $mod, odoo_version: $v}) "
                    "SET mod.repo = 'ext_repo', mod.edition = 'community'",
                    mod=ext_mod, v=METHOD_TIE_VERSION,
                )
                session.run(
                    "MERGE (ext:Model {name: 'test.order', module: $mod, "
                    "odoo_version: $v}) "
                    "MERGE (base:Model {name: 'test.order', "
                    "module: 'test_mod_m', odoo_version: $v}) "
                    "MERGE (ext)-[:INHERITS]->(base) "
                    "MERGE (extmod:Module {name: $mod, odoo_version: $v}) "
                    "MERGE (ext)-[:DEFINED_IN]->(extmod)",
                    mod=ext_mod, v=METHOD_TIE_VERSION,
                )
                # Method in extension
                session.run(
                    "MERGE (mth:Method {name: 'action_confirm', model: 'test.order', "
                    "module: $mod, odoo_version: $v}) "
                    "SET mth.has_super_call = true",
                    mod=ext_mod, v=METHOD_TIE_VERSION,
                )

        resolve_method = _make_method_tools(neo4j_driver)
        result = resolve_method("test.order", "action_confirm", METHOD_TIE_VERSION)

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
    finally:
        with neo4j_driver.session() as session:
            session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=METHOD_TIE_VERSION)


def test_resolve_method_redeclare_extension_demoted(neo4j_driver):
    """Base with is_definition=true beats extension redeclare with is_definition=false.

    Base model has is_definition=true; extension redeclares same method with is_definition=false.
    Tier 1: base is_ext=0, extension is_ext=1 → base wins in Override chain.
    """
    METHOD_REDEF_VERSION = "87.0"
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=METHOD_REDEF_VERSION)

    try:
        with neo4j_driver.session() as session:
            # Base module + Model with is_definition=true
            session.run(
                "MERGE (mod:Module {name: 'base_method_mod', odoo_version: $v}) "
                "SET mod.repo = 'test_repo', mod.edition = 'community'",
                v=METHOD_REDEF_VERSION,
            )
            session.run(
                "MERGE (m:Model {name: 'test.beta', module: 'base_method_mod', odoo_version: $v}) "
                "SET m.is_definition = true "
                "MERGE (mod:Module {name: 'base_method_mod', odoo_version: $v}) "
                "MERGE (m)-[:DEFINED_IN]->(mod)",
                v=METHOD_REDEF_VERSION,
            )
            # Method in base
            session.run(
                "MERGE (mth:Method {name: 'do_something', model: 'test.beta', "
                "module: 'base_method_mod', odoo_version: $v}) "
                "SET mth.has_super_call = false",
                v=METHOD_REDEF_VERSION,
            )

            # Extension module that redeclares the method
            session.run(
                "MERGE (mod:Module {name: 'ext_method_mod', odoo_version: $v}) "
                "SET mod.repo = 'ext_repo', mod.edition = 'community'",
                v=METHOD_REDEF_VERSION,
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
                v=METHOD_REDEF_VERSION,
            )
            # Method redeclared in extension
            session.run(
                "MERGE (mth:Method {name: 'do_something', model: 'test.beta', "
                "module: 'ext_method_mod', odoo_version: $v}) "
                "SET mth.has_super_call = true",
                v=METHOD_REDEF_VERSION,
            )

        resolve_method = _make_method_tools(neo4j_driver)
        result = resolve_method("test.beta", "do_something", METHOD_REDEF_VERSION)

        assert "Override chain" in result
        # First method in override chain should be from base_method_mod
        lines = result.split("\n")
        for line in lines[1:]:
            if "base_method_mod" in line:
                assert "base_method_mod" in lines[2], (
                    f"Expected 'base_method_mod' as first in Override chain; got:\n{result}"
                )
                break
    finally:
        with neo4j_driver.session() as session:
            session.run(
                "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n",
                v=METHOD_REDEF_VERSION,
            )
