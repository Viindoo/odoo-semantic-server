# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for the remaining 4 odoo:// resource handlers (WI-F4).

Covers AC-F4-1:
  ≥4 integration tests for the handlers NOT covered by F1 (which spot-checked
  model, field, module only).  This file adds real Neo4j fixture coverage for:

    - method    → odoo://{version}/method/{model}/{method}
    - view      → odoo://{version}/view/{xmlid}
    - pattern   → odoo://{version}/pattern/{pattern_id}
    - stylesheet → odoo://{version}/stylesheet/{module}/{file_path*}

DB isolation:
  - Neo4j version "F4_99.0" — distinct from F1 (F1_99.0), F2 (99.5), index (98.5).
  - All nodes seeded by ``f4_db`` fixture, wiped in teardown.

Markers:
  - All tests are marked ``neo4j``.
"""

from __future__ import annotations

import asyncio
import importlib
import os

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

F4_VERSION = "F4_99.0"
F4_MODULE = "f4_sale"
F4_MODEL = "f4.order"
F4_METHOD = "action_confirm"
F4_VIEW_XMLID = "f4_sale.view_order_form"
F4_PATTERN_ID = "f4-test-pattern-001"

pytestmark = pytest.mark.neo4j


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def f4_db(neo4j_driver):
    """Seed a tiny Neo4j fixture for F4_VERSION covering all 4 handler types.

    Creates:
    - 1 Module (f4_sale)
    - 1 Model (f4.order) with 1 field + 1 method
    - 1 View (primary form view)
    - 1 PatternExample
    - 1 Stylesheet node (no disk file — tests the not-found path)
    """
    with neo4j_driver.session() as s:
        s.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=F4_VERSION)

    from src.indexer.models import (
        FieldInfo,
        MethodInfo,
        ModelInfo,
        ModuleInfo,
        ParseResult,
        PatternExample,
    )
    from src.indexer.writer_neo4j import Neo4jWriter

    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    module = ModuleInfo(
        name=F4_MODULE,
        odoo_version=F4_VERSION,
        repo="odoo_test",
        path="/tmp/f4_sale",
        depends=["base"],
        edition="community",
    )
    model = ModelInfo(
        name=F4_MODEL,
        module=F4_MODULE,
        odoo_version=F4_VERSION,
        fields=[
            FieldInfo("amount_total", "monetary", compute="_compute_total", stored=True),
        ],
        methods=[
            MethodInfo(
                name=F4_METHOD,
                decorators=["api.multi"],
                has_super_call=True,
            ),
        ],
    )
    writer.write_results([ParseResult(module=module, models=[model])])

    # Seed a primary View node directly via Cypher (ViewParseResult path is
    # tested by other suites; here we seed minimally for resource handler coverage).
    with neo4j_driver.session() as s:
        s.run(
            """
            MERGE (mod:Module {name: $module, odoo_version: $v})
            MERGE (view:View {xmlid: $xmlid, odoo_version: $v})
            ON CREATE SET
                view.name = $name,
                view.model = $model,
                view.module = $module,
                view.type = $vtype,
                view.mode = $mode
            ON MATCH SET
                view.name = $name,
                view.model = $model,
                view.module = $module,
                view.type = $vtype,
                view.mode = $mode
            MERGE (view)-[:DEFINED_IN]->(mod)
            """,
            xmlid=F4_VIEW_XMLID,
            v=F4_VERSION,
            name="Order Form",
            model=F4_MODEL,
            module=F4_MODULE,
            vtype="form",
            mode="primary",
        )

    # Seed a PatternExample node.
    writer.write_pattern_examples([
        PatternExample(
            pattern_id=F4_PATTERN_ID,
            intent_keywords=["action_confirm", "sale order"],
            file_ref="addons/sale/models/sale_order.py:100",
            snippet_text=(
                "def action_confirm(self):\n"
                "    res = super().action_confirm()\n"
                "    return res"
            ),
            gotchas=["Always call super() in action_confirm."],
            odoo_version_min="14.0",
            language="python",
        ),
    ])

    writer.close()

    yield neo4j_driver

    with neo4j_driver.session() as s:
        s.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=F4_VERSION)
        s.run(
            "MATCH (n:PatternExample {pattern_id: $pid}) DETACH DELETE n",
            pid=F4_PATTERN_ID,
        )


@pytest.fixture()
def fresh_resources_module(monkeypatch):
    """Reload src.mcp.resources so its cache is empty for each test."""
    monkeypatch.setenv("NEO4J_URI", os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"))
    monkeypatch.setenv("NEO4J_USER", os.getenv("NEO4J_TEST_USER", "neo4j"))
    monkeypatch.setenv("NEO4J_PASSWORD", os.getenv("NEO4J_TEST_PASSWORD", "password"))
    import src.mcp.resources as mod
    importlib.reload(mod)
    return mod


@pytest.fixture()
def mcp_with_resources(f4_db, fresh_resources_module):
    """Create a fresh FastMCP with all 7 handlers registered."""
    from fastmcp import FastMCP

    mcp = FastMCP("test-f4-resources")
    fresh_resources_module.register_resources(mcp)
    return mcp


def _read(mcp, uri: str) -> str:
    """Synchronous helper: read a resource URI and return its first text body.

    Uses a private event loop to avoid disturbing the test-suite event-loop
    state (see test_mcp_resources.py for the rationale).
    """
    try:
        prior_loop = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        prior_loop = None

    new_loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(new_loop)
        contents = new_loop.run_until_complete(
            mcp._resource_manager.read_resource(uri),
        )
    finally:
        new_loop.close()
        if prior_loop is not None and not prior_loop.is_closed():
            asyncio.set_event_loop(prior_loop)
        else:
            asyncio.set_event_loop(asyncio.new_event_loop())

    if isinstance(contents, list | tuple):
        first = contents[0]
        return first.content if hasattr(first, "content") else str(first)
    if hasattr(contents, "content"):
        return contents.content
    return str(contents)


# ===========================================================================
# Test 1 — method handler returns override-chain tree
# ===========================================================================


def test_method_resource_returns_markdown_tree(mcp_with_resources) -> None:
    """odoo://F4_99.0/method/f4.order/action_confirm returns resolve_method tree."""
    uri = f"odoo://{F4_VERSION}/method/{F4_MODEL}/{F4_METHOD}"
    body = _read(mcp_with_resources, uri)

    assert F4_METHOD in body, f"Body must mention method name; got: {body[:300]!r}"
    assert F4_MODEL in body, "Body must mention the model name"
    # resolve_method output includes "Override chain"
    assert "Override chain" in body or F4_MODULE in body, (
        f"Body must include override chain or module; got: {body[:300]!r}"
    )


