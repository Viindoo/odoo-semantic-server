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

    def test_html_escape_is_stable_at_every_version_that_has_it(self):
        """Core calls html_escape throughout v17-v20 and no source marks it
        deprecated, so it is stable with no replacement at every major.
        """
        for major in range(8, 21):
            syms = _load_static_tools_symbols(f"{major}.0", static_data_dir=_SPEC_DATA_DIR)
            html_escape = next(s for s in syms if s.qualified_name == "odoo.tools.html_escape")
            assert html_escape.status == "stable", major
            assert html_escape.replacement_qname is None, major

    def test_ustr_is_deprecated_from_v18_and_stable_before(self):
        """odoo/loglevels.py emits a DeprecationWarning from ustr() starting
        Odoo 18; every earlier major that carries it keeps it stable.
        """
        for major in range(8, 20):
            version = f"{major}.0"
            syms = _load_static_tools_symbols(version, static_data_dir=_SPEC_DATA_DIR)
            ustr = next(s for s in syms if s.qualified_name == "odoo.tools.ustr")
            assert ustr.status == ("deprecated" if major >= 18 else "stable"), version

    def test_pycompat_is_curated_from_v11_to_v19_and_deprecated_from_v18(self):
        """pycompat.py exists at v11-v19 (resolvable as a submodule even when
        odoo/tools/__init__.py stops importing it) and is gone at v20.
        """
        for major in range(8, 21):
            version = f"{major}.0"
            syms = _load_static_tools_symbols(version, static_data_dir=_SPEC_DATA_DIR)
            entry = next((s for s in syms if s.qualified_name == "odoo.tools.pycompat"), None)
            if major < 11 or major > 19:
                assert entry is None, version
            else:
                assert entry is not None, version
                assert entry.status == ("deprecated" if major >= 18 else "stable"), version

    def test_html_escape_alias_is_carried_as_its_source_statement_from_v15(self):
        """From v15 odoo/tools/misc.py binds html_escape = markupsafe.escape, so
        the curated signature is that statement, not a function signature.
        """
        for major in range(15, 21):
            syms = _load_static_tools_symbols(f"{major}.0", static_data_dir=_SPEC_DATA_DIR)
            entry = next(s for s in syms if s.qualified_name == "odoo.tools.html_escape")
            assert entry.signature == "html_escape = markupsafe.escape", major
