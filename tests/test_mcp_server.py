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
