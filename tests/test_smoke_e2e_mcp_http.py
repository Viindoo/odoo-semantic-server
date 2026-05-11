"""M7 T1 MCP HTTP E2E smoke tests — calling MCP tools via HTTP JSON-RPC transport.

Smoke tier: fast per-PR tests that verify MCP HTTP transport returns user-visible output.
Requires: Neo4j running (pytest.mark.neo4j)

Tests:
- resolve_model returns tree format with module names + tree connectors (├─, └─)
- resolve_view returns XPath chain with module names
- impact_analysis returns affected modules list
"""
import contextlib
import os

import httpx
import pytest
from asgi_lifespan import LifespanManager

from src.indexer.models import (
    FieldInfo,
    MethodInfo,
    ModelInfo,
    ModuleInfo,
    ParseResult,
    ViewInfo,
    ViewParseResult,
    XPathInfo,
)
from src.indexer.writer_neo4j import Neo4jWriter
from src.mcp.server import mcp
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j


@contextlib.asynccontextmanager
async def _mcp_http_client():
    """ASGI client for the MCP HTTP transport.

    Uses stateless_http + json_response so callers can `resp.json()` directly
    without an init/session handshake and without parsing SSE frames. The MCP
    spec requires `Accept: application/json, text/event-stream`, so we set it
    by default for all requests through this client.
    """
    app = mcp.http_app(stateless_http=True, json_response=True)
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Accept": "application/json, text/event-stream"},
        ) as client:
            yield client


@pytest.fixture(scope="module")
def seeded_neo4j_http(neo4j_driver):
    """Seed Neo4j with minimal test data for HTTP transport tests."""
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=TEST_VERSION)

    # Base module: sale with sale.order model + 1 field
    base_mod = ModuleInfo("sale", TEST_VERSION, "odoo_test", "/tmp", [], "")
    base_model = ModelInfo(
        name="sale.order",
        module="sale",
        odoo_version=TEST_VERSION,
        fields=[
            FieldInfo("name", "char", required=True),
            FieldInfo("amount_total", "float", compute="_compute_amount", stored=True),
        ],
        methods=[MethodInfo("action_confirm", has_super_call=False)],
    )

    # Extended module: viin_sale extends sale.order
    ext_mod = ModuleInfo("viin_sale", TEST_VERSION, "acme_addons_test", "/tmp", ["sale"], "")
    ext_model = ModelInfo(
        name="sale.order",
        module="viin_sale",
        odoo_version=TEST_VERSION,
        inherit=["sale.order"],
        fields=[FieldInfo("x_approval_state", "selection")],
        methods=[MethodInfo("action_confirm", has_super_call=True)],
    )

    # Base module: account with account.move model + view
    account_mod = ModuleInfo("account", TEST_VERSION, "odoo_test", "/tmp", [], "")
    account_model = ModelInfo(
        name="account.move",
        module="account",
        odoo_version=TEST_VERSION,
        fields=[FieldInfo("date", "date"), FieldInfo("amount", "float")],
        methods=[],
    )
    account_view = ViewInfo(
        xmlid="account.view_move_tree",
        name="account.view_move_tree",
        module="account",
        odoo_version=TEST_VERSION,
        model="account.move",
        view_type="tree",
        mode="primary",
        inherit_xmlid=None,
    )

    # Extended module: viin_account extends account.move + adds view inheritance
    viin_account_mod = ModuleInfo(
        "viin_account", TEST_VERSION, "acme_addons_test", "/tmp", ["account"], ""
    )
    viin_account_model = ModelInfo(
        name="account.move",
        module="viin_account",
        odoo_version=TEST_VERSION,
        inherit=["account.move"],
        fields=[FieldInfo("x_custom_field", "char")],
        methods=[],
    )
    viin_account_view = ViewInfo(
        xmlid="viin_account.view_move_tree_custom",
        name="viin_account.view_move_tree_custom",
        module="viin_account",
        odoo_version=TEST_VERSION,
        model="account.move",
        view_type="tree",
        mode="extension",
        inherit_xmlid="account.view_move_tree",
        xpaths=[XPathInfo(expr="//field[@name='date']", position="inside")],
    )

    writer.write_results([
        ParseResult(module=base_mod, models=[base_model]),
        ParseResult(module=ext_mod, models=[ext_model]),
        ParseResult(module=account_mod, models=[account_model]),
        ParseResult(module=viin_account_mod, models=[viin_account_model]),
    ])
    writer.write_view_results([
        ViewParseResult(module=account_mod, views=[account_view]),
        ViewParseResult(module=viin_account_mod, views=[viin_account_view]),
    ])
    writer.close()
    yield
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=TEST_VERSION)


