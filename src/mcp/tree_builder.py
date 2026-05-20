# SPDX-License-Identifier: AGPL-3.0-or-later
"""ADR-0023 §1 tree-grammar renderer.

This module provides two public APIs:

1. ``TreeNode`` — A node in a tree that auto-applies the ADR-0023 §1.3 indent
   rules (``├─`` for non-last child, ``└─`` for last child, ``│   `` vertical
   continuation under non-last parents, 4 spaces under last parents).

   Usage::

       root = TreeNode("sale.order (Odoo 17.0)")
       root.add("├─ Defined in:     [odoo_17.0] sale")
       ext = root.add("├─ Extended by:")
       ext.add("[odoo_17.0] sale_stock")
       ext.add("[acme_enterprise_17.0] viin_sale")
       root.add("├─ Fields:         72")
       root.add("└─ Methods:        58")
       print(root.render())

   ``TreeNode.render()`` returns the multiline string with correct connectors
   and indentation derived from sibling position.

2. ``render_list_block`` — A lightweight helper for the dominant "list of leaf
   rows under a header" pattern. Both ``_resolve_model`` (Extended by sublist)
   and ``_list_fields`` (per-module field rows) use this pattern.

   Instead of rewriting entire functions to use a single root ``TreeNode``
   (which would risk byte-drift on edge cases), functions keep their existing
   flat ``lines: list[str]`` structure and call ``render_list_block`` only for
   the inner indent-string assembly.

   Usage::

       # Parent branch was already appended as a non-last child (├─):
       lines += render_list_block(rows, prefix="│   ")

       # Parent branch was already appended as a last child (└─):
       lines += render_list_block(rows, prefix="    ")

   ``render_list_block`` attaches ``├─`` / ``└─`` connectors automatically;
   callers only choose the vertical-continuation prefix per ADR-0023 §1.3.

Design notes
------------
The two PoC migrations (``_resolve_model``, ``_list_fields``) keep the flat
``lines: list[str]`` + ``'\\n'.join(lines)`` return shape.  Only the inner
``for i, row in enumerate(rendered): ... lines.append(f"│   {connector} {row}")``
loops are replaced with ``render_list_block(rendered)``. This is intentional:

* Minimises blast radius — surrounding logic is unchanged.
* Avoids byte-drift edge cases that would arise from forcing a single-root
  ``TreeNode`` onto functions that emit sibling-level header rows interleaved
  with sublists.
* The ``render_list_block`` abstraction is where the indent contract lives;
  future tools can call it directly instead of hand-coding ``│   ├─ `` strings.

ADR-0023 §1.3 summary (enforced here):
    Non-last parent (parent uses ``├─``): sublist indent = ``│   `` (4 chars).
    Last parent    (parent uses ``└─``): sublist indent = ``    `` (4 spaces).
"""

class TreeNode:
    """ADR-0023 §1 tree-grammar node.

    Renders to a multiline string using ``├─`` / ``└─`` / ``│   `` (or
    4-space) indentation derived from sibling position and ancestor chain.

    Parameters
    ----------
    label:
        The text content for this node (without connectors or indent).
        For the root node this is the header line (e.g.
        ``"sale.order (Odoo 17.0)"``).  For child nodes it is the bare
        row text (e.g. ``"[odoo_17.0] sale_stock"``).
    children:
        Optional initial child list.  Each element is a ``TreeNode``; pass
        ``None`` (default) for a leaf.
    is_root:
        Set ``True`` for the top-level header node so ``render()`` omits
        the connector/indent prefix on that line.
    """

    def __init__(
        self,
        label: str,
        *,
        children: list["TreeNode"] | None = None,
        is_root: bool = False,
    ) -> None:
        self.label = label
        self.children: list[TreeNode] = children if children is not None else []
        self.is_root = is_root

    def add(self, child: "TreeNode | str") -> "TreeNode":
        """Append a child node and return it.

        If ``child`` is a plain ``str`` it is wrapped in a leaf ``TreeNode``
        (no further children).  Returns the appended node so callers can
        chain: ``node.add("header").add("row1").add("row2")``.
        """
        if isinstance(child, str):
            child = TreeNode(child)
        self.children.append(child)
        return child

    def render(self) -> str:
        """Return the full multiline tree as a single string.

        ADR-0023 §1.2–§1.3 rules applied recursively:

        * ``├─`` for every non-last child; ``└─`` for the last child.
        * Non-last parent (``├─``) → descendant indent prefix ``│   ``.
        * Last parent    (``└─``) → descendant indent prefix ``    ``.
        """
        lines: list[str] = []
        self._render_into(lines, prefix="", is_last=True)
        return "\n".join(lines)

    def _render_into(
        self,
        lines: list[str],
        prefix: str,
        is_last: bool,
    ) -> None:
        if self.is_root:
            lines.append(self.label)
            child_prefix = ""
        else:
            connector = "└─" if is_last else "├─"
            lines.append(f"{prefix}{connector} {self.label}")
            child_prefix = prefix + ("    " if is_last else "│   ")

        for idx, child in enumerate(self.children):
            child_is_last = idx == len(self.children) - 1
            child._render_into(lines, child_prefix, child_is_last)


# ---------------------------------------------------------------------------
# render_list_block — surgical helper for the flat-lines refactor pattern
# ---------------------------------------------------------------------------

def render_list_block(rows: list[str], *, prefix: str = "│   ") -> list[str]:
    """Render a list of leaf rows with ADR-0023 §1 connectors and given indent.

    Returns a list of lines such as::

        ["│   ├─ row1", "│   ├─ row2", "│   └─ rowN"]

    suitable for direct extension of a flat ``lines: list[str]`` that the
    caller is building.

    Parameters
    ----------
    rows:
        The pre-formatted row strings (no connectors, no indent).  Typically
        the return value of ``_render_capped(...)``.  An empty list returns
        an empty list.
    prefix:
        The vertical-continuation prefix that precedes the connector.
        Per ADR-0023 §1.3:

        * ``"│   "`` (pipe + 3 spaces, 4 chars) when the parent branch used
          ``├─`` (non-last child — vertical line must continue up).
        * ``"    "`` (4 spaces) when the parent branch used ``└─`` (last
          child — vertical line ends at parent, no pipe needed).

        Default is ``"│   "`` — covers the common case where the caller
        appended the parent header as a non-last child (``├─``).

    Notes
    -----
    This helper is intentionally *not* a full ``TreeNode`` construction.  Its
    purpose is to be the single place that knows the ``{prefix}├─ `` /
    ``{prefix}└─ `` string shapes so server.py functions no longer hard-code
    them.  AC-B1-4 verifies that the literal ``"│   "`` strings disappear from
    the two refactored function bodies in ``server.py``.
    """
    if not rows:
        return []
    last_idx = len(rows) - 1
    result: list[str] = []
    for i, row in enumerate(rows):
        connector = "└─" if i == last_idx else "├─"
        result.append(f"{prefix}{connector} {row}")
    return result
