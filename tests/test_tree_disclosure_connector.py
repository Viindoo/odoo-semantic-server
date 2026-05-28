# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_tree_disclosure_connector.py
"""ADR-0023 §1.2 contract: the truncation-disclosure row is a real last child.

When a capped section renders "... and N more (...)" as its final row, that row
is the LAST child of its parent branch and therefore MUST carry the └─ connector
(ADR-0023 §1.2). Two MCP renderers build this shape from the same helpers:

  * `_resolve_method` override-chain section (server.py)
  * `_append_capped_section` (impact_analysis) (server.py)

Both were refactored to delegate the connector assignment to
`render_list_block`, so the disclosure row can no longer be special-cased out of
its └─. These DB-free unit tests pin that contract at the helper-composition
level (the exact `render_list_block(_render_capped(...))` pipeline both call
sites now use), so a regression that re-introduces a connector-less disclosure
row turns red without needing Neo4j.
"""
from src.mcp.server import LIST_PREVIEW_MAX_ITEMS, _render_capped
from src.mcp.tree_builder import render_list_block


def test_disclosure_row_is_last_child_with_branch_connector():
    """Over-cap list: the '... and N more' row must end the block with └─."""
    total = LIST_PREVIEW_MAX_ITEMS + 7
    items = list(range(total))

    rendered = _render_capped(
        items[:LIST_PREVIEW_MAX_ITEMS],
        str,
        cap=LIST_PREVIEW_MAX_ITEMS,
        total=total,
        more_hint="model_inspect(...) for full list",
    )
    # Sanity: the last rendered row is the disclosure row.
    assert rendered[-1].startswith("... and "), rendered[-1]

    # Parent header was appended as a non-last child (├─) → prefix "│   ".
    block = render_list_block(rendered, prefix="│   ")

    last = block[-1]
    assert last.startswith("│   └─ ... and "), (
        "ADR-0023 §1.2: the disclosure row must be the last child and carry the "
        f"└─ connector; got {last!r}"
    )
    # No interior row may use └─ — only the final one.
    for interior in block[:-1]:
        assert interior.startswith("│   ├─ "), (
            f"Interior rows must use ├─, not └─; got {interior!r}"
        )


def test_capped_block_last_row_always_has_terminal_connector():
    """Whether or not the list is over cap, the LAST row of the block gets └─."""
    # Under cap — no disclosure row; the last data row terminates the block.
    under = [f"row{i}" for i in range(3)]
    rendered_under = _render_capped(under, str, cap=LIST_PREVIEW_MAX_ITEMS)
    block_under = render_list_block(rendered_under, prefix="│   ")
    assert block_under[-1] == "│   └─ row2", block_under[-1]
    assert "..." not in block_under[-1]

    # Exactly at cap — still no disclosure, last data row terminates.
    at_cap = [f"row{i}" for i in range(LIST_PREVIEW_MAX_ITEMS)]
    rendered_at = _render_capped(at_cap, str, cap=LIST_PREVIEW_MAX_ITEMS)
    block_at = render_list_block(rendered_at, prefix="│   ")
    assert block_at[-1].startswith("│   └─ row"), block_at[-1]
    assert "..." not in block_at[-1]
