# SPDX-License-Identifier: AGPL-3.0-or-later
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
def snapshot_db(neo4j_driver, monkeypatch_module):
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

    monkeypatch_module.setenv("NEO4J_URI", os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"))
    monkeypatch_module.setenv("NEO4J_USER", os.getenv("NEO4J_TEST_USER", "neo4j"))
    monkeypatch_module.setenv("NEO4J_PASSWORD", os.getenv("NEO4J_TEST_PASSWORD", "password"))
    import sys
    sys.modules.pop("src.mcp.server", None)

    yield

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=_SNAP_VERSION)


def test_resolve_model_output_contract(snapshot_db):
    """
    Contract per docs/thiet-ke-kien-truc.md §MCP Tools Interface:
        account.move (Odoo 96.0)
        ├─ Defined in:     [odoo_test] account
        ├─ Fields:         1
        └─ Methods:        1

    If output format changes: update this test + architecture doc §MCP Tools.
    """
    from src.mcp.server import _resolve_model

    result = _resolve_model("account.move", _SNAP_VERSION)
    lines = result.splitlines()

    assert lines[0] == f"account.move (Odoo {_SNAP_VERSION})", (
        "Line 0 must be '<model> (Odoo <version>)' — see architecture doc §6"
    )
    assert any("Defined in" in ln for ln in lines), (
        "Missing 'Defined in' line"
    )
    assert any("Fields:" in ln for ln in lines), "Missing field count"
    assert any("Methods:" in ln for ln in lines), "Missing method count"
    assert any(ln.startswith("├─") or ln.startswith("└─") for ln in lines), (
        "Missing tree connectors (Ship Wow Product requirement)"
    )


def test_resolve_field_output_contract(snapshot_db):
    """
    Contract per docs/thiet-ke-kien-truc.md §MCP Tools Interface:
        account.move.name (Odoo 96.0)
        ├─ Type:     char
        ├─ Computed: No
        ...
        └─ Declared in: ...

    If output format changes: update this test + architecture doc §MCP Tools.
    """
    from src.mcp.server import _resolve_field

    result = _resolve_field("account.move", "name", _SNAP_VERSION)
    lines = result.splitlines()

    assert lines[0] == f"account.move.name (Odoo {_SNAP_VERSION})"
    assert any("Type:" in ln for ln in lines), "Missing field type line"
    assert any("Computed" in ln for ln in lines), "Missing computed indicator"
    assert any("Declared in" in ln for ln in lines), "Missing declaration source"
    assert any(ln.startswith("├─") or ln.startswith("└─") for ln in lines)


