# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_pipeline_core_symbol_dedup.py
"""index_core() parsed/curated CoreSymbol dedup — issue #364 C3.

`pipeline.index_core()` merges CoreSymbol nodes from two sources: symbols
PARSED from real source (`parse_odoo_core`) and CURATED `odoo.tools.*` symbols
(`load_tools_symbols`, `spec_data/tools_symbols_<version>.json`). Its own
comment states the merge order exists specifically so a curated entry can
never clobber a real parsed node - but for any symbol defined in an
`odoo/tools/<submodule>.py` file that is ALSO in `_CORE_FILES` (e.g.
`odoo/tools/sql.py`), the dedup's plain string-equality check
(`s.qualified_name not in parsed_qnames`) never fires: `parse_odoo_core`
qualifies the symbol by its real submodule path (`odoo.tools.sql.SQL`) while
the curated JSON always uses the flat re-export name (`odoo.tools.SQL`) -
`"odoo.tools.SQL" not in {"odoo.tools.sql.SQL", ...}` is always True. BOTH
nodes get written, and `lookup_core_api("SQL", ...)` deterministically prefers
the THINNER curated node (spec.py's Cypher ranks an exact qualified_name match
ahead of a suffix match), silently hiding the real, fuller, source-verified
node - a live, user-visible wrong answer, not data drift.

This test drives the REAL `index_core()` pipeline function (not a hand-seeded
Neo4j fixture) against a minimal synthetic Odoo tree that reproduces the exact
collision shape, then asserts on BEHAVIOR: `lookup_core_api("SQL", ...)`
returns the fuller (parsed) node - real file_path/line, no `tool_export`
placeholder metadata - never the thinner curated one. It fails before the
pipeline.py fix (both nodes get written; ranking picks the curated one) and
passes after (only the parsed node is ever written, because the curated
duplicate is now correctly excluded by the flattened-alias dedup).
"""
import json
import os
import sys

import pytest

from src.indexer.pipeline import index_core
from src.indexer.writer_neo4j import Neo4jWriter

pytestmark = pytest.mark.neo4j

# Dedicated test version — unused elsewhere in the suite (grep-verified against
# every other test file's version-string literals before picking it).
CORE_TEST_VERSION = "92.5"


@pytest.fixture
def mini_odoo_tree_with_sql(tmp_path):
    """Minimal fake Odoo source tree whose odoo/tools/sql.py defines a real
    `class SQL` — reproducing the exact file _CORE_FILES walks in production
    (`odoo/tools/sql.py`), so `parse_odoo_core` emits `odoo.tools.sql.SQL`
    with a real file_path/line, just like it does for the real Odoo source.
    """
    odoo_dir = tmp_path / "odoo"
    odoo_dir.mkdir()
    tools_dir = odoo_dir / "tools"
    tools_dir.mkdir()

    (tools_dir / "safe_eval.py").write_text(
        "def safe_eval(expr, globals_dict=None, locals_dict=None, **kwargs):\n"
        "    pass\n"
    )
    (tools_dir / "query.py").write_text(
        "class Query:\n    def select(self, *args): pass\n"
    )
    # The file under audit: a real, parseable SQL class (mirrors the shape of
    # odoo/tools/sql.py's real `class SQL` across v17-v19).
    (tools_dir / "sql.py").write_text(
        "class SQL:\n"
        "    '''SQL query builder for safe parameterized queries.'''\n"
        "    def code(self): pass\n"
        "    def params(self): pass\n"
        "    def join(self, *args): pass\n"
    )
    (odoo_dir / "fields.py").write_text(
        "class Field:\n    def __init__(self, *args, **kwargs): pass\n"
        "class Char(Field): pass\n"
    )
    (odoo_dir / "models.py").write_text(
        "class BaseModel:\n    def write(self, vals): pass\n"
        "    def read(self, fields): pass\n"
    )
    (odoo_dir / "api.py").write_text(
        "def model(fn): return fn\n"
        "def depends(*args): pass\n"
    )
    (odoo_dir / "sql_db.py").write_text(
        "class Cursor:\n    def execute(self, query): pass\n"
    )
    (odoo_dir / "exceptions.py").write_text(
        "class UserError(Exception): pass\n"
        "class ValidationError(Exception): pass\n"
    )
    return tmp_path