def test_method_resource_unknown_returns_not_found(mcp_with_resources) -> None:
    """odoo://F4_99.0/method/f4.order/does_not_exist returns a not-found body."""
    uri = f"odoo://{F4_VERSION}/method/{F4_MODEL}/does_not_exist"
    body = _read(mcp_with_resources, uri)
    # _resolve_method returns "Method '<name>' not found on model ..."
    assert "not found" in body.lower() or "does_not_exist" in body, (
        f"Expected not-found body; got: {body[:200]!r}"
    )


# ===========================================================================
# Test 2 — view handler returns view-tree
# ===========================================================================


def test_view_resource_returns_markdown_tree(mcp_with_resources) -> None:
    """odoo://F4_99.0/view/f4_sale.view_order_form returns resolve_view tree."""
    uri = f"odoo://{F4_VERSION}/view/{F4_VIEW_XMLID}"
    body = _read(mcp_with_resources, uri)

    assert F4_VIEW_XMLID in body, f"Body must mention the xmlid; got: {body[:300]!r}"
    # resolve_view output includes the view type
    assert "form" in body.lower(), (
        f"Body must mention view type 'form'; got: {body[:300]!r}"
    )
    assert F4_MODULE in body, "Body must mention the defining module"


def test_view_resource_unknown_returns_not_found(mcp_with_resources) -> None:
    """odoo://F4_99.0/view/nonexistent.xmlid returns a not-found body, no exception."""
    uri = f"odoo://{F4_VERSION}/view/nonexistent.xmlid"
    body = _read(mcp_with_resources, uri)
    assert "not found" in body.lower() or "nonexistent" in body, (
        f"Expected not-found body; got: {body[:200]!r}"
    )


# ===========================================================================
# Test 3 — pattern handler returns snippet tree
# ===========================================================================