def test_resolve_method_output_contract(snapshot_db):
    """
    Contract per docs/thiet-ke-kien-truc.md §MCP Tools Interface:
        account.move.action_post() (Odoo 96.0)
        Override chain:
          [odoo_test] account — ✓ calls super() — decorators: —

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
    assert "not found" in result, (
        "NOT_FOUND response must contain 'not found'"
    )
    assert "nonexistent.view.xmlid" in result, (
        "NOT_FOUND response must echo the queried xmlid"
    )


class TestFindExamplesOutputSchema:
    """Lock find_examples output contract — format change breaks this test intentionally.

    Requires both Neo4j + PostgreSQL + pgvector extension.
    Skips gracefully when pgvector is not installed locally.
    """

    pytestmark = [pytest.mark.neo4j, pytest.mark.postgres]

    @pytest.fixture(autouse=True)
    def _seed(self, request, neo4j_driver, pg_conn):
        from src.db.migrate import _vector_extension_available
        # pg_conn fixture self-skips if Postgres is unreachable.
        pg_conn_fixture = pg_conn
        if not _vector_extension_available(pg_conn_fixture):
            pytest.skip("pgvector extension not installed")

        from pgvector.psycopg2 import register_vector

        from src.db.migrate import run_migrations
        from src.indexer.embedder import FakeEmbedder
        from src.indexer.writer_pgvector import EmbeddingChunk, write_module_embeddings

        run_migrations(pg_conn_fixture)
        register_vector(pg_conn_fixture)

        with pg_conn_fixture.cursor() as cur:
            cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (_SNAP_VERSION,))

        with neo4j_driver.session() as s:
            s.run("MERGE (:Module {name:'snap_mod', odoo_version:$v})", v=_SNAP_VERSION)

        embedder = FakeEmbedder(dim=1024)
        chunks = [
            EmbeddingChunk(
                "method", "snap_mod", _SNAP_VERSION, "snap_mod.sale.order.action_confirm",
                "sale.order", "snap_mod/models/sale.py", 0,
                f"[snap_mod] sale.order.action_confirm ({_SNAP_VERSION})\n"
                "def action_confirm(self): ...",
            ),
            EmbeddingChunk(
                "view", "snap_mod", _SNAP_VERSION, "snap_mod.sale_order_form",
                "sale.order", "snap_mod/views/sale.xml", 0,
                "[snap_mod] snap_mod.sale_order_form (form)\n<form/>",
            ),
            EmbeddingChunk(
                "method", "snap_mod", _SNAP_VERSION, "snap_mod.sale.order.action_confirm",
                "sale.order", "snap_mod/models/sale.py", 1,
                "[snap_mod] ...continued...",
            ),
        ]
        write_module_embeddings("snap_mod", _SNAP_VERSION, chunks, embedder)

        self._pg = pg_conn_fixture
        self._neo4j = neo4j_driver

        yield

        with pg_conn_fixture.cursor() as cur:
            cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (_SNAP_VERSION,))

    def test_find_examples_output_header_contract(self):
        """
        Contract: first non-empty line = 'find_examples: "<query>" (<version>)'
        Second line = 'Found N results'

        If output format changes: update this test + architecture doc §MCP Tools.
        """
        from src.indexer.embedder import FakeEmbedder
        from src.mcp.server import _find_examples

        result = _find_examples(
            "confirm order", odoo_version=_SNAP_VERSION,
            _driver=self._neo4j, _pg_conn=self._pg, _embedder=FakeEmbedder(dim=1024),
        )
        lines = result.splitlines()
        assert lines[0] == f'find_examples: "confirm order" ({_SNAP_VERSION})'
        assert lines[1].startswith("Found ")

    def test_find_examples_result_block_contract(self):
        """
        Each result block must contain:
        - '#N · score X.XX · <type> · [<module>] <entity>'
        - 'File: <path>'
        - '┌' box border
        - '│' content lines
        - '└' box border
        """
        from src.indexer.embedder import FakeEmbedder
        from src.mcp.server import _find_examples

        result = _find_examples(
            "confirm order", odoo_version=_SNAP_VERSION,
            _driver=self._neo4j, _pg_conn=self._pg, _embedder=FakeEmbedder(dim=1024),
        )
        assert "· score" in result
        assert "File:" in result
        assert "┌" in result
        assert "│" in result
        assert "└" in result

    def test_find_examples_view_shows_model_name(self):
        """View chunks must show model_name in the entity label."""
        from src.indexer.embedder import FakeEmbedder
        from src.mcp.server import _find_examples

        result = _find_examples(
            "sale form view", odoo_version=_SNAP_VERSION,
            _driver=self._neo4j, _pg_conn=self._pg, _embedder=FakeEmbedder(dim=1024),
            chunk_types=["view"],
        )
        assert "(model: sale.order)" in result, (
            "View chunks must include model_name — see server.py _find_examples output format"
        )

    def test_find_examples_sliding_chunk_shows_chunk_index(self):
        """Sliding-window chunks (chunk_idx > 0) must show 'chunk N' in the type label."""
        from src.indexer.embedder import FakeEmbedder
        from src.mcp.server import _find_examples

        result = _find_examples(
            "confirm action", odoo_version=_SNAP_VERSION, limit=10,
            _driver=self._neo4j, _pg_conn=self._pg, _embedder=FakeEmbedder(dim=1024),
            chunk_types=["method"],
        )
        assert "chunk 2" in result, (
            "Sliding-window chunks (chunk_idx=1) must be labeled 'method chunk 2'"
        )


def test_impact_analysis_output_has_required_sections(snapshot_db, neo4j_driver, monkeypatch):
    """
    Contract per docs/thiet-ke-kien-truc.md §MCP Tools Interface:
        impact_analysis(field, snapshot.model.snap_field, 96.0)
        ├─ Risk: LOW (0 affected entities)
        ├─ Views (0):
        ├─ Methods with super (N):
        ├─ JS patches: none
        └─ Dependent modules: none

    If output format changes: update this test + architecture doc §MCP Tools.
    """
    from src.indexer.models import FieldInfo, MethodInfo, ModelInfo, ModuleInfo, ParseResult
    from src.indexer.writer_neo4j import Neo4jWriter

    # Setup: create Module + Model + Field + 2 Views + 1 Method with super
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    # Create test data version
    test_version = "97.0"
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=test_version)

    mod = ModuleInfo("snapshot", test_version, "odoo_test", "/tmp", [], "")
    model = ModelInfo(
        name="snapshot.model",
        module="snapshot",
        odoo_version=test_version,
        fields=[FieldInfo("snap_field", "char", required=True)],
        methods=[MethodInfo("do_action", has_super_call=True)],
    )
    writer.write_results([ParseResult(module=mod, models=[model])])

    # Add Views and JS patches via raw Cypher — Neo4jWriter doesn't expose these yet
    with neo4j_driver.session() as session:
        session.run("""
            MATCH (m:Model {name: 'snapshot.model', odoo_version: $v})
            CREATE (view1:View {xmlid: 'snapshot.view1', module: 'snapshot',
                                type: 'form', odoo_version: $v})
            CREATE (view2:View {xmlid: 'snapshot.view2', module: 'snapshot',
                                type: 'tree', odoo_version: $v})
            CREATE (view1)-[:TARGETS_MODEL]->(m)
            CREATE (view2)-[:TARGETS_MODEL]->(m)
            RETURN 'Views created'
        """, v=test_version)

    writer.close()

    # Patch Neo4j env so _impact_analysis picks up test data — use monkeypatch for isolation
    monkeypatch.setenv("NEO4J_URI", os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"))
    monkeypatch.setenv("NEO4J_USER", os.getenv("NEO4J_TEST_USER", "neo4j"))
    monkeypatch.setenv("NEO4J_PASSWORD", os.getenv("NEO4J_TEST_PASSWORD", "password"))
    import sys
    sys.modules.pop("src.mcp.server", None)
    from src.mcp.server import _impact_analysis as impact_analysis_fresh

    result = impact_analysis_fresh("field", "snapshot.model.snap_field", test_version)
    lines = result.splitlines()

    # Assert header line
    assert lines[0] == f"impact_analysis(field, snapshot.model.snap_field, {test_version})", (
        "Line 0 must be 'impact_analysis(<type>, <entity>, <version>)'"
    )
    # Assert Risk line present and has threshold
    assert any("Risk:" in ln for ln in lines), "Missing 'Risk:' line"
    assert any("HIGH" in ln or "MEDIUM" in ln or "LOW" in ln for ln in lines), (
        "Risk level must be HIGH/MEDIUM/LOW"
    )
    # Assert Views section
    assert any("Views" in ln for ln in lines), "Missing Views section header"
    # Assert Methods section (for field, should say "Methods with super")
    assert any("Methods with super" in ln or "Methods" in ln for ln in lines), (
        "Missing Methods section header"
    )
    # Assert JS patches section
    assert any("JS patches" in ln for ln in lines), "Missing JS patches section header"
    # Assert Dependent modules section
    assert any("Dependent modules" in ln for ln in lines), (
        "Missing Dependent modules section header"
    )
    # Assert tree connectors present
    assert any(ln.startswith("├─") or ln.startswith("└─") for ln in lines), (
        "Missing tree connectors (Ship Wow Product requirement)"
    )

    # Cleanup
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=test_version)


def test_impact_analysis_output_empty_sections_render_gracefully(
    snapshot_db, neo4j_driver, monkeypatch
):
    """
    impact_analysis with no affected views/methods/js should render without errors.
    Empty sections show ': none' gracefully — no 'None' leak into output.

    If output format changes: update this test.
    """
    from src.indexer.models import FieldInfo, ModelInfo, ModuleInfo, ParseResult
    from src.indexer.writer_neo4j import Neo4jWriter

    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    test_version = "98.0"
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=test_version)

    # Minimal data: just module + model + field, no views/methods/js
    mod = ModuleInfo("minimal", test_version, "odoo_test", "/tmp", [], "")
    model = ModelInfo(
        name="minimal.model",
        module="minimal",
        odoo_version=test_version,
        fields=[FieldInfo("x", "integer")],
        methods=[],
    )
    writer.write_results([ParseResult(module=mod, models=[model])])
    writer.close()

    monkeypatch.setenv("NEO4J_URI", os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"))
    monkeypatch.setenv("NEO4J_USER", os.getenv("NEO4J_TEST_USER", "neo4j"))
    monkeypatch.setenv("NEO4J_PASSWORD", os.getenv("NEO4J_TEST_PASSWORD", "password"))
    import sys
    sys.modules.pop("src.mcp.server", None)
    from src.mcp.server import _impact_analysis as impact_analysis_fresh

    result = impact_analysis_fresh("field", "minimal.model.x", test_version)

    # Assert has risk line and should be LOW (0 affected)
    assert "Risk:" in result and "LOW" in result, (
        "Empty impact should render Risk: LOW"
    )
    # Assert no 'None' string leak
    assert "None" not in result, "Output must not contain None literal"
    # Assert graceful 'none' (lowercase) for empty sections
    assert "none" in result, "Empty sections should show 'none'"

    # Cleanup
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=test_version)


def test_impact_analysis_invalid_entity_type_message_shape(snapshot_db):
    """
    Invalid entity_type must return error message with exact shape:
        Invalid entity_type '...' Use: field, method, model.

    If output format changes: update this test.
    """
    from src.mcp.server import _impact_analysis

    result = _impact_analysis("garbage", "x.y", "96.0")

    assert "Invalid entity_type" in result, (
        "Invalid entity_type must mention 'Invalid entity_type'"
    )
    assert "garbage" in result, "Error must echo the invalid type provided"
    # Assert all 3 valid types are listed
    for valid in ["field", "method", "model"]:
        assert valid in result, f"Error message must list '{valid}' as valid option"


# --- M4.5 spec layer snapshot contracts ----------------------------------

_SPEC_SNAP_VERSION = "94.0"


@pytest.fixture(scope="module")
def spec_snapshot_db(neo4j_driver, monkeypatch_module):
    """Seed minimal CoreSymbol/LintRule/CLI* data for spec-tool snapshots."""
    from src.indexer.models import (
        CLICommandInfo,
        CLIFlagInfo,
        CoreSymbolInfo,
        LintRuleInfo,
    )

    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n",
            v=_SPEC_SNAP_VERSION,
        )

    writer.write_core_symbols([
        CoreSymbolInfo(
            qualified_name="odoo.models.BaseModel.name_get",
            kind="orm_method", odoo_version=_SPEC_SNAP_VERSION,
            signature="name_get(self)",
            status="deprecated",
            replacement_qname="odoo.models.BaseModel.display_name",
        ),
    ])
    writer.write_lint_rules([
        LintRuleInfo(
            rule_id="E8502", odoo_version=_SPEC_SNAP_VERSION,
            kind="pylint-odoo",
            message="Bad usage of _, _lt function",
            severity="error",
        ),
    ])
    writer.write_cli_commands([
        CLICommandInfo("server", _SPEC_SNAP_VERSION, description="Run server"),
    ])
    writer.write_cli_flags([
        CLIFlagInfo(
            "--longpolling-port", "server", _SPEC_SNAP_VERSION,
            type="int", status="deprecated",
            replacement_flag_name="--gevent-port",
            help="Deprecated alias",
        ),
        CLIFlagInfo(
            "--gevent-port", "server", _SPEC_SNAP_VERSION,
            type="int", default="8072",
        ),
    ])
    writer.write_cli_flag_replacements(
        [("--longpolling-port", "--gevent-port")],
        command_name="server",
        from_version=_SPEC_SNAP_VERSION,
        to_version=_SPEC_SNAP_VERSION,
    )
    writer.close()

    monkeypatch_module.setenv(
        "NEO4J_URI", os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
    )
    monkeypatch_module.setenv(
        "NEO4J_USER", os.getenv("NEO4J_TEST_USER", "neo4j"),
    )
    monkeypatch_module.setenv(
        "NEO4J_PASSWORD", os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    import sys
    sys.modules.pop("src.mcp.server", None)

    yield _SPEC_SNAP_VERSION

    with neo4j_driver.session() as session:
        session.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n",
            v=_SPEC_SNAP_VERSION,
        )


def _assert_tree_format(output: str, label: str) -> None:
    """Common contract: header + at least one tree connector + no None leak."""
    assert output.startswith(label) or label in output.splitlines()[0], (
        f"Header must start with {label!r}"
    )
    assert any(ln.startswith(("├─", "└─")) for ln in output.splitlines()), (
        f"Missing tree connector (├─ or └─) in:\n{output}"
    )
    assert "None" not in output, f"Output must not contain None literal:\n{output}"


def test_lookup_core_api_output_contract(spec_snapshot_db):
    from src.mcp.server import _lookup_core_api
    out = _lookup_core_api("name_get", spec_snapshot_db)
    _assert_tree_format(out, "odoo.models.BaseModel.name_get")
    assert "Status:" in out
    assert "deprecated" in out.lower()


def test_api_version_diff_output_contract(spec_snapshot_db):
    from src.mcp.server import _api_version_diff
    out = _api_version_diff("name_get", spec_snapshot_db, spec_snapshot_db)
    # Same-version short-circuit message has the tool name in header.
    assert out.startswith("api_version_diff")
    # Strengthen: also assert no nested quote characters (guard against !r repr bug)
    # Valid output has single quotes around method/version only, not doubled quotes like '"method"'
    assert '="' not in out, "NULL_HINT should not have nested quote characters from !r formatting"
    assert '\'"' not in out, "Output should not contain mixed quote nesting"


def test_api_version_diff_cross_version_contract(neo4j_driver, monkeypatch):
    """
    Cross-version diff contract: when same method exists in 2 versions with DIFFERENT
    signatures, output shows both signatures AND contains NO nested quote characters.

    Seeds Neo4j with same method (e.g. 'name_get') in 99.0 (with signature A) and
    98.0 (with signature B), then calls _api_version_diff across versions.
    Asserts:
    1. Both signatures appear in output (clearly delimited)
    2. No nested quote characters from !r formatting (e.g., '="...' or '\'"')
    3. NULL_HINT (not stored...) does not have nested quotes if either version missing
    """
    from src.indexer.models import CoreSymbolInfo
    from src.indexer.writer_neo4j import Neo4jWriter

    _CROSS_V1 = "99.0"  # from_version with one signature
    _CROSS_V2 = "98.0"  # to_version with a different signature

    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=_CROSS_V1)
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=_CROSS_V2)

    # Seed same method with DIFFERENT signatures in each version
    writer.write_core_symbols([
        CoreSymbolInfo(
            qualified_name="odoo.models.BaseModel.name_get",
            kind="orm_method",
            odoo_version=_CROSS_V1,
            signature="name_get(self, args=None)",  # signature in v99.0
            status="deprecated",
            replacement_qname="odoo.models.BaseModel.display_name",
        ),
        CoreSymbolInfo(
            qualified_name="odoo.models.BaseModel.name_get",
            kind="orm_method",
            odoo_version=_CROSS_V2,
            signature="name_get(self)",  # DIFFERENT signature in v98.0
            status="stable",
            replacement_qname=None,
        ),
    ])
    writer.close()

    monkeypatch.setenv("NEO4J_URI", os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"))
    monkeypatch.setenv("NEO4J_USER", os.getenv("NEO4J_TEST_USER", "neo4j"))
    monkeypatch.setenv("NEO4J_PASSWORD", os.getenv("NEO4J_TEST_PASSWORD", "password"))
    import sys
    sys.modules.pop("src.mcp.server", None)
    from src.mcp.server import _api_version_diff

    result = _api_version_diff("name_get", _CROSS_V1, _CROSS_V2)

    # Header contains version diff indicator
    assert "api_version_diff" in result or "Method version diff" in result, (
        "Output must have api_version_diff or Method version diff header"
    )

    # Both signatures must appear (business intent: show what changed)
    assert "name_get(self, args=None)" in result, (
        "from_version signature must be visible"
    )
    assert "name_get(self)" in result, (
        "to_version signature must be visible"
    )

    # No nested quote characters (key fix for M7 C3)
    # When from_sig_str = "name_get(self, args=None)" (plain string, no !r),
    # output should be like: "99.0=name_get(self, args=None) → 98.0=name_get(self)"
    # NOT like: '99.0="name_get(self, args=None)"' (which is what !r would produce)
    assert '="' not in result, (
        "NULL_HINT / signature should not have nested quote from !r formatting"
    )
    assert '\'"' not in result, (
        "Output should not contain mixed quote nesting from repr()"
    )

    # Cleanup
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=_CROSS_V1)
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=_CROSS_V2)


def test_find_deprecated_usage_output_contract(spec_snapshot_db):
    from src.mcp.server import _find_deprecated_usage
    out = _find_deprecated_usage(spec_snapshot_db)
    assert out.startswith("find_deprecated_usage")
    assert "None" not in out
    has_connector = any(
        ln.startswith(("├─", "└─")) for ln in out.splitlines()
    )
    assert has_connector or "no deprecated usage" in out.lower()


def test_lint_check_output_contract(spec_snapshot_db):
    from src.mcp.server import _lint_check
    out = _lint_check("x = _('hello')", spec_snapshot_db, language="python")
    # Header `lint_check(...)` always present (banner may prepend per WI-F6).
    assert "lint_check(" in out
    assert "None" not in out


def test_cli_help_output_contract(spec_snapshot_db):
    from src.mcp.server import _cli_help
    out = _cli_help("server", "--longpolling-port", spec_snapshot_db)
    _assert_tree_format(out, "cli_help")
    assert "Status:" in out
    assert "--gevent-port" in out  # replacement surfaced


# --- M4.6 pattern layer snapshot contracts -------------------------------

_PAT_SNAP_VERSION = "93.0"


@pytest.fixture(scope="module")
def pattern_snapshot_db(neo4j_driver, monkeypatch_module):
    """Seed minimal Module + Method + PatternExample for M4.6 tool snapshots."""
    from src.indexer.models import (
        MethodInfo,
        ModelInfo,
        ModuleInfo,
        ParseResult,
        PatternExample,
    )

    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n",
            v=_PAT_SNAP_VERSION,
        )

    # Module: viindoo edition
    sale_mod = ModuleInfo(
        name="sale", odoo_version=_PAT_SNAP_VERSION, repo="odoo",
        path="/odoo/addons/sale", depends=[], version_raw="",
        edition="community",
    )
    viin_sale_mod = ModuleInfo(
        name="viin_sale", odoo_version=_PAT_SNAP_VERSION, repo="acme_addons",
        path="/acme_addons/viin_sale", depends=[], version_raw="",
        edition="viindoo",
    )
    sale_model = ModelInfo(
        name="sale.order", module="sale", odoo_version=_PAT_SNAP_VERSION,
        methods=[
            MethodInfo(
                name="action_confirm", has_super_call=False,
                convention_kind="action", super_safety="always",
                return_required=True,
            ),
        ],
    )
    viin_model = ModelInfo(
        name="sale.order", module="viin_sale", odoo_version=_PAT_SNAP_VERSION,
        methods=[
            MethodInfo(
                name="action_confirm", has_super_call=True,
                convention_kind="action", super_safety="always",
                return_required=True,
            ),
        ],
    )
    writer.write_results([
        ParseResult(module=sale_mod, models=[sale_model]),
        ParseResult(module=viin_sale_mod, models=[viin_model]),
    ])
    # PatternExample for find_override_point + suggest_pattern (Neo4j-only fetch)
    writer.write_pattern_examples([
        PatternExample(
            pattern_id="snap-action-return-super",
            intent_keywords=["action", "super", "return"],
            file_ref="addons/sale/models/sale_order.py:1",
            snippet_text="def action_confirm(self):\n    return super().action_confirm()",
            gotchas=["Always return super() result"],
            odoo_version_min=_PAT_SNAP_VERSION,
            language="python",
            core_symbol_names=[],
        ),
    ])
    writer.close()

    monkeypatch_module.setenv(
        "NEO4J_URI", os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
    )
    monkeypatch_module.setenv(
        "NEO4J_USER", os.getenv("NEO4J_TEST_USER", "neo4j"),
    )
    monkeypatch_module.setenv(
        "NEO4J_PASSWORD", os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    import sys
    sys.modules.pop("src.mcp.server", None)

    yield _PAT_SNAP_VERSION

    with neo4j_driver.session() as session:
        session.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n",
            v=_PAT_SNAP_VERSION,
        )


def test_check_module_exists_output_contract(pattern_snapshot_db):
    """check_module_exists header + Indexed/EE-confusion sections."""
    from src.mcp.server import _check_module_exists
    out = _check_module_exists("knowledge", pattern_snapshot_db)
    assert out.startswith("check_module_exists(")
    assert "Indexed:" in out
    assert "Is EE confusion:" in out
    assert "WARNING" in out
    assert "None" not in out
    assert any(ln.startswith(("├─", "└─")) for ln in out.splitlines())


def test_find_override_point_output_contract(pattern_snapshot_db):
    """find_override_point header + Convention/Super safety/Return required/Anti-patterns.

    Note: anti-pattern text legitimately contains the word 'None' (e.g.
    'caller gets None, breaks chain') so we only guard against literal
    Neo4j null leakage like '[None]' / 'repo: None' / parenthesised standalone.
    """
    from src.mcp.server import _find_override_point
    out = _find_override_point(
        "sale.order", "action_confirm", pattern_snapshot_db,
    )
    assert out.startswith("find_override_point(")
    for sec in ["Convention:", "Super safety:", "Return required:", "Anti-patterns"]:
        assert sec in out, f"Missing section {sec!r}"
    # Null-leak guards: standalone unwrapping shapes from Neo4j.
    assert "[None]" not in out
    assert "(None)" not in out
    assert any(ln.startswith(("├─", "└─")) for ln in out.splitlines())


def test_find_override_point_diff_output_contract(pattern_snapshot_db, neo4j_driver):
    """find_override_point cross-version diff mode: header + all section headers + tree connectors.

    Seeds a second version (92.0) alongside the existing 93.0 data, then calls
    diff mode. Guards against output-format drift in _diff_method_across_versions.
    """
    import os as _os

    from src.indexer.models import MethodInfo, ModelInfo, ModuleInfo, ParseResult
    from src.indexer.writer_neo4j import Neo4jWriter

    _DIFF_VERSION = "92.0"  # to_version (older)
    from_version = pattern_snapshot_db  # "93.0"

    writer = Neo4jWriter(
        uri=_os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=_os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=_os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()
    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=_DIFF_VERSION)

    mod = ModuleInfo(
        name="sale", odoo_version=_DIFF_VERSION, repo="odoo",
        path="/odoo/addons/sale", depends=[], version_raw="",
    )
    model = ModelInfo(
        name="sale.order", module="sale", odoo_version=_DIFF_VERSION,
        methods=[
            MethodInfo(
                name="action_confirm", has_super_call=True,
                decorators=["api.multi"],
                convention_kind="action", super_safety="always",
                return_required=True,
            ),
        ],
    )
    writer.write_results([ParseResult(module=mod, models=[model])])
    writer.close()

    from src.mcp.server import _find_override_point
    out = _find_override_point(
        "sale.order", "action_confirm",
        odoo_version=from_version,
        to_version=_DIFF_VERSION,
        _driver=neo4j_driver,
    )

    # Header
    assert "Method version diff (" in out
    assert from_version in out
    assert "→" in out
    assert _DIFF_VERSION in out

    # All section headers present
    for section in ["Status:", "Decorator changes:", "Convention:", "Signature:", "Super safety:"]:
        assert section in out, f"Missing section {section!r} in diff output"

    # Tree connectors
    assert any(ln.startswith(("├─", "└─")) for ln in out.splitlines())

    # No None leak
    assert "[None]" not in out
    assert "(None)" not in out

    # Both versions present → status says "both"
    assert "both" in out

    with neo4j_driver.session() as session:
        session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=_DIFF_VERSION)


@pytest.mark.postgres
def test_suggest_pattern_output_contract(pattern_snapshot_db, clean_pg_embeddings):
    """suggest_pattern header + tree connectors + no None leak.

    Postgres-marked because pgvector ANN is required. Skipped when extension
    not installed — same gate as find_examples / mcp_pattern_tools.
    """
    from psycopg2.extras import execute_values

    from src.indexer.embedder import FakeEmbedder
    from src.indexer.models import PatternExample
    from src.indexer.writer_pgvector import (
        _INSERT_SQL,
        make_pattern_chunks,
    )

    pe = PatternExample(
        pattern_id="snap-action-return-super",
        intent_keywords=["action"],
        file_ref="addons/sale/models/sale_order.py:1",
        snippet_text="def action_confirm(self): return super().action_confirm()",
        gotchas=["Always return"],
        odoo_version_min=pattern_snapshot_db,
        language="python",
    )
    embedder = FakeEmbedder(dim=1024)
    chunks = make_pattern_chunks([pe])
    vecs = embedder.embed([c.content for c in chunks])
    with clean_pg_embeddings.cursor() as cur:
        execute_values(
            cur, _INSERT_SQL,
            [c.as_tuple(vecs[i]) for i, c in enumerate(chunks)],
        )

    from src.mcp.server import _suggest_pattern
    out = _suggest_pattern(
        "action confirm super",
        odoo_version=pattern_snapshot_db,
        language="python",
        _driver=None, _pg_conn=clean_pg_embeddings, _embedder=embedder,
    )
    assert out.startswith("suggest_pattern(")
    assert "matches" in out
    assert "None" not in out
    assert any(ln.startswith(("├─", "└─")) for ln in out.splitlines())


def test_setup_indexes_creates_all_spec_indexes(neo4j_driver):
    """Integration: setup_indexes() creates 4 M4.5 spec-layer indexes."""
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()
    with neo4j_driver.session() as session:
        indexes = session.run("SHOW INDEXES").data()
    writer.close()
    labels_props = [
        (i.get("labelsOrTypes") or [], i.get("properties") or [])
        for i in indexes
    ]
    expected = [
        ("CoreSymbol", "qualified_name"),
        ("LintRule", "rule_id"),
        ("CLICommand", "name"),
        ("CLIFlag", "flag_name"),
    ]
    for label, prop in expected:
        found = any(label in lbls and prop in props for lbls, props in labels_props)
        assert found, f"Missing index on ({label}, {prop})"


# --- M5.5 Wave 2 resolve_view snapshot contracts ---------------------------

class TestResolveViewSnapshots:
    """Lock resolve_view output contract (M5.5 Wave 2) — format change breaks tests."""

    pytestmark = pytest.mark.neo4j

    _VIEW_SNAP_VERSION = "95.0"

    @pytest.fixture(autouse=True)
    def _setup_env(self, monkeypatch):
        """Setup Neo4j connection env vars for resolve_view tests."""
        monkeypatch.setenv(
            "NEO4J_URI", os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        )
        monkeypatch.setenv(
            "NEO4J_USER", os.getenv("NEO4J_TEST_USER", "neo4j"),
        )
        monkeypatch.setenv(
            "NEO4J_PASSWORD", os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        import sys
        sys.modules.pop("src.mcp.server", None)
        yield

    def test_resolve_view_base_only(self, neo4j_driver):
        """Test 1: base-only view (no extensions, no parent).

        Wave 3 (ADR-0023 §1.6): empty Extended-by section is now silently
        skipped — the prior ``└─ No extensions`` literal was removed because
        ``resolve_view`` is overview intent, not enumeration intent.

        Wave 5 (ADR-0023 §4): drill-down tools terminate with ``└─ Next:``.

        Output must contain:
        - Header: "sale.view_sale_form (Odoo 95.0)"
        - Type: form
        - Model: sale.order
        - "└─ Next:" footer (the empty Extended-by section is silent-skipped)
        """
        from src.indexer.models import ModuleInfo, ViewInfo, ViewParseResult

        # Setup: clean and seed only the base view
        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        writer.setup_indexes()
        with neo4j_driver.session() as session:
            session.run(
                "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n",
                v=self._VIEW_SNAP_VERSION,
            )

        sale_mod = ModuleInfo(
            name="sale",
            odoo_version=self._VIEW_SNAP_VERSION,
            repo="odoo",
            path="/odoo/addons/sale",
            depends=[],
            version_raw="",
        )
        base_view = ViewInfo(
            xmlid="sale.view_sale_form",
            name="sale.order Form",
            model="sale.order",
            module="sale",
            odoo_version=self._VIEW_SNAP_VERSION,
            view_type="form",
            mode="primary",
            inherit_xmlid=None,
            xpaths=[],
        )
        writer.write_view_results([ViewParseResult(module=sale_mod, views=[base_view], qweb=[])])
        writer.close()

        from src.mcp.server import _resolve_view

        result = _resolve_view("sale.view_sale_form", self._VIEW_SNAP_VERSION)
        lines = result.splitlines()

        assert lines[0] == f"sale.view_sale_form (Odoo {self._VIEW_SNAP_VERSION})", (
            "Header must be '<xmlid> (Odoo <version>)'"
        )
        assert any("Type:" in ln and "form" in ln for ln in lines), (
            "Missing 'Type: form' line"
        )
        assert any("Model:" in ln and "sale.order" in ln for ln in lines), (
            "Missing 'Model: sale.order' line"
        )
        # Wave 3 (ADR-0023 §1.6): empty Extended-by branch is silent-skipped;
        # the prior 'No extensions' literal MUST NOT appear.
        assert not any("No extensions" in ln for ln in lines), (
            f"'No extensions' string must be silent-skipped now, got: {lines}"
        )
        # Wave 5 (ADR-0023 §4): drill-down tools terminate with '└─ Next:'.
        assert lines[-1].startswith("└─ Next:"), (
            f"resolve_view must end with '└─ Next:' footer, got: {lines[-1]!r}"
        )
        assert any(ln.startswith("├─") or ln.startswith("└─") for ln in lines), (
            "Missing tree connectors"
        )

        # Cleanup
        with neo4j_driver.session() as session:
            session.run(
                "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n",
                v=self._VIEW_SNAP_VERSION,
            )

    def test_resolve_view_extension_with_xpath(self, neo4j_driver):
        """Test 2: extension view (has parent, has xpaths).

        When querying the EXTENSION view's xmlid, output must contain:
        - "Inherits from: sale.view_sale_form"
        - "XPath modifications (1):"
        - The xpath expr "//field[@name='partner_id']" with position "before"
        """
        from src.indexer.models import ModuleInfo, ViewInfo, ViewParseResult, XPathInfo

        # Setup: seed base view + extension
        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        writer.setup_indexes()
        with neo4j_driver.session() as session:
            session.run(
                "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n",
                v=self._VIEW_SNAP_VERSION,
            )

        sale_mod = ModuleInfo(
            name="sale",
            odoo_version=self._VIEW_SNAP_VERSION,
            repo="odoo",
            path="/odoo/addons/sale",
            depends=[],
            version_raw="",
        )
        viin_sale_mod = ModuleInfo(
            name="viin_sale",
            odoo_version=self._VIEW_SNAP_VERSION,
            repo="acme_addons",
            path="/acme_addons/viin_sale",
            depends=[],
            version_raw="",
        )
        base_view = ViewInfo(
            xmlid="sale.view_sale_form",
            name="sale.order Form",
            model="sale.order",
            module="sale",
            odoo_version=self._VIEW_SNAP_VERSION,
            view_type="form",
            mode="primary",
            inherit_xmlid=None,
            xpaths=[],
        )
        ext_view = ViewInfo(
            xmlid="viin_sale.view_sale_form_inherit",
            name="Sale Order Form Extension",
            model="sale.order",
            module="viin_sale",
            odoo_version=self._VIEW_SNAP_VERSION,
            view_type="form",
            mode="extension",
            inherit_xmlid="sale.view_sale_form",
            xpaths=[XPathInfo(expr="//field[@name='partner_id']", position="before")],
        )
        writer.write_view_results([
            ViewParseResult(module=sale_mod, views=[base_view], qweb=[]),
            ViewParseResult(module=viin_sale_mod, views=[ext_view], qweb=[]),
        ])
        writer.close()

        from src.mcp.server import _resolve_view

        result = _resolve_view("viin_sale.view_sale_form_inherit", self._VIEW_SNAP_VERSION)
        lines = result.splitlines()

        assert lines[0] == f"viin_sale.view_sale_form_inherit (Odoo {self._VIEW_SNAP_VERSION})", (
            "Header must be extension xmlid"
        )
        assert any("Inherits from:" in ln and "sale.view_sale_form" in ln for ln in lines), (
            "Missing 'Inherits from: sale.view_sale_form' line"
        )
        assert any("XPath modifications" in ln for ln in lines), (
            "Missing 'XPath modifications' line"
        )
        assert any("//field[@name='partner_id']" in ln for ln in lines), (
            "Missing xpath expression"
        )
        assert any("[before]" in ln for ln in lines), (
            "Missing xpath position [before]"
        )

        # Cleanup
        with neo4j_driver.session() as session:
            session.run(
                "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n",
                v=self._VIEW_SNAP_VERSION,
            )

    def test_resolve_view_base_with_extensions(self, neo4j_driver):
        """Test 3: base view with multiple extensions.

        When querying the BASE view: output contains "Extended by (2 modules):"
        and both extension xmlids appear.
        """
        from src.indexer.models import ModuleInfo, ViewInfo, ViewParseResult, XPathInfo

        # Setup: seed base + 2 extensions
        writer = Neo4jWriter(
            uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            user=os.getenv("NEO4J_TEST_USER", "neo4j"),
            password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
        )
        writer.setup_indexes()
        with neo4j_driver.session() as session:
            session.run(
                "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n",
                v=self._VIEW_SNAP_VERSION,
            )

        sale_mod = ModuleInfo(
            name="sale",
            odoo_version=self._VIEW_SNAP_VERSION,
            repo="odoo",
            path="/odoo/addons/sale",
            depends=[],
            version_raw="",
        )
        viin_sale_mod = ModuleInfo(
            name="viin_sale",
            odoo_version=self._VIEW_SNAP_VERSION,
            repo="acme_addons",
            path="/acme_addons/viin_sale",
            depends=[],
            version_raw="",
        )
        to_sale_mod = ModuleInfo(
            name="to_sale_custom",
            odoo_version=self._VIEW_SNAP_VERSION,
            repo="customer",
            path="/customer/to_sale_custom",
            depends=[],
            version_raw="",
        )

        base_view = ViewInfo(
            xmlid="sale.view_sale_form",
            name="sale.order Form",
            model="sale.order",
            module="sale",
            odoo_version=self._VIEW_SNAP_VERSION,
            view_type="form",
            mode="primary",
            inherit_xmlid=None,
            xpaths=[],
        )
        ext_view1 = ViewInfo(
            xmlid="viin_sale.view_sale_form_inherit",
            name="Sale Order Form Extension",
            model="sale.order",
            module="viin_sale",
            odoo_version=self._VIEW_SNAP_VERSION,
            view_type="form",
            mode="extension",
            inherit_xmlid="sale.view_sale_form",
            xpaths=[XPathInfo(expr="//field[@name='partner_id']", position="before")],
        )
        ext_view2 = ViewInfo(
            xmlid="to_sale_custom.view_sale_form_inherit",
            name="Sale Order Custom Extension",
            model="sale.order",
            module="to_sale_custom",
            odoo_version=self._VIEW_SNAP_VERSION,
            view_type="form",
            mode="extension",
            inherit_xmlid="sale.view_sale_form",
            xpaths=[XPathInfo(expr="//field[@name='amount_total']", position="after")],
        )

        writer.write_view_results([
            ViewParseResult(module=sale_mod, views=[base_view], qweb=[]),
            ViewParseResult(module=viin_sale_mod, views=[ext_view1], qweb=[]),
            ViewParseResult(module=to_sale_mod, views=[ext_view2], qweb=[]),
        ])
        writer.close()

        from src.mcp.server import _resolve_view

        result = _resolve_view("sale.view_sale_form", self._VIEW_SNAP_VERSION)
        lines = result.splitlines()

        assert lines[0] == f"sale.view_sale_form (Odoo {self._VIEW_SNAP_VERSION})"
        assert any("Extended by (2 modules):" in ln for ln in lines), (
            "Missing 'Extended by (2 modules):' line when base has 2 extensions"
        )
        assert any("viin_sale.view_sale_form_inherit" in ln for ln in lines), (
            "Missing first extension xmlid"
        )
        assert any("to_sale_custom.view_sale_form_inherit" in ln for ln in lines), (
            "Missing second extension xmlid"
        )

        # Cleanup
        with neo4j_driver.session() as session:
            session.run(
                "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n",
                v=self._VIEW_SNAP_VERSION,
            )

