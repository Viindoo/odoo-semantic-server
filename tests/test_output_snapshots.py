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
    def _seed(self, request, neo4j_driver):
        from src.db.migrate import _vector_extension_available
        # Check if pg_conn fixture is available (reachable)
        pg_conn_fixture = (
            request.getfixturevalue("pg_conn") if "pg_conn" in request.fixturenames else None
        )
        if pg_conn_fixture is None:
            pytest.skip("PostgreSQL not reachable")
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
        write_module_embeddings(pg_conn_fixture, "snap_mod", _SNAP_VERSION, chunks, embedder)

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
    assert out.startswith("lint_check")
    assert "None" not in out


def test_cli_help_output_contract(spec_snapshot_db):
    from src.mcp.server import _cli_help
    out = _cli_help("server", "--longpolling-port", spec_snapshot_db)
    _assert_tree_format(out, "cli_help")
    assert "Status:" in out
    assert "--gevent-port" in out  # replacement surfaced


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

