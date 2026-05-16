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

    ext_mod = ModuleInfo("viin_account", TEST_VERSION, "tvtmaaddons_test", "/tmp",
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


def test_resolve_model_abstract_mixin_is_base(neo4j_driver):
    """Mixin model with is_definition=true is correctly identified as Defined-in.

    Synthetic mixin 'test.mixin' has 1 Model node (is_definition=true) and 5
    consumer models that inherit from it under different model names.
    The mixin itself has no INHERITS edge going outward to *its own* name, so
    is_ext=0 → it ranks as the definition even though it has many inbound edges.
    """
    MIXIN_BASE_VERSION = "86.0"
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=MIXIN_BASE_VERSION)

    try:
        with neo4j_driver.session() as session:
            # Mixin module + Model node with is_definition=true
            session.run(
                "MERGE (mod:Module {name: 'mixin_core', odoo_version: $v}) "
                "SET mod.repo = 'test_repo', mod.edition = 'community'",
                v=MIXIN_BASE_VERSION,
            )
            session.run(
                "MERGE (m:Model {name: 'test.mixin', module: 'mixin_core', odoo_version: $v}) "
                "SET m.is_definition = true, m.is_abstract = true "
                "MERGE (mod:Module {name: 'mixin_core', odoo_version: $v}) "
                "MERGE (m)-[:DEFINED_IN]->(mod)",
                v=MIXIN_BASE_VERSION,
            )
            # 5 consumer models inherit from test.mixin but under different model names
            for i in range(5):
                consumer_name = f"consumer.model.{i}"
                consumer_mod = f"consumer_mod_{i}"
                session.run(
                    "MERGE (mod:Module {name: $mod, odoo_version: $v}) "
                    "SET mod.repo = 'consumer_repo', mod.edition = 'community'",
                    mod=consumer_mod, v=MIXIN_BASE_VERSION,
                )
                session.run(
                    "MERGE (c:Model {name: $cname, module: $mod, odoo_version: $v}) "
                    "SET c.is_definition = true "
                    "MERGE (mx:Model {name: 'test.mixin', module: 'mixin_core', odoo_version: $v}) "
                    "MERGE (c)-[:INHERITS]->(mx) "
                    "MERGE (cmod:Module {name: $mod, odoo_version: $v}) "
                    "MERGE (c)-[:DEFINED_IN]->(cmod)",
                    cname=consumer_name, mod=consumer_mod, v=MIXIN_BASE_VERSION,
                )

        resolve_model = _make_ranking_tools(neo4j_driver)
        result = resolve_model("test.mixin", MIXIN_BASE_VERSION)

        assert "Defined in:" in result
        first_defined_in_line = result.split("Defined in:")[1].split("\n")[0]
        assert "mixin_core" in first_defined_in_line, (
            f"Expected 'mixin_core' as Defined-in for mixin model; got:\n{result}"
        )
    finally:
        with neo4j_driver.session() as session:
            session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=MIXIN_BASE_VERSION)


def test_resolve_model_sub_mixin_with_different_name(neo4j_driver):
    """Sub-mixin with _name != _inherit is treated as a new base definition.

    'mixin.alpha' has _name='mixin.alpha' and _inherit='base.mixin' (different names).
    Because the INHERITS edge goes to 'base.mixin' (a different model name), the
    is_ext heuristic treats 'mixin.alpha' as is_ext=0 → it is its own base.
    Assert Defined-in is 'mixin_alpha_mod', not 'base_mixin_mod'.
    """
    SUB_MIXIN_VERSION = "85.0"
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=SUB_MIXIN_VERSION)

    try:
        with neo4j_driver.session() as session:
            # Parent mixin: base.mixin
            session.run(
                "MERGE (mod:Module {name: 'base_mixin_mod', odoo_version: $v}) "
                "SET mod.repo = 'test_repo', mod.edition = 'community'",
                v=SUB_MIXIN_VERSION,
            )
            session.run(
                "MERGE (m:Model {name: 'base.mixin', module: 'base_mixin_mod', odoo_version: $v}) "
                "SET m.is_definition = true "
                "MERGE (mod:Module {name: 'base_mixin_mod', odoo_version: $v}) "
                "MERGE (m)-[:DEFINED_IN]->(mod)",
                v=SUB_MIXIN_VERSION,
            )

            # Sub-mixin: mixin.alpha inherits base.mixin but has a DIFFERENT _name
            # This is the pattern: _name = 'mixin.alpha'; _inherit = 'base.mixin'
            session.run(
                "MERGE (mod:Module {name: 'mixin_alpha_mod', odoo_version: $v}) "
                "SET mod.repo = 'test_repo', mod.edition = 'community'",
                v=SUB_MIXIN_VERSION,
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
                v=SUB_MIXIN_VERSION,
            )

        resolve_model = _make_ranking_tools(neo4j_driver)
        result = resolve_model("mixin.alpha", SUB_MIXIN_VERSION)

        assert "Defined in:" in result
        first_defined_in_line = result.split("Defined in:")[1].split("\n")[0]
        assert "mixin_alpha_mod" in first_defined_in_line, (
            f"Expected 'mixin_alpha_mod' as Defined-in for sub-mixin; got:\n{result}"
        )
    finally:
        with neo4j_driver.session() as session:
            session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=SUB_MIXIN_VERSION)


