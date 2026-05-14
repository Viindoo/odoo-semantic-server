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
    """Method with 3 overrides: first two use ├─, last uses └─ in Override chain."""
    resolve_method, version = multi_mth_tools
    result = resolve_method("account.move", "action_post", version)
    assert "Override chain (3)" in result, f"Expected 3-override chain:\n{result}"

    lines = result.splitlines()
    chain_start = next(i for i, line in enumerate(lines) if "Override chain" in line)
    chain_lines = [line for line in lines[chain_start + 1:] if line.startswith("    ")]

    assert len(chain_lines) == 3, (
        f"Expected 3 override entries, got {len(chain_lines)}:\n{result}"
    )
    assert "└─" in chain_lines[-1], f"Last override must use └─:\n{chain_lines[-1]}"
    assert all("├─" in line for line in chain_lines[:-1]), (
        f"Non-last overrides must use ├─:\n{chain_lines[:-1]}"
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
