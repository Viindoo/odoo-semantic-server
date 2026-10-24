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


def format_view_conditions(
    conditions_raw: str | None, connector: str, sub_indent: str
) -> list[str]:
    """Render a View node's conditional-visibility blob into tree lines (GAP-1).

    *conditions_raw* is the JSON string stored on ``View.conditions`` by
    ``writer_neo4j_ui`` - a list of ``{element, attr, expr, field, legacy}`` maps
    covering both the legacy ``attrs=``/``states=`` form (v8-v16) and the v17+
    direct ``invisible=``/``required=``/``readonly=``/``column_invisible=`` form.

    Returns the rendered lines (header + one line per condition) using the caller's
    ``connector`` (the tree ASCII branch glyph) and ``sub_indent``. Returns ``[]``
    when there are no conditions or the blob is missing/malformed (so the caller
    appends nothing) - keeps the empty-section-silently-skipped contract
    (ADR-0023 §1.6).
    """
    if not conditions_raw:
        return []
    import json

    try:
        conds = json.loads(conditions_raw)
    except (ValueError, TypeError):
        return []
    if not conds:
        return []

    lines = [f"{connector} Conditional visibility ({len(conds)}):"]
    last_c = len(conds) - 1
    for j, c in enumerate(conds):
        cconn = "└─" if j == last_c else "├─"
        fld = c.get("field")
        target = f"{c.get('element', '?')}[{fld}]" if fld else c.get("element", "?")
        tag = " (legacy attrs/states)" if c.get("legacy") else ""
        lines.append(
            f"{sub_indent}{cconn} {target} {c.get('attr', '?')}="
            f"{c.get('expr', '')!r}{tag}"
        )
    return lines


# ---------------------------------------------------------------------------
# entity_lookup(kind='report') — Report (ir.actions.report) listing (GAP-2/GAP-5)
# ---------------------------------------------------------------------------
#
# Implemented HERE (not in server.py) because server.py is at its 3700-line
# god-file ceiling (test_no_god_file). Uses the same srv.* read helpers every
# other inspect path uses (_get_driver / _resolve_version / _data_bounded /
# _scope / _scope_pred). Routed through srv._data_bounded so a tx-timeout becomes
# OrmQueryTimeout (clean English, no Cypher leaked) — the raise propagates to the
# entity_lookup async handler which records the metric + returns the clean string.