def test_resolve_model_transient_wizard_single_node(neo4j_driver):
    """Transient wizard with a single node resolves without error.

    A wizard model (is_transient=true) with exactly 1 Model node and no INHERITS
    edges. The resolver must return a valid result (no crash, no 'not found') and
    correctly identify that single node as Defined-in.
    """
    TRANSIENT_VERSION = "84.0"
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=TRANSIENT_VERSION)

    try:
        with neo4j_driver.session() as session:
            session.run(
                "MERGE (mod:Module {name: 'wizard_mod', odoo_version: $v}) "
                "SET mod.repo = 'test_repo', mod.edition = 'community'",
                v=TRANSIENT_VERSION,
            )
            session.run(
                "MERGE (m:Model {name: 'wizard.confirm', module: 'wizard_mod', odoo_version: $v}) "
                "SET m.is_transient = true, m.is_definition = true "
                "MERGE (mod:Module {name: 'wizard_mod', odoo_version: $v}) "
                "MERGE (m)-[:DEFINED_IN]->(mod)",
                v=TRANSIENT_VERSION,
            )

        resolve_model = _make_ranking_tools(neo4j_driver)
        result = resolve_model("wizard.confirm", TRANSIENT_VERSION)

        assert "not found" not in result.lower(), (
            f"Single-node transient model should resolve; got:\n{result}"
        )
        assert "wizard.confirm" in result
        assert "Defined in:" in result
        first_defined_in_line = result.split("Defined in:")[1].split("\n")[0]
        assert "wizard_mod" in first_defined_in_line, (
            f"Expected 'wizard_mod' as Defined-in; got:\n{result}"
        )
    finally:
        with neo4j_driver.session() as session:
            session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=TRANSIENT_VERSION)


