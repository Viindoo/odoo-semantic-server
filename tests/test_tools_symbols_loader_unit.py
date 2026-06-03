# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_tools_symbols_loader_unit.py
"""Pure-logic unit tests extracted from test_tools_symbols_integration.py (WS-D / DD2 demote).

The ``TestLoaderDataCompleteness`` class only calls ``_load_static_tools_symbols``,
which reads the curated JSON spec files from ``src/indexer/spec_data`` on disk — it
never opens a Neo4j session, never requests the ``neo4j_driver`` / ``seeded_tools_neo4j``
fixtures, and never instantiates the MCP tool resolver.  The parent file carries a
module-level ``pytestmark = pytest.mark.neo4j`` (its other classes seed and query real
CoreSymbol nodes), which a per-test override cannot subtract; so these pure loader
tests live here in an unmarked module and now run in the fast unit tier
(``-m 'not neo4j'``).

DD2 evidence: confirmed file-system read only via ``_load_static_tools_symbols`` —
no DB fixture dependency.
"""
from src.indexer.parser_tools_symbols import _load_static_tools_symbols

# Use real version strings to validate lifecycle correctness.
TOOLS_V16 = "16.0"
TOOLS_V17 = "17.0"

_SPEC_DATA_DIR = (
    __import__("pathlib").Path(__file__).parent.parent / "src" / "indexer" / "spec_data"
)


class TestLoaderDataCompleteness:
    """Spot-check that the loaded symbols have the expected lifecycle."""

    def test_v16_has_no_sql_symbol(self):
        syms = _load_static_tools_symbols(TOOLS_V16, static_data_dir=_SPEC_DATA_DIR)
        qnames = {s.qualified_name for s in syms}
        assert "odoo.tools.SQL" not in qnames

    def test_v17_has_sql_symbol_stable(self):
        syms = _load_static_tools_symbols(TOOLS_V17, static_data_dir=_SPEC_DATA_DIR)
        sql_sym = next((s for s in syms if s.qualified_name == "odoo.tools.SQL"), None)
        assert sql_sym is not None
        assert sql_sym.status == "stable"
        assert sql_sym.kind == "tool_export"

    def test_v17_has_deprecated_html_escape(self):
        syms = _load_static_tools_symbols(TOOLS_V17, static_data_dir=_SPEC_DATA_DIR)
        html_escape = next(
            (s for s in syms if s.qualified_name == "odoo.tools.html_escape"), None
        )
        assert html_escape is not None
        assert html_escape.status == "deprecated"
        assert html_escape.replacement_qname == "markupsafe.escape"