def list_reports(
    *,
    model: str | None,
    name: str | None,
    odoo_version: str = "auto",
    profile_name: str | None = None,
) -> str:
    """List :Report nodes (ir.actions.report) matching a model and/or name.

    At least one of *model* (business model the report runs on) or *name* (xmlid /
    title substring) must be given. Renders an ADR-0023 §1 tree: one row per report
    with its report_type, target model, and the QWeb template it uses.
    """
    from src.constants import LIST_PREVIEW_MAX_ITEMS
    from src.mcp import server as srv

    if not model and not name:
        return (
            "Error: entity_lookup(kind='report') requires model='<model_name>' "
            "(reports on a model) and/or name='<report_xmlid_or_title>'."
        )

    with srv._get_driver().session() as session:
        odoo_version = srv._resolve_version(odoo_version, session)

        where = [srv._scope_pred("rp"), "rp.module <> '__unresolved__'"]
        params: dict = {"v": odoo_version, **srv._scope(profile_name)}
        if model:
            where.append("rp.model = $model")
            params["model"] = model
        if name:
            # Case-insensitive substring match on either the xmlid or the title.
            where.append(
                "(toLower(rp.xmlid) CONTAINS toLower($name) "
                "OR toLower(rp.name) CONTAINS toLower($name))"
            )
            params["name"] = name
        where_clause = " AND ".join(where)

        # ADR-0023 list convention (mirror _find_deprecated_usage / entity_lookup):
        # fetch cap+1 so a full page reveals whether more rows exist, then trim and
        # append a truncation banner — never a silent hard cap.
        cap_plus_one = LIST_PREVIEW_MAX_ITEMS + 1
        params["cap_plus_one"] = cap_plus_one
        rows = srv._data_bounded(
            session,
            f"""
            MATCH (rp:Report {{odoo_version: $v}})
            WHERE {where_clause}
            OPTIONAL MATCH (rp)-[:DEFINED_IN]->(mod:Module)
            RETURN rp.xmlid AS xmlid, rp.name AS name, rp.model AS model,
                   rp.report_type AS report_type, rp.report_name AS report_name,
                   rp.paperformat AS paperformat,
                   coalesce(mod.repo_url, mod.repo) AS repo, mod.name AS module
            ORDER BY rp.xmlid ASC
            LIMIT $cap_plus_one
            """,
            f"report list (model={model!r}, name={name!r}, Odoo {odoo_version})",
            **params,
        )

    overflow = len(rows) > LIST_PREVIEW_MAX_ITEMS
    if overflow:
        rows = rows[:LIST_PREVIEW_MAX_ITEMS]

    scope = []
    if model:
        scope.append(f"model={model!r}")
    if name:
        scope.append(f"name={name!r}")
    header = (
        f"entity_lookup(kind='report', {', '.join(scope)}, "
        f"odoo_version={odoo_version!r})"
    )
    if not rows:
        return (
            f"{header}\n"
            "└─ No reports found. Verify the model name / report xmlid, or the "
            "version (reports are indexed from ir.actions.report records and the "
            "v8-v13 <report> shorthand)."
        )

    lines = [header, f"├─ Reports ({len(rows)}):"]
    # When overflow, the truncation banner is the tree's last sibling, so no
    # report row may close the tree with "└─".
    last = -1 if overflow else len(rows) - 1
    for i, r in enumerate(rows):
        conn = "└─" if i == last else "├─"
        # integration LOW-1: ADR-0023 §1.3 wants 4-char indent — the non-last
        # vertical-continuation prefix is "│   " (pipe + 3 spaces), not "│  ".
        sub = "    " if i == last else "│   "
        repo = f"[{r['repo']}] " if r.get("repo") else ""
        lines.append(f"{conn} {r['xmlid']}  ({r.get('report_type') or '?'})")
        title = r.get("name")
        if title:
            lines.append(f"{sub}├─ Title:    {title}")
        lines.append(f"{sub}├─ Model:    {r.get('model') or '?'}")
        lines.append(f"{sub}├─ Module:   {repo}{r.get('module') or '?'}")
        tmpl = r.get("report_name")
        pf = r.get("paperformat")
        if pf:
            lines.append(f"{sub}├─ Template: {tmpl or '(none)'}")
            lines.append(f"{sub}└─ Paperformat: {pf}")
        else:
            lines.append(f"{sub}└─ Template: {tmpl or '(none)'}")
    if overflow:
        cap = len(rows)  # == LIST_PREVIEW_MAX_ITEMS after the trim above
        lines.append(
            f"└─ ... showing first {cap} reports (more than {cap} total)"
            " — narrow with model=<model_name> and/or name=<xmlid_or_title>"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# entity_lookup(kind='view') / odoo://{version}/view/{xmlid} - QWeb templates
# ---------------------------------------------------------------------------
#
# Every QWeb template is a :QWebTmpl node, never a :View (parser_qweb owns both
# the <template> element and the <record model="ir.ui.view"> type=qweb form, and
# parser_xml._parse_record skips the latter so it is classified once). The two
# labels share one xmlid namespace per version (writer_neo4j_ui EXTENDS_TMPL
# matches either), so a view lookup that misses :View falls through to here.
# A record-form template is keyed by its website `key` and keeps the record id
# as `record_xmlid`; `inherit_id ref=` and ir.model.data name the record id, so
# the lookup also matches it (website_forum.default_faq has key
# website_forum.faq_accordion). Lives here, not in server.py, because server.py
# is at its god-file ceiling (test_no_god_file).

_QWEB_TMPL_BY_XMLID = """
    MATCH (t:QWebTmpl {{xmlid: $xmlid, odoo_version: $ver}})
    WHERE {scope}
      AND NOT coalesce(t.unresolved, false)
      AND t.module <> '__unresolved__'
    OPTIONAL MATCH (t)-[:DEFINED_IN]->(mod:Module)
    RETURN t, mod.name AS module_name, coalesce(mod.repo_url, mod.repo) AS repo
    ORDER BY t.xmlid ASC
    LIMIT 1
"""

# Tried only after the xmlid match failed, so a node whose own xmlid is the
# requested one always wins (same precedence as the EXTENDS_TMPL resolver).
_QWEB_TMPL_BY_RECORD_XMLID = """
    MATCH (t:QWebTmpl {{record_xmlid: $xmlid, odoo_version: $ver}})
    WHERE {scope}
      AND NOT coalesce(t.unresolved, false)
      AND t.module <> '__unresolved__'
    OPTIONAL MATCH (t)-[:DEFINED_IN]->(mod:Module)
    RETURN t, mod.name AS module_name, coalesce(mod.repo_url, mod.repo) AS repo
    ORDER BY t.xmlid ASC
    LIMIT 1
"""


def resolve_view_or_qweb_template(
    session, xmlid: str, odoo_version: str, profile_name: str | None = None,
) -> str:
    """Render the QWeb template *xmlid* names, or the view not-found line.

    Called by ``server._resolve_view`` when no :View matches *xmlid*. Matches a
    :QWebTmpl by its node ``xmlid`` (the ``<template id>`` or the record-form
    website ``key``), then by ``record_xmlid`` (the record-form record id). Every
    returned node passes the tenant choke-point predicate (ADR-0034). Reads go
    through ``srv._single_bounded`` / ``srv._data_bounded``, so a tx-timeout
    raises ``OrmQueryTimeout`` to the owning handler exactly like the View path.
    """
    from src.constants import LIST_PREVIEW_MAX_ITEMS, REL_USES_TEMPLATE
    from src.mcp import server as srv
    from src.mcp.hints import format_next_step

    scope = srv._scope(profile_name)
    label = f"QWeb template detail for '{xmlid}' (Odoo {odoo_version})"
    rec = None
    for text in (_QWEB_TMPL_BY_XMLID, _QWEB_TMPL_BY_RECORD_XMLID):
        rec = srv._single_bounded(
            session, text.format(scope=srv._scope_pred("t")), label,
            xmlid=xmlid, ver=odoo_version, **scope,
        )
        if rec:
            break
    if not rec:
        return f"View '{xmlid}' not found in Odoo {odoo_version}."

    t = rec["t"]
    node_xmlid = t.get("xmlid") or xmlid
    params = {"txmlid": node_xmlid, "ver": odoo_version, "cap": LIST_PREVIEW_MAX_ITEMS,
              **scope}

    # The base this template extends: a QWebTmpl, or a plain :View (cross-type
    # EXTENDS_TMPL). The returned node is gated, not only the child (WG-3t).
    parent = srv._single_bounded(
        session,
        f"""
        MATCH (t:QWebTmpl {{xmlid: $txmlid, odoo_version: $ver}})
              -[:EXTENDS_TMPL]->(base {{odoo_version: $ver}})
        WHERE (base:QWebTmpl OR base:View)
          AND NOT coalesce(base.unresolved, false)
          AND base.module <> '__unresolved__'
          AND {srv._scope_pred("base")}
        RETURN base.xmlid AS parent_xmlid
        ORDER BY base.xmlid ASC
        LIMIT 1
        """,
        f"parent template for '{node_xmlid}' (Odoo {odoo_version})",
        **params,
    )

    ext = srv._single_bounded(
        session,
        f"""
        MATCH (ext:QWebTmpl {{odoo_version: $ver}})-[:EXTENDS_TMPL]->
              (t:QWebTmpl {{xmlid: $txmlid, odoo_version: $ver}})
        WHERE NOT coalesce(ext.unresolved, false)
          AND {srv._scope_pred("ext")}
        OPTIONAL MATCH (ext)-[:DEFINED_IN]->(mod:Module)
        WITH ext, mod ORDER BY ext.xmlid ASC
        RETURN count(ext) AS total,
               collect({{xmlid: ext.xmlid, module: mod.name,
                         repo: coalesce(mod.repo_url, mod.repo)}})[..$cap] AS rows
        """,
        f"template extension chain for '{node_xmlid}' (Odoo {odoo_version})",
        **params,
    )

    reports = srv._single_bounded(
        session,
        f"""
        MATCH (rp:Report {{odoo_version: $ver}})-[:{REL_USES_TEMPLATE}]->
              (t:QWebTmpl {{xmlid: $txmlid, odoo_version: $ver}})
        WHERE rp.module <> '__unresolved__'
          AND {srv._scope_pred("rp")}
        WITH rp ORDER BY rp.xmlid ASC
        RETURN count(rp) AS total, collect(rp.xmlid)[..$cap] AS rows
        """,
        f"reports using template '{node_xmlid}' (Odoo {odoo_version})",
        **params,
    )

    root = TreeNode(f"{xmlid} (Odoo {odoo_version})", is_root=True)
    root.add("Type:   qweb")
    if node_xmlid != xmlid:
        # Looked up by the record id: name the template t-call / website use.
        root.add(f"Template key: {node_xmlid} (the name t-call uses)")
    key = t.get("key")
    if key and key != node_xmlid:
        root.add(f"Key:    {key}")
    record_xmlid = t.get("record_xmlid")
    if record_xmlid and record_xmlid != xmlid:
        root.add(f"Record xmlid: {record_xmlid} (the name inherit_id ref= uses)")
    module_name = rec.get("module_name") or t.get("module") or "?"
    repo_str = f"[{rec['repo']}] " if rec.get("repo") else ""
    mode_str = f" ({t['mode']})" if t.get("mode") else ""
    root.add(f"Module: {repo_str}{module_name}{mode_str}")
    if parent and parent.get("parent_xmlid"):
        root.add(f"Inherits from: {parent['parent_xmlid']}")

    ext_total = (ext or {}).get("total") or 0
    if ext_total:
        node = root.add(f"Extended by ({ext_total}):")
        for row in srv._render_capped(
            (ext or {}).get("rows") or [],
            lambda r: (
                f"{r['xmlid']}  →  "
                f"{'[' + r['repo'] + '] ' if r.get('repo') else ''}{r.get('module') or '?'}"
            ),
            cap=LIST_PREVIEW_MAX_ITEMS,
            total=ext_total,
            more_hint=(
                f"find_examples(query='{node_xmlid} inherit_id'"
                f", odoo_version='{odoo_version}') for the rest"
            ),
        ):
            node.add(row)

    rep_total = (reports or {}).get("total") or 0
    if rep_total:
        node = root.add(f"Used by reports ({rep_total}):")
        for row in srv._render_capped(
            (reports or {}).get("rows") or [],
            str,
            cap=LIST_PREVIEW_MAX_ITEMS,
            total=rep_total,
            more_hint=(
                f"entity_lookup(kind='report', name='{module_name}'"
                f", odoo_version='{odoo_version}')"
            ),
        ):
            node.add(row)

    # format_next_step owns the footer text; TreeNode supplies the connector.
    root.add(format_next_step([
        f"module_inspect(name='{module_name}', method='qweb'"
        f", odoo_version='{odoo_version}') for sibling templates",
        f"find_examples(query='{node_xmlid} t-call', odoo_version='{odoo_version}')"
        " for usages",
    ]).removeprefix("└─ "))
    return root.render()
