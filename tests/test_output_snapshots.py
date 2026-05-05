"""
MCP output schema guard — integration tests (require Neo4j).

Catches API drift: when _resolve_* functions change output format without
updating docs/thiet-ke-kien-truc.md §MCP Tools Interface.

Run: pytest tests/test_output_snapshots.py -m neo4j
When intentionally changing output format: update this test + architecture doc.
"""
import os

import pytest

from src.indexer.models import FieldInfo, MethodInfo, ModelInfo, ModuleInfo, ParseResult
from src.indexer.writer_neo4j import Neo4jWriter

pytestmark = pytest.mark.neo4j

_SNAP_VERSION = "96.0"  # dedicated version — avoids conflict with 99.0 / 98.0 / 97.0 fixtures


@pytest.fixture(scope="module")
def snapshot_db(neo4j_driver):
    """Seed minimal account.move data + yield; teardown after module."""
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=_SNAP_VERSION)

    mod = ModuleInfo("account", _SNAP_VERSION, "odoo_test", "/tmp", [], "")
    model = ModelInfo(
        name="account.move",
        module="account",
        odoo_version=_SNAP_VERSION,
        fields=[FieldInfo("name", "char", required=True)],
        methods=[MethodInfo("action_post", has_super_call=True)],
    )
    writer.write_results([ParseResult(module=mod, models=[model])])
    writer.close()

    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    import sys
    sys.modules.pop("src.mcp.server", None)

    yield

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=_SNAP_VERSION)


def test_resolve_model_output_contract(snapshot_db):
    """
    Contract per docs/thiet-ke-kien-truc.md §MCP Tools Interface:
        account.move (Odoo 96.0)
        ├─ Định nghĩa tại:   [odoo_test] account
        ├─ Tổng số field:  1
        └─ Tổng số method: 1

    If output format changes: update this test + architecture doc §MCP Tools.
    """
    from src.mcp.server import _resolve_model

    result = _resolve_model("account.move", _SNAP_VERSION)
    lines = result.splitlines()

    assert lines[0] == f"account.move (Odoo {_SNAP_VERSION})", (
        "Line 0 must be '<model> (Odoo <version>)' — see architecture doc §6"
    )
    assert any("Định nghĩa tại" in ln for ln in lines), (
        "Missing 'Định nghĩa tại' (definition source)"
    )
    assert any("Tổng số field" in ln for ln in lines), "Missing field count"
    assert any("Tổng số method" in ln for ln in lines), "Missing method count"
    assert any(ln.startswith("├─") or ln.startswith("└─") for ln in lines), (
        "Missing tree connectors (Ship Wow Product requirement)"
    )


def test_resolve_field_output_contract(snapshot_db):
    """
    Contract per docs/thiet-ke-kien-truc.md §MCP Tools Interface:
        account.move.name (Odoo 96.0)
        ├─ Loại:     char
        ├─ Computed: Không
        ...
        └─ Khai báo trong: ...

    If output format changes: update this test + architecture doc §MCP Tools.
    """
    from src.mcp.server import _resolve_field

    result = _resolve_field("account.move", "name", _SNAP_VERSION)
    lines = result.splitlines()

    assert lines[0] == f"account.move.name (Odoo {_SNAP_VERSION})"
    assert any("Loại" in ln for ln in lines), "Missing field type line"
    assert any("Computed" in ln for ln in lines), "Missing computed indicator"
    assert any("Khai báo trong" in ln for ln in lines), "Missing declaration source"
    assert any(ln.startswith("├─") or ln.startswith("└─") for ln in lines)


def test_resolve_method_output_contract(snapshot_db):
    """
    Contract per docs/thiet-ke-kien-truc.md §MCP Tools Interface:
        account.move.action_post() (Odoo 96.0)
        Override chain:
          [odoo_test] account — ✓ gọi super() — decorators: —

    If output format changes: update this test + architecture doc §MCP Tools.
    """
    from src.mcp.server import _resolve_method

    result = _resolve_method("account.move", "action_post", _SNAP_VERSION)
    lines = result.splitlines()

    assert lines[0] == f"account.move.action_post() (Odoo {_SNAP_VERSION})"
    assert any("Override chain" in ln for ln in lines), "Missing 'Override chain' header"
    assert any("super()" in ln for ln in lines), "Missing super() call indicator"
    assert any("decorators" in ln for ln in lines), "Missing decorators field"


def test_resolve_view_not_found_contract(snapshot_db):
    """
    resolve_view NOT_FOUND output contract — added in M2.
    Happy path contract is in test_mcp_server.py::test_resolve_view_found.

    If output format changes: update this test + architecture doc §MCP Tools.
    """
    from src.mcp.server import _resolve_view

    result = _resolve_view("nonexistent.view.xmlid", _SNAP_VERSION)
    assert "Không tìm thấy" in result, (
        "NOT_FOUND response must contain 'Không tìm thấy'"
    )
    assert "nonexistent.view.xmlid" in result, (
        "NOT_FOUND response must echo the queried xmlid"
    )