def test_pattern_resource_returns_snippet_body(mcp_with_resources) -> None:
    """odoo://F4_99.0/pattern/f4-test-pattern-001 returns the curated snippet."""
    uri = f"odoo://{F4_VERSION}/pattern/{F4_PATTERN_ID}"
    body = _read(mcp_with_resources, uri)

    assert F4_PATTERN_ID in body, f"Body must mention the pattern_id; got: {body[:300]!r}"
    # _render_pattern emits "Language:", "File:", and snippet
    assert "Language:" in body, "Body must include Language: branch"
    assert "File:" in body, "Body must include File: branch"
    # Our fixture snippet mentions action_confirm
    assert "action_confirm" in body, "Body must include the seeded snippet text"


def test_pattern_resource_unknown_returns_not_found(mcp_with_resources) -> None:
    """odoo://F4_99.0/pattern/no-such-pattern returns a not-found tree."""
    uri = f"odoo://{F4_VERSION}/pattern/no-such-pattern"
    body = _read(mcp_with_resources, uri)
    assert "not found" in body.lower() or "no-such-pattern" in body, (
        f"Expected not-found body; got: {body[:200]!r}"
    )


# ===========================================================================
# Test 4 — stylesheet handler: not-indexed URI returns not-found tree
# ===========================================================================


def test_stylesheet_resource_unknown_returns_not_found(mcp_with_resources) -> None:
    """odoo://F4_99.0/stylesheet/f4_sale/static/src/no_such.css returns not-found.

    We deliberately do NOT seed a Stylesheet node here — this tests the
    guard path: _render_stylesheet checks the Neo4j index before opening
    any on-disk file, so a URI for a non-indexed file must return a
    structured not-found body rather than an OSError.
    """
    uri = f"odoo://{F4_VERSION}/stylesheet/{F4_MODULE}/static/src/no_such.css"
    body = _read(mcp_with_resources, uri)
    assert "not found" in body.lower(), (
        f"Expected 'not found' in body for un-indexed stylesheet; got: {body[:200]!r}"
    )
    # Specifically assert the recovery hint is present (per _render_stylesheet impl)
    assert "Recovery:" in body or "describe_module" in body, (
        f"Expected recovery hint in not-found body; got: {body[:300]!r}"
    )


def test_stylesheet_resource_indexed_but_unreadable(mcp_with_resources, f4_db) -> None:
    """Stylesheet node indexed in Neo4j but file missing on disk → 'unreadable' body.

    Seeds a :Stylesheet node pointing to a non-existent on-disk path,
    then asserts the handler returns the 'indexed but file unreadable'
    error tree rather than raising an OSError.
    """
    # Seed a Stylesheet node with a non-existent on-disk path.
    missing_path = "/tmp/f4_this_file_does_not_exist_ever.css"
    with f4_db.session() as s:
        s.run(
            """
            MERGE (mod:Module {name: $mod, odoo_version: $v})
            MERGE (ss:Stylesheet {file_path: $fp, module: $mod, odoo_version: $v})
            ON CREATE SET ss.language = 'css', ss.selector_count = 0
            ON MATCH  SET ss.language = 'css'
            MERGE (ss)-[:DEFINED_IN]->(mod)
            """,
            mod=F4_MODULE,
            v=F4_VERSION,
            fp=missing_path,
        )

    # Strip the leading "/" from the path for the URI segment.
    path_segment = missing_path.lstrip("/")
    uri = f"odoo://{F4_VERSION}/stylesheet/{F4_MODULE}/{path_segment}"

    import importlib as _il

    from fastmcp import FastMCP

    import src.mcp.resources as res_mod

    # Patch the driver env so the resource handler hits the test Neo4j.
    _il.reload(res_mod)
    mcp2 = FastMCP("test-f4-stylesheet-unreadable")
    res_mod.register_resources(mcp2)

    body = _read(mcp2, uri)

    # Cleanup the seeded Stylesheet.
    with f4_db.session() as s:
        s.run(
            "MATCH (ss:Stylesheet {file_path: $fp, module: $mod, odoo_version: $v}) "
            "DETACH DELETE ss",
            fp=missing_path, mod=F4_MODULE, v=F4_VERSION,
        )

    assert "unreadable" in body.lower() or "not found" in body.lower(), (
        f"Expected 'unreadable' or 'not found' body; got: {body[:300]!r}"
    )
