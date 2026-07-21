# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_lookup_core_api_note_render.py
"""issue #364 C4 - the CoreSymbol.note render leg of lookup_core_api.

`note` (tools_symbol.schema.json) is a curated lifecycle/misuse/gotcha string:
204/204 curated tools_symbols_<version>.json entries carry one, but nothing in
the load/write/MCP chain read it - the schema's own "Shown in tool output"
claim was false for every request a user could make. This issue's fix wires
the two hops that ARE reachable without touching parser_tools_symbols.py
(reserved for concurrent oracle work as of this pass):

  1. write:  writer_neo4j_spec.py::_write_core_symbols_batch persists
             CoreSymbolInfo.note to CoreSymbol.note (Neo4j-only, see
             test_mcp_spec_tools.py for a round-trip check).
  2. render: src/mcp/tools/spec.py::_format_core_symbol renders a "Note:"
             line when the fetched record carries one - THIS is what these
             tests pin, since it is a pure function (dict in, str out) and
             needs no Neo4j.

The THIRD hop - src/indexer/parser_tools_symbols.py::_load_static_tools_symbols
reading the JSON `note` key into CoreSymbolInfo.note - is NOT wired here (see
models.CoreSymbolInfo's docstring and
tests/test_parser_tools_symbols.py::TestNoteFieldPendingLoaderWiring, which
pins that exact gap so it is visible rather than latent).

Pure no-DB unit tests - no Neo4j needed, so they run in the
`not neo4j and not pg` lane.
"""
from src.mcp.tools.spec import _format_core_symbol


def _base_rec(**overrides) -> dict:
    rec = {
        "qualified_name": "odoo.tools.mute_logger",
        "kind": "tool_export",
        "status": "stable",
        "signature": "mute_logger(*loggers) -> contextmanager",
    }
    rec.update(overrides)
    return rec


def test_note_line_rendered_when_present():
    rec = _base_rec(note="Context manager to silence specific loggers. Use only in tests.")
    out = _format_core_symbol(rec, "17.0")
    assert "├─ Note:        Context manager to silence specific loggers. Use only in tests." in out


def test_note_line_absent_when_none():
    """A CoreSymbol with no note (the current reality for every static-JSON-
    sourced entry, per the loader gap this file's docstring names) must not
    render an empty/placeholder Note line."""
    rec = _base_rec(note=None)
    out = _format_core_symbol(rec, "17.0")
    assert "Note:" not in out


def test_note_line_absent_when_key_missing():
    """Defensive: a record dict that never had a 'note' key at all (older
    Cypher result shape, before this fix added `cs.note AS note`) must not
    raise or render a stray line."""
    rec = _base_rec()
    assert "note" not in rec
    out = _format_core_symbol(rec, "17.0")
    assert "Note:" not in out


def test_note_line_position_before_footer():
    """Note renders as an interior tree line (├─), never the final line - the
    Next-step footer (└─) must always stay last (ADR-0023 tree grammar)."""
    rec = _base_rec(note="Some curated gotcha.")
    out = _format_core_symbol(rec, "17.0")
    lines = out.splitlines()
    note_idx = next(i for i, ln in enumerate(lines) if ln.startswith("├─ Note:"))
    assert note_idx < len(lines) - 1
    assert lines[note_idx].startswith("├─")
    assert lines[-1].startswith("└─") or "└─" in lines[-1]