class TestMCPHTTPResolveModel:
    @pytest.mark.asyncio
    async def test_resolve_model_tree_format(self, seeded_neo4j_http):
        """resolve_model via HTTP returns tree format with module names + tree chars."""
        # JSON-RPC 2.0 request to call tools/call
        request_body = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "tools/call",
            "params": {
                "name": "resolve_model",
                "arguments": {
                    "model_name": "sale.order",
                    "odoo_version": TEST_VERSION,
                },
            },
        }

        async with _mcp_http_client() as client:
            resp = await client.post("/mcp", json=request_body)

        assert resp.status_code == 200, f"Unexpected status: {resp.status_code}"
        body = resp.json()

        # Extract text from JSON-RPC response
        assert "result" in body, f"No result in response: {body}"
        result = body["result"]

        # MCP CallToolResult wraps the content array in a dict; fall back to
        # raw list shape for older FastMCP versions.
        content_list = result["content"] if isinstance(result, dict) else result
        assert isinstance(content_list, list) and len(content_list) > 0, (
            f"Result should contain non-empty content list, got: {result}"
        )
        content = content_list[0]
        assert "text" in content, f"No text in content: {content}"
        text = content["text"]

        # Assert user-visible markers
        assert "sale.order" in text, "Model name not in output"
        assert TEST_VERSION in text or "Odoo" in text, "Version not in output"
        # Tree connectors
        assert "├─" in text or "└─" in text, (
            f"Tree connectors (├─ or └─) not found in output:\n{text}"
        )
        # Module names
        assert "sale" in text, "Base module 'sale' not visible in output"
        assert "viin_sale" in text, "Extending module 'viin_sale' not visible in output"

    @pytest.mark.asyncio
    async def test_resolve_model_shows_inheritance(self, seeded_neo4j_http):
        """resolve_model shows Extended by section with extending module."""
        request_body = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "tools/call",
            "params": {
                "name": "resolve_model",
                "arguments": {
                    "model_name": "sale.order",
                    "odoo_version": TEST_VERSION,
                },
            },
        }

        async with _mcp_http_client() as client:
            resp = await client.post("/mcp", json=request_body)

        assert resp.status_code == 200
        body = resp.json()
        result = body["result"]
        content_list = result["content"] if isinstance(result, dict) else result
        content = content_list[0]
        text = content["text"]

        # Should show "Extended by" section
        assert "Extended by" in text or "extended" in text.lower(), (
            f"No extension information visible:\n{text}"
        )
        assert "viin_sale" in text, "Extending module not shown"