@pytest.fixture
def spec_data_with_curated_sql(tmp_path):
    """Curated spec_data with a flat `odoo.tools.SQL` tools_symbols entry — the
    real shape of `tools_symbols_17.0.json`'s actual SQL entry in production
    (`{"qualified_name": "odoo.tools.SQL", "kind": "tool_export", ...}`, no
    file_path/line — curated JSON never carries source location).
    """
    spec_dir = tmp_path / "spec_data"
    spec_dir.mkdir(exist_ok=True)

    tools_symbols_data = {
        "_curate_status": "complete",
        "symbols": [
            {
                "qualified_name": "odoo.tools.SQL",
                "kind": "tool_export",
                "status": "stable",
                "signature": "class SQL",
                "note": "SQL query builder for safe parameterized queries.",
            },
        ],
    }
    (spec_dir / f"tools_symbols_{CORE_TEST_VERSION}.json").write_text(
        json.dumps(tools_symbols_data)
    )

    # Empty-but-valid placeholders for the other two spec_data families, so
    # index_core()'s lint/CLI steps have something well-formed to load.
    (spec_dir / f"lint_rules_{CORE_TEST_VERSION}.json").write_text(
        json.dumps({"_curate_status": "pending", "rules": []})
    )
    (spec_dir / f"cli_flags_{CORE_TEST_VERSION}.json").write_text(
        json.dumps({"_curate_status": "pending", "flags": []})
    )
    return spec_dir


@pytest.fixture
def dedup_writer(neo4j_driver):
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=CORE_TEST_VERSION,
        )
    yield writer
    with neo4j_driver.session() as session:
        session.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=CORE_TEST_VERSION,
        )
    writer.close()


@pytest.fixture
def mcp_server_module(neo4j_driver):
    """Import the MCP server module bound to the test Neo4j credentials, AFTER
    the caller has already written data (mirrors the established pattern in
    tests/test_find_deprecated_usage_acl_family.py / test_tools_symbols_integration.py).
    """
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    sys.modules.pop("src.mcp.server", None)
    from src.mcp import server as mcp_server
    return mcp_server


class TestCoreSymbolDedupAcrossSubmoduleBoundary:
    def test_only_one_sql_core_symbol_node_is_written(
        self, mini_odoo_tree_with_sql, spec_data_with_curated_sql, dedup_writer, neo4j_driver,
    ):
        """The dedup step must exclude the curated flat entry once a
        submodule-qualified parsed entry for the same real symbol exists -
        exactly ONE CoreSymbol node named SQL, not two.
        """
        index_core(
            source_root=str(mini_odoo_tree_with_sql),
            odoo_version=CORE_TEST_VERSION,
            writer=dedup_writer,
            static_data_dir=str(spec_data_with_curated_sql),
        )
        with neo4j_driver.session() as session:
            rows = session.run(
                "MATCH (cs:CoreSymbol {odoo_version: $v}) "
                "WHERE cs.qualified_name ENDS WITH '.SQL' OR cs.qualified_name = 'odoo.tools.SQL' "
                "RETURN cs.qualified_name AS qualified_name, cs.kind AS kind, "
                "       cs.file_path AS file_path, cs.line AS line",
                v=CORE_TEST_VERSION,
            ).data()
        assert len(rows) == 1, (
            f"Expected exactly 1 SQL-named CoreSymbol node, got {len(rows)}: {rows}"
        )
        # The surviving node must be the FULLER, parsed one (real file_path/line,
        # submodule-qualified name) - never the thinner curated placeholder.
        assert rows[0]["qualified_name"] == "odoo.tools.sql.SQL"
        assert rows[0]["file_path"] is not None
        assert rows[0]["line"] is not None

    def test_lookup_core_api_returns_the_fuller_parsed_node(
        self,
        mini_odoo_tree_with_sql,
        spec_data_with_curated_sql,
        dedup_writer,
        neo4j_driver,
        mcp_server_module,
    ):
        """End-to-end behavioral proof: lookup_core_api("SQL", ...) - the
        natural way an agent queries this symbol - must surface the real
        source location and the class's own methods, not the curated
        placeholder's generic "class SQL" signature with no file_path/line.
        """
        index_core(
            source_root=str(mini_odoo_tree_with_sql),
            odoo_version=CORE_TEST_VERSION,
            writer=dedup_writer,
            static_data_dir=str(spec_data_with_curated_sql),
        )
        out = mcp_server_module._lookup_core_api("SQL", CORE_TEST_VERSION)
        assert "not found" not in out.lower(), f"SQL must be found:\n{out}"
        # The fuller (parsed) node reports a real source location.
        assert "sql.py" in out, (
            f"Expected the parsed node's real file_path (odoo/tools/sql.py) in "
            f"the output, got the thinner curated placeholder instead:\n{out}"
        )