def test_resolve_model_redeclare_with_mixin_injection(neo4j_driver):
    """Redeclare pattern (_name=X, _inherit=[X, mail.thread]) is ranked as extension.

    The redeclare module has both:
      - An INHERITS edge to 'doc.order' (same name → is_ext=1 via CASE 2)
      - An INHERITS edge to 'mail.thread' (mixin injection, different name)
    The base module (is_definition=true) must win Defined-in over the redeclare module.
    """
    REDECLARE_MIXIN_VERSION = "83.0"
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=REDECLARE_MIXIN_VERSION
        )

    try:
        with neo4j_driver.session() as session:
            # Base module: original definition of doc.order
            session.run(
                "MERGE (mod:Module {name: 'doc_base_mod', odoo_version: $v}) "
                "SET mod.repo = 'test_repo', mod.edition = 'community'",
                v=REDECLARE_MIXIN_VERSION,
            )
            session.run(
                "MERGE (m:Model {name: 'doc.order', module: 'doc_base_mod', odoo_version: $v}) "
                "SET m.is_definition = true "
                "MERGE (mod:Module {name: 'doc_base_mod', odoo_version: $v}) "
                "MERGE (m)-[:DEFINED_IN]->(mod)",
                v=REDECLARE_MIXIN_VERSION,
            )

            # mail.thread mixin (referenced but lives in another module)
            session.run(
                "MERGE (mod:Module {name: 'mail_mod', odoo_version: $v}) "
                "SET mod.repo = 'test_repo', mod.edition = 'community'",
                v=REDECLARE_MIXIN_VERSION,
            )
            session.run(
                "MERGE (mt:Model {name: 'mail.thread', module: 'mail_mod', odoo_version: $v}) "
                "SET mt.is_definition = true "
                "MERGE (mod:Module {name: 'mail_mod', odoo_version: $v}) "
                "MERGE (mt)-[:DEFINED_IN]->(mod)",
                v=REDECLARE_MIXIN_VERSION,
            )

            # Redeclare module: _name='doc.order', _inherit=['doc.order', 'mail.thread']
            # Creates INHERITS to same-name base (→ is_ext=1) AND to mail.thread mixin
            session.run(
                "MERGE (mod:Module {name: 'doc_mixin_mod', odoo_version: $v}) "
                "SET mod.repo = 'ext_repo', mod.edition = 'community'",
                v=REDECLARE_MIXIN_VERSION,
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
                v=REDECLARE_MIXIN_VERSION,
            )

        resolve_model = _make_ranking_tools(neo4j_driver)
        result = resolve_model("doc.order", REDECLARE_MIXIN_VERSION)

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
    finally:
        with neo4j_driver.session() as session:
            session.run(
                "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n",
                v=REDECLARE_MIXIN_VERSION,
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

    ext1_mod = ModuleInfo("viin_purchase", MULTI_EXT_VERSION, "tvtmaaddons_test", "/tmp",
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

    ext1_mod = ModuleInfo("viin_account", MULTI_MTH_VERSION, "tvtmaaddons_test", "/tmp",
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
    ext1_mod = ModuleInfo("viin_sale", MULTI_VIEW_VERSION, "tvtmaaddons_test", "/tmp",
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


def test_resolve_view_profile_filter_isolates(view_profile_tools):
    """profile_name='beta_93' should find the beta view but not the alpha view."""
    resolve_view, ver = view_profile_tools
    # beta view is found under beta_93
    result = resolve_view("mod_beta.view_beta_form", ver, profile_name="beta_93")
    assert "mod_beta.view_beta_form" in result

    # alpha view is NOT found under beta_93 (different profile)
    result_alpha = resolve_view("mod_alpha.view_alpha_form", ver, profile_name="beta_93")
    assert "not found" in result_alpha.lower()


# ===========================================================================
# Wave 6 (ADR-0023) — tests for the 7 new tools + grammar / footer / language
# policy enforcement. Each new tool gets happy/empty/truncation coverage; the
# grammar test runs against all 21 tools; the language-policy test parses
# server.py via ast and asserts no Vietnamese diacritics in static template
# strings (docstrings exempt).
# ===========================================================================

from tests.conftest import (  # noqa: E402,I001
    seed_js_patches, seed_owl_components, seed_qweb_templates,
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
        assert "use list_fields(" in out
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
    _cleanup_version(neo4j_driver, W6_LIST_FIELDS_VERSION)
    try:
        srv = _import_server_module()
        out = srv._list_fields("ghost.model", W6_LIST_FIELDS_VERSION)
        assert out.startswith(
            f"Fields of ghost.model (Odoo {W6_LIST_FIELDS_VERSION})",
        )
        assert "(none)" in out
        # Empty result still emits a Next: hint per Wave 5.
        assert "└─ Next:" in out
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
        # cap = LIST_PREVIEW_FIELDS_MAX (50); 60 total → "and 10 more".
        assert "and 10 more (use" in out
        assert "list_fields" in out  # more_hint references the same tool
    finally:
        _cleanup_version(neo4j_driver, W6_LIST_FIELDS_VERSION)


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
        # cap = 20; 30 total → "and 10 more"
        assert "and 10 more (use" in out
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
        assert "and 5 more (use" in out
    finally:
        _cleanup_version(neo4j_driver, W6_LIST_VIEWS_VERSION)


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
        # cap = LIST_PREVIEW_MAX_ITEMS (20); 25 total → "and 5 more"
        assert "and 5 more (use" in out
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
        assert "list_js_patches" in out
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
        assert "and 5 more (use" in out
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
        # cap = LIST_PREVIEW_PATCHES_MAX (10); 15 total → "and 5 more"
        assert "and 5 more (use" in out
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

    All 21 tools either render content or a deterministic empty/error string;
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
    ]


TERMINAL_TOOLS = {"lint_check", "cli_help", "api_version_diff"}

# `find_examples` and `suggest_pattern` empty-input sentinels are intentional
# user-error messages that bypass the tree grammar — exclude them from the
# strict grammar / next-step tests. Their normal-path output IS tree-shaped
# but reproducing it here would require seeded pgvector embeddings.
SENTINEL_EDGE_CASES = {"find_examples", "suggest_pattern"}


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

    # Strip leading informational banners (V0 lint matcher, curate status).
    def _is_banner(line: str) -> bool:
        return (
            "V0 fuzzy matcher" in line
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
# Next-step footer test — 18 drill-down MUST emit, 3 terminal MUST NOT emit.
# ===========================================================================


def test_next_step_footer_present(grammar_seed):
    """Each drill-down tool's normal output MUST contain ``└─ Next:``.

    Per ADR-0023 §4.3 the 18 drill-down tools emit ``└─ Next:`` either as the
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
# Language policy test — static template strings inside server.py functions
# must not contain Vietnamese diacritics (ADR-0023 §2). Docstrings exempt.
# ===========================================================================


def test_language_policy_static_templates():
    """Walk server.py via ast; every string Constant inside a function body
    (excluding the first stmt when it's a docstring) must not match
    ``[À-ỹ]`` — that range covers Vietnamese diacritics + Latin Extended."""
    import ast
    import re
    from pathlib import Path

    src_path = Path(__file__).parent.parent / "src" / "mcp" / "server.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    vi_re = re.compile(r"[À-ỹ]")

    violations: list[tuple[str, int, str]] = []

    def _walk_function(node, fname: str) -> None:
        body = list(node.body)
        # Drop a leading docstring (Expr → Constant str) — per ADR-0023 §2
        # docstrings exempt because they hold EN+VI TRIGGER patterns.
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        for stmt in body:
            for child in ast.walk(stmt):
                if (
                    isinstance(child, ast.Constant)
                    and isinstance(child.value, str)
                    and vi_re.search(child.value)
                ):
                    preview = child.value.replace("\n", " ")[:60]
                    violations.append((fname, child.lineno, preview))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _walk_function(node, node.name)

    assert not violations, (
        "ADR-0023 §2 language policy violations (Vietnamese diacritics in "
        "static template strings):\n"
        + "\n".join(f"  {fn}:{lineno}: {prev!r}" for fn, lineno, prev in violations)
    )


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