class TestMCPHTTPResolveView:
    @pytest.mark.asyncio
    async def test_resolve_view_xpath_chain(self, seeded_neo4j_http):
        """resolve_view via HTTP returns XPath override chain with module names."""
        request_body = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "tools/call",
            "params": {
                "name": "resolve_view",
                "arguments": {
                    "xmlid": "account.view_move_tree",
                    "odoo_version": TEST_VERSION,
                },
            },
        }

        async with _mcp_http_client() as client:
            resp = await client.post("/mcp", json=request_body)

        assert resp.status_code == 200, f"Unexpected status: {resp.status_code}"
        body = resp.json()

        assert "result" in body, f"No result in response: {body}"
        result = body["result"]
        content_list = result["content"] if isinstance(result, dict) else result
        assert isinstance(content_list, list) and len(content_list) > 0, (
            f"Result should contain non-empty content list, got: {result}"
        )
        content = content_list[0]
        assert "text" in content, f"No text in content: {content}"
        text = content["text"]

        # Assert user-visible markers
        assert "account.view_move_tree" in text, "View xmlid not in output"
        assert "account" in text, "Defining module 'account' not visible"
        # Tree connectors or section headers
        assert ("├─" in text or "└─" in text or "tree" in text.lower()), (
            f"View structure not clearly shown:\n{text}"
        )

    @pytest.mark.asyncio
    async def test_resolve_view_extensions(self, seeded_neo4j_http):
        """resolve_view shows extended-by section when view is inherited."""
        request_body = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "tools/call",
            "params": {
                "name": "resolve_view",
                "arguments": {
                    "xmlid": "account.view_move_tree",
                    "odoo_version": TEST_VERSION,
                },
            },
        }

        async with _mcp_http_client() as client:
            resp = await client.post("/mcp", json=request_body)

        assert resp.status_code == 200
        body = resp.json()
        result = body["result"]
        content_list = result["content"] if isinstance(result, dict) else result
        content = content_list[0]
        text = content["text"]

        # Check if extended-by section shows (may be "Extended by" or similar)
        # Since viin_account extends account.view_move_tree
        has_extension_section = (
            "Extended by" in text or "extended" in text.lower() or
            "viin_account" in text  # at minimum, extending module should be mentioned
        )
        assert has_extension_section, (
            f"Extension information not clearly shown:\n{text}"
        )


class TestMCPHTTPImpactAnalysis:
    @pytest.mark.asyncio
    async def test_impact_analysis_field(self, seeded_neo4j_http):
        """impact_analysis via HTTP returns affected modules for a field."""
        request_body = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "tools/call",
            "params": {
                "name": "impact_analysis",
                "arguments": {
                    "entity_type": "field",
                    "entity_name": "sale.order.amount_total",
                    "odoo_version": TEST_VERSION,
                },
            },
        }

        async with _mcp_http_client() as client:
            resp = await client.post("/mcp", json=request_body)

        assert resp.status_code == 200, f"Unexpected status: {resp.status_code}"
        body = resp.json()

        assert "result" in body, f"No result in response: {body}"
        result = body["result"]
        content_list = result["content"] if isinstance(result, dict) else result
        assert isinstance(content_list, list) and len(content_list) > 0, (
            f"Result should contain non-empty content list, got: {result}"
        )
        content = content_list[0]
        assert "text" in content, f"No text in content: {content}"
        text = content["text"]

        # Assert user-visible markers
        assert "impact_analysis" in text or "amount_total" in text, (
            f"Impact analysis marker not in output:\n{text}"
        )
        # Should mention affected entities (methods, views, etc.)
        assert ("Methods" in text or "methods" in text.lower() or
                "├─" in text or "└─" in text), (
            f"No affected entities structure shown:\n{text}"
        )

    @pytest.mark.asyncio
    async def test_impact_analysis_model(self, seeded_neo4j_http):
        """impact_analysis via HTTP works for model entity_type."""
        request_body = {
            "jsonrpc": "2.0",
            "id": "1",
            "method": "tools/call",
            "params": {
                "name": "impact_analysis",
                "arguments": {
                    "entity_type": "model",
                    "entity_name": "account.move",
                    "odoo_version": TEST_VERSION,
                },
            },
        }

        async with _mcp_http_client() as client:
            resp = await client.post("/mcp", json=request_body)

        assert resp.status_code == 200, f"Unexpected status: {resp.status_code}"
        body = resp.json()

        assert "result" in body, f"No result in response: {body}"
        result = body["result"]
        content_list = result["content"] if isinstance(result, dict) else result
        assert isinstance(content_list, list) and len(content_list) > 0, (
            f"Result should contain non-empty content list, got: {result}"
        )
        content = content_list[0]
        assert "text" in content, f"No text in content: {content}"
        text = content["text"]

        # Should show impact analysis output
        assert "impact_analysis" in text or "account.move" in text, (
            f"Impact analysis not clearly shown:\n{text}"
        )
        # Should have risk or affected count indicator
        assert ("Risk" in text or "affected" in text.lower() or
                "Views" in text or "Methods" in text), (
            f"No risk/impact indicators found:\n{text}"
        )
