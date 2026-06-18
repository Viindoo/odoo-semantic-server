# SPDX-License-Identifier: AGPL-3.0-or-later
"""Test-surface MCP tools (WI-4).

Six new tools exposing the OSM test index over MCP:
  - ``find_test_examples``   — semantic search over test chunks only.
  - ``tests_covering``       — COVERS_* edge traversal for a model/field/method.
  - ``test_class_inspect``   — full TestClass/TestHelper inspection tree.
  - ``test_base_classes``    — framework base-class menu + cursor contract.
  - ``test_coverage_audit``  — fields/methods with zero COVERS edge (static).
  - ``js_test_inspect``      — JsTestSuite map for a module (Hoot/QUnit/tour).

All tools:
- emit ADR-0023 tree grammar output, English-only.
- end with ``└─ Next: ...`` footer (ADR-0023 §4).
- accept ``odoo_version`` with active-version default (ADR-0029).
- are registered via import-time ``@mcp.tool()`` side effect; server.py imports
  this module at the END of the file (MED-2: NOT tools/__init__.py which is
  comment-only).

Implementation helpers (``_find_test_examples`` etc.) live HERE and are
imported by tests directly (FastMCP wraps public tools as FunctionTool —
not directly callable from tests).

All DB/Neo4j reads go through the server hub (``_srv.<name>``) accessed at
call time so ``monkeypatch.setattr(srv, ...)`` still works in tests.

Concurrent-subagent note: when running multiple subagents in parallel, each
subagent should pass ``odoo_version`` EXPLICITLY (e.g. ``odoo_version='17.0'``)
rather than relying on ``'auto'``.  The active-version session state is
per-API-key-per-session; concurrent subagents sharing the same API key may race
on the TTL cache — explicit version eliminates that race.
"""

import sys

from src.constants import VALID_CHUNK_TYPES
from src.mcp.hints import format_next_step
from src.mcp.server import (
    READONLY_TOOL_KWARGS,
    RequiredOdooVersion,
    mcp,
    offload_neo4j,
)
from src.mcp.test_query import (
    build_coverage_audit_query,
    build_coverage_audit_total_query,
    build_js_test_inspect_query,
    build_test_base_classes_query,
    build_test_class_inspect_query,
    build_tests_covering_query,
)

# Test chunk types (WI-1/WI-3, in VALID_CHUNK_TYPES via constants.py).
_TEST_CHUNK_TYPES = [t for t in ("test_method", "test_class", "js_test") if t in VALID_CHUNK_TYPES]

# Fallback to list literal if constants not yet updated (resilience).
if not _TEST_CHUNK_TYPES:
    _TEST_CHUNK_TYPES = ["test_method", "test_class", "js_test"]

_COMMIT_FORBIDDEN_MSG = "cr.commit() FORBIDDEN — isolation is savepoint rollback"

# ---------------------------------------------------------------------------
# Tool 1: find_test_examples
# ---------------------------------------------------------------------------


def _find_test_examples(
    query: str,
    odoo_version: str = "auto",
    model: str | None = None,
    kind: str | None = None,
    limit: int = 5,
    profile_name: str | None = None,
    *,
    _driver=None,
    _pg_conn=None,
    _embedder=None,
    _query_vec=None,
    _use_lexical: bool = False,
) -> str:
    """Semantic search restricted to test chunks (test_method, test_class, js_test).

    NEVER returns production method/field chunks.  Delegates to
    ``_srv._find_examples`` with ``chunk_types`` forced to test-only types,
    then applies optional post-filters.
    """
    if not query.strip():
        return (
            "find_test_examples: empty query — provide a description of the"
            " test pattern you want to find\nFound 0 results\n"
        )

    # Map kind alias to chunk_type filter
    chunk_types = list(_TEST_CHUNK_TYPES)
    if kind == "js":
        chunk_types = ["js_test"]
    elif kind in ("transaction", "http", "form", "savepoint"):
        # Backend test kinds — exclude js_test
        chunk_types = ["test_method", "test_class"]

    result = _srv._find_examples(
        query=query,
        odoo_version=odoo_version,
        limit=limit,
        context_module=None,
        chunk_types=chunk_types,
        profile_name=profile_name,
        _driver=_driver,
        _pg_conn=_pg_conn,
        _embedder=_embedder,
        _query_vec=_query_vec,
        _use_lexical=_use_lexical,
    )

    # Post-filter by model if requested: only keep results mentioning the model.
    if model and result:
        lines = result.splitlines()
        # Keep header line + only entries containing the model name.
        filtered = []
        inside_entry = False
        for line in lines:
            if line.startswith("Found") or line.startswith("find_test_examples"):
                filtered.append(line)
                continue
            if line.startswith("#") or line.startswith("─"):
                # Start of new entry block
                inside_entry = True
            if inside_entry:
                if model in line:
                    filtered.append(line)
        if filtered:
            result = "\n".join(filtered)

    # Ensure Next: footer references tests_covering
    if result and "Next:" not in result:
        next_line = format_next_step([
            f"tests_covering(model='{model or '<model>'}', odoo_version='{odoo_version}')"
            " to see which tests cover a model",
        ])
        result = result.rstrip() + "\n" + next_line

    return result


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload_neo4j
def find_test_examples(
    query: str,
    odoo_version: RequiredOdooVersion,
    model: str | None = None,
    kind: str | None = None,
    limit: int = 5,
    profile_name: str | None = None,
) -> str:
    """Semantic search over test chunks (test_method, test_class, js_test) only.

    TRIGGER when: "show me a test for X", "how is X tested", "give me a test
    example", "unit test example for", "viết test cho", "test mẫu cho",
    "how is amount_total tested", "example test for OWL component".
    PREFER over: find_examples when you specifically want test code, not
    production implementation code.
    SKIP when: you want production code examples — use find_examples instead.

    Returns ONLY test/js chunks, never production method/field chunks.
    Concurrent subagents: pass odoo_version explicitly to avoid session race.

    Args:
        query: Natural language description of the test pattern to find.
        odoo_version: Odoo version (e.g. '17.0'). 'auto' resolves to active.
        model: Optional model name filter (e.g. 'sale.order').
        kind: Optional test kind filter: 'transaction'|'http'|'form'|'js'.
        limit: Max results (default 5).
        profile_name: Optional profile scope.

    Example:
        find_test_examples("how is amount_total tested", odoo_version="17.0")
        -> [illustrative shape]
        Found 3 results  [test_method/test_class chunks only]
        #1 · score 0.86 · test_method · [sale] TestSaleOrder.test_amount_total_computed
        └─ Next: tests_covering(model='sale.order', odoo_version='17.0')
    """
    return _find_test_examples(
        query=query,
        odoo_version=odoo_version,
        model=model,
        kind=kind,
        limit=limit,
        profile_name=profile_name,
    )


# ---------------------------------------------------------------------------
# Tool 2: tests_covering
# ---------------------------------------------------------------------------


def _tests_covering(
    model: str,
    odoo_version: str = "auto",
    field: str | None = None,
    method: str | None = None,
    profile_name: str | None = None,
    *,
    _driver=None,
    _reraise_timeout: bool = False,
) -> str:
    """List TestMethods with COVERS_MODEL/FIELD/METHOD edges to the target.

    Static reference coverage — not executed/runtime coverage.
    """
    driver = _driver or _srv._get_driver()
    with driver.session() as session:
        v = _srv._resolve_version(odoo_version, session)

    # H1 (ADR-0034): thread the canonical fail-closed choke + bind $own/$shared.
    cypher, params = build_tests_covering_query(
        model, v, field=field, method=method, scope_pred=_srv._scope_pred,
    )
    params.update(_srv._scope(profile_name))

    with driver.session() as session:
        from src.mcp.orm import OrmQueryTimeout
        try:
            rows = _srv._data_bounded(session, cypher, f"tests_covering({model})", **params)
        except OrmQueryTimeout as exc:
            if _reraise_timeout:
                raise
            return _srv._nonorm_timeout_response(exc, "tests_covering")

    target_label = model
    if field:
        target_label = f"{model}.{field}"
    elif method:
        target_label = f"{model}.{method}()"

    header = f"tests_covering({target_label!r}, {v!r})"
    if not rows:
        next_line = format_next_step([
            f"find_test_examples(query='{model}', odoo_version='{v}')"
            " for semantic test search",
            f"test_coverage_audit(module='<module>', odoo_version='{v}')"
            " for full audit",
        ])
        return (
            f"{header}\n"
            f"├─ No test coverage edges found for {target_label!r} at Odoo {v}.\n"
            "│   Coverage = static reference edges (COVERS_*). Index may need a full run.\n"
            + next_line
        )

    # Group by via type
    assert_rows = [r for r in rows if r.get("via") == "assert"]
    setup_rows = [r for r in rows if r.get("via") in ("setup", None) and r not in assert_rows]
    body_rows = [r for r in rows if r.get("via") == "body"]
    # Remaining (dedupe)
    seen = {(r.get("method_name"), r.get("class_name"), r.get("module"))
            for r in assert_rows + setup_rows + body_rows}
    other_rows = [
        r for r in rows
        if (r.get("method_name"), r.get("class_name"), r.get("module")) not in seen
    ]

    lines = [header]

    def _row_line(r: dict, connector: str = "├─") -> str:
        cls = r.get("class_name") or "?"
        mth = r.get("method_name") or "?"
        mod = r.get("module") or "?"
        fp = r.get("file_path") or ""
        ln = r.get("line")
        asserts = r.get("asserts_count")
        via = r.get("via") or ""
        parts = [f"[{mod}] {cls}.{mth}"]
        if asserts:
            parts.append(f"asserts:{asserts}")
        if fp:
            loc = f"{fp}:{ln}" if ln else fp
            parts.append(loc)
        if via:
            parts.append(f"via:{via}")
        return f"   {connector} {' · '.join(parts)}"

    if assert_rows:
        lines.append(f"├─ Assert-coverage: {len(assert_rows)} test method(s)")
        _no_more = not setup_rows and not body_rows and not other_rows
        for i, r in enumerate(assert_rows[:8]):
            conn = "└─" if i == len(assert_rows) - 1 and _no_more else "├─"
            lines.append(_row_line(r, conn))

    if setup_rows:
        lines.append(f"├─ Setup/fixture-coverage: {len(setup_rows)} test method(s)")
        for i, r in enumerate(setup_rows[:5]):
            conn = "└─" if i == len(setup_rows) - 1 and not body_rows and not other_rows else "├─"
            lines.append(_row_line(r, conn))

    if body_rows:
        lines.append(f"├─ Body-coverage: {len(body_rows)} test method(s)")
        for i, r in enumerate(body_rows[:5]):
            conn = "└─" if i == len(body_rows) - 1 and not other_rows else "├─"
            lines.append(_row_line(r, conn))

    if other_rows:
        lines.append(f"├─ Other-coverage: {len(other_rows)} test method(s)")

    total = len(rows)
    lines.append(f"├─ Total: {total} test method(s) reference {target_label!r}")
    lines.append("│   Note: static reference coverage (COVERS_* edges), not executed coverage.")

    next_line = format_next_step([
        f"find_test_examples(query='{model}', odoo_version='{v}')"
        " for semantic test examples",
        f"test_coverage_audit(module='<module>', odoo_version='{v}')"
        " for gaps audit",
    ])
    lines.append(next_line)
    return "\n".join(lines)


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload_neo4j
def tests_covering(
    model: str,
    odoo_version: RequiredOdooVersion,
    field: str | None = None,
    method: str | None = None,
    profile_name: str | None = None,
) -> str:
    """List tests that reference a model, field, or method (COVERS_* edges).

    TRIGGER when: "which tests cover X", "what tests exist for sale.order",
    "is amount_total tested", "find tests for this field", "test nào test field
    này", "test coverage cho model X", "tests that use X".
    PREFER over: find_test_examples when you need a structured list of what
    tests reference a specific entity (not semantic similarity).
    SKIP when: you need a semantic example — use find_test_examples instead.

    Coverage = static reference edges (COVERS_*), NOT runtime executed coverage.
    Concurrent subagents: pass odoo_version explicitly to avoid session race.

    Args:
        model: Model name e.g. 'sale.order'. Required.
        odoo_version: Odoo version (e.g. '17.0'). 'auto' resolves to active.
        field: Optional field name to narrow to COVERS_FIELD edges.
        method: Optional method name to narrow to COVERS_METHOD edges.
        profile_name: Optional profile scope.

    Example:
        tests_covering(model='sale.order', field='amount_total', odoo_version='17.0')
        -> [illustrative shape]
        tests_covering('sale.order.amount_total', '17.0')
        ├─ Assert-coverage: 3 test method(s)
        └─ Next: find_test_examples(...)
    """
    return _tests_covering(
        model=model,
        odoo_version=odoo_version,
        field=field,
        method=method,
        profile_name=profile_name,
    )


# ---------------------------------------------------------------------------
# Tool 3: test_class_inspect
# ---------------------------------------------------------------------------


def _test_class_inspect(
    name: str,
    odoo_version: str = "auto",
    module: str | None = None,
    file_path: str | None = None,
    method: str = "summary",
    profile_name: str | None = None,
    *,
    _driver=None,
    _reraise_timeout: bool = False,
) -> str:
    """Inspect a TestClass or TestHelper: base chain, methods, subclassed-by."""
    driver = _driver or _srv._get_driver()
    with driver.session() as session:
        v = _srv._resolve_version(odoo_version, session)

    # H1 (ADR-0034): scope the TestClass + addon TestHelper candidates; framework
    # helpers bypass the choke inside the builder (public Odoo source).
    cypher, params = build_test_class_inspect_query(
        name, v, module=module, file_path=file_path, scope_pred=_srv._scope_pred,
    )
    params.update(_srv._scope(profile_name))

    with driver.session() as session:
        from src.mcp.orm import OrmQueryTimeout
        try:
            rows = _srv._data_bounded(
                session, cypher, f"test_class_inspect({name})", **params,
            )
        except OrmQueryTimeout as exc:
            if _reraise_timeout:
                raise
            return _srv._nonorm_timeout_response(exc, "test_class_inspect")

    if not rows:
        next_line = format_next_step([
            f"find_test_examples(query='{name}', odoo_version='{v}')"
            " for semantic search",
            f"test_base_classes(odoo_version='{v}') for framework base menu",
        ])
        return (
            f"test_class_inspect({name!r}, {v!r})\n"
            f"├─ Not found. Ensure the name is exact (case-sensitive).\n"
            + next_line
        )

    row = rows[0]
    node_name = row.get("name") or name
    node_module = row.get("module") or "?"
    node_file = row.get("file_path") or ""
    node_line = row.get("line")
    test_type = row.get("test_type") or "unknown"
    commit_allowed = row.get("commit_allowed")
    is_helper = row.get("is_helper")
    docstring = row.get("docstring")
    setup_summary = row.get("setup_summary") or []
    all_bases = row.get("all_bases") or []
    methods_list = row.get("methods") or []
    subclassed_by = row.get("subclassed_by") or []

    kind_tag = " [helper]" if is_helper else ""
    commit_str = "No" if not commit_allowed else "Yes (@standalone only)"
    loc_str = f"{node_file}:{node_line}" if node_line else node_file

    header = f"{node_name} (Odoo {v}){kind_tag}"
    lines = [header]

    if loc_str:
        lines.append(f"├─ Defined in:   [{node_module}] {loc_str}")
    else:
        lines.append(f"├─ Defined in:   [{node_module}]")

    lines.append(f"├─ test_type:    {test_type}   commit_allowed: {commit_str}")

    # Inheritance chain (summary display)
    if all_bases and method in ("summary", "hierarchy"):
        bases_str = " -> ".join(all_bases[:5])
        lines.append(f"├─ Inherits:     {bases_str}")

    # setUpClass fixtures
    if setup_summary and method in ("summary", "setup"):
        lines.append(f"├─ setUpClass:   creates {', '.join(setup_summary[:6])}")

    # Docstring (summary)
    if docstring and method == "summary":
        doc_short = docstring[:80].replace("\n", " ")
        lines.append(f"├─ Docstring:    {doc_short}")

    # Test methods
    test_methods = [m for m in methods_list if (m.get("name") or "").startswith("test_")]
    n_methods = len(test_methods)
    lines.append(f"├─ Test methods: {n_methods}")
    if method in ("summary", "methods") and test_methods:
        for i, m in enumerate(test_methods[:8]):
            conn = "└─" if i == len(test_methods) - 1 and not subclassed_by else "├─"
            mname = m.get("name", "?")
            asserts = m.get("asserts")
            mln = m.get("line")
            suffix = ""
            if asserts:
                suffix += f" (asserts:{asserts})"
            if mln:
                suffix += f" :{mln}"
            lines.append(f"│  {conn} {mname}{suffix}")

    # Subclassed-by
    if subclassed_by:
        lines.append(f"├─ Subclassed by: {len(subclassed_by)} test classes")
        for i, child in enumerate(subclassed_by[:6]):
            conn = "└─" if i == len(subclassed_by) - 1 else "├─"
            child_name = child.get("name") or "?"
            child_module = child.get("module") or "?"
            lines.append(f"│  {conn} [{child_module}] {child_name}")

    next_line = format_next_step([
        f"test_base_classes(odoo_version='{v}') for framework base semantics",
        f"tests_covering(model='<model>', odoo_version='{v}') for coverage",
    ])
    lines.append(next_line)
    return "\n".join(lines)


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload_neo4j
def test_class_inspect(
    name: str,
    odoo_version: RequiredOdooVersion,
    module: str | None = None,
    file_path: str | None = None,
    method: str = "summary",
    profile_name: str | None = None,
) -> str:
    """Inspect a test class or helper: base chain, setUp, methods, subclassed-by.

    TRIGGER when: "inspect TestSaleCommon", "what does AccountTestInvoicingCommon
    set up", "who inherits TestSaleCommon", "show test class hierarchy",
    "xem chuỗi kế thừa của test class", "TestClass có những method gì",
    "test class X thiết lập những gì".
    PREFER over: find_test_examples when you know the class name and want its
    full structure (not a semantic search).
    SKIP when: you need framework base classes — use test_base_classes instead.

    Concurrent subagents: pass odoo_version explicitly to avoid session race.

    Args:
        name: TestClass or TestHelper name (case-sensitive).
        odoo_version: Odoo version (e.g. '17.0'). 'auto' resolves to active.
        module: Optional module filter (restrict when same name in 2 modules).
        file_path: Optional file_path filter (restrict to one file).
        method: 'summary'|'hierarchy'|'methods'|'setup' (default 'summary').

    Example:
        test_class_inspect('AccountTestInvoicingCommon', odoo_version='17.0')
        -> [illustrative shape]
        AccountTestInvoicingCommon (Odoo 17.0) [helper]
        ├─ Defined in:   [account] tests/common.py:10
        ├─ test_type:    transaction   commit_allowed: No
        ├─ Subclassed by: 12 test classes
        └─ Next: test_base_classes(odoo_version='17.0')
    """
    return _test_class_inspect(
        name=name,
        odoo_version=odoo_version,
        module=module,
        file_path=file_path,
        method=method,
        profile_name=profile_name,
    )


# ---------------------------------------------------------------------------
# Tool 4: test_base_classes
# ---------------------------------------------------------------------------


def _test_base_classes(
    odoo_version: str = "auto",
    name: str | None = None,
    *,
    _driver=None,
) -> str:
    """Return framework base class menu + cursor contract for the version."""
    driver = _driver or _srv._get_driver()
    with driver.session() as session:
        v = _srv._resolve_version(odoo_version, session)

    cypher, params = build_test_base_classes_query(v, name=name)

    with driver.session() as session:
        from src.mcp.orm import OrmQueryTimeout
        try:
            rows = _srv._data_bounded(
                session, cypher, f"test_base_classes(Odoo {v})", **params,
            )
        except OrmQueryTimeout as exc:
            return _srv._nonorm_timeout_response(exc, "test_base_classes")

    # era1 note for v8/v9
    era1_note = ""
    try:
        major = int(v.split(".")[0])
    except (ValueError, AttributeError):
        major = 99
    if major in (8, 9):
        era1_note = (
            "\n│   Note: v8/v9 era1 - addon-level class hierarchy is regex best-effort."
        )

    if not rows and not era1_note:
        # Fallback: framework bases not yet seeded for this version.
        # Return static known info so tool is always useful.
        rows_fallback = _static_framework_bases(v)
        return _format_base_classes(rows_fallback, v) + era1_note

    if not rows:
        return _static_framework_bases_str(v) + era1_note

    return _format_base_classes(rows, v) + era1_note


def _format_base_classes(rows: list[dict], v: str) -> str:
    """Format TestHelper rows as ADR-0023 tree."""
    header = f"Odoo {v} - Test framework base classes (odoo/tests/)"
    lines = [header]

    for i, row in enumerate(rows):
        is_last = i == len(rows) - 1
        conn = "└─" if is_last else "├─"
        name = row.get("name") or "?"
        test_type = row.get("test_type") or "?"
        commit_allowed = row.get("commit_allowed")
        setup_notes = row.get("setup_summary") or []

        if not commit_allowed:
            commit_str = _COMMIT_FORBIDDEN_MSG
        else:
            commit_str = "cr.commit() allowed (@standalone only)"
        lines.append(f"{conn} {name}     {test_type} · {commit_str}")
        if setup_notes:
            indent = "    " if is_last else "│   "
            lines.append(f"{indent}└─ setup: {', '.join(setup_notes[:3])}")

    # Always append cursor rule (PP3 contract - must appear in output)
    lines.append(f"├─ Cursor rule:   {_COMMIT_FORBIDDEN_MSG}; isolation = savepoint rollback")

    next_line = format_next_step([
        f"suggest_pattern(intent='test computed field', odoo_version='{v}',"
        " category='test') for curated patterns",
        f"test_class_inspect(name='<ClassName>', odoo_version='{v}')"
        " to inspect one class",
    ])
    lines.append(next_line)
    return "\n".join(lines)


def _static_framework_bases(v: str) -> list[dict]:
    """Return static known framework bases when graph is not yet populated."""
    try:
        major = int(v.split(".")[0])
    except (ValueError, AttributeError):
        major = 17

    bases = [
        {"name": "TransactionCase", "test_type": "transaction",
         "commit_allowed": False,
         "setup_summary": ["savepoint-per-method", "auto-rollback"],
         "parent_bases": []},
        {"name": "HttpCase", "test_type": "http",
         "commit_allowed": False,
         "setup_summary": ["Chrome headless", "start_tour()"],
         "parent_bases": ["TransactionCase"]},
        {"name": "SingleTransactionCase", "test_type": "single_transaction",
         "commit_allowed": False,
         "setup_summary": ["one-txn", "no savepoint"],
         "parent_bases": ["TransactionCase"]},
        {"name": "Form", "test_type": "form",
         "commit_allowed": False,
         "setup_summary": ["server-side onchange"],
         "parent_bases": []},
    ]
    if major <= 15:
        bases.insert(1, {
            "name": "SavepointCase", "test_type": "savepoint",
            "commit_allowed": False,
            "setup_summary": ["legacy savepoint", "merged into TransactionCase v16+"],
            "parent_bases": ["TransactionCase"],
        })
    return bases


def _static_framework_bases_str(v: str) -> str:
    rows = _static_framework_bases(v)
    return _format_base_classes(rows, v)


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload_neo4j
def test_base_classes(
    odoo_version: RequiredOdooVersion,
    name: str | None = None,
) -> str:
    """Authoritative framework base-class menu + transaction/cursor contract.

    TRIGGER when: "what base class to use for testing", "TransactionCase vs
    SavepointCase", "can I use cr.commit() in a test", "test base class for
    HTTP", "nên dùng base class nào để test", "TransactionCase làm gì",
    "cursor contract khi viet test", "v17 test transaction semantics".
    PREFER over: test_class_inspect when you want the FRAMEWORK base menu
    (not an addon-level helper class).
    SKIP when: you know the class name and want its full structure — use
    test_class_inspect instead.

    Always states cr.commit() FORBIDDEN rule (PP3 contract).
    Concurrent subagents: pass odoo_version explicitly to avoid session race.

    Args:
        odoo_version: Odoo version (e.g. '17.0'). 'auto' resolves to active.
        name: Optional — drill into one base class by name.

    Example:
        test_base_classes(odoo_version='17.0')
        -> Odoo 17.0 - Test framework base classes (odoo/tests/)
        ├─ TransactionCase  transaction · cr.commit() FORBIDDEN ...
        ├─ HttpCase  http · cr.commit() FORBIDDEN ...
        ├─ Cursor rule: cr.commit() FORBIDDEN — isolation is savepoint rollback
        └─ Next: suggest_pattern(...)
    """
    return _test_base_classes(odoo_version=odoo_version, name=name)


# ---------------------------------------------------------------------------
# Tool 5: test_coverage_audit
# ---------------------------------------------------------------------------


def _test_coverage_audit(
    module: str,
    odoo_version: str = "auto",
    model: str | None = None,
    profile_name: str | None = None,
    *,
    _driver=None,
) -> str:
    """Audit fields in a module with zero COVERS_FIELD edges."""
    driver = _driver or _srv._get_driver()
    with driver.session() as session:
        v = _srv._resolve_version(odoo_version, session)

    # H1 (ADR-0034): scope both the unreferenced + total queries with the same
    # choke so numerator and denominator share one visible set, and bind params.
    _scope_params = _srv._scope(profile_name)
    cypher_unref, params_unref = build_coverage_audit_query(
        module, v, model=model, scope_pred=_srv._scope_pred,
    )
    params_unref.update(_scope_params)
    cypher_total, params_total = build_coverage_audit_total_query(
        module, v, model=model, scope_pred=_srv._scope_pred,
    )
    params_total.update(_scope_params)

    with driver.session() as session:
        from src.mcp.orm import OrmQueryTimeout
        try:
            unref_rows = _srv._data_bounded(
                session, cypher_unref, f"coverage_audit_unref({module})", **params_unref,
            )
            total_rows = _srv._data_bounded(
                session, cypher_total, f"coverage_audit_total({module})", **params_total,
            )
        except OrmQueryTimeout as exc:
            return _srv._nonorm_timeout_response(exc, "test_coverage_audit")

    total_count = total_rows[0]["total_count"] if total_rows else 0
    unref_count = len(unref_rows)
    ref_count = total_count - unref_count

    scope = f"{module}.{model}" if model else module
    header = f"Test coverage audit - {scope} (Odoo {v})   [static reference coverage]"
    lines = [header]

    if total_count == 0:
        lines.append(f"├─ No fields indexed for {scope} at Odoo {v}.")
        lines.append(
            "│   Run indexer to populate test coverage edges (reconcile_test_coverage)."
        )
    else:
        pct = int(ref_count * 100 / total_count) if total_count else 0
        lines.append(
            f"├─ {scope}: {ref_count}/{total_count} fields referenced by >=1 test ({pct}%)"
        )
        if unref_rows:
            # Group unreferenced by model
            by_model: dict[str, list[str]] = {}
            for r in unref_rows:
                m = r.get("model") or "?"
                by_model.setdefault(m, []).append(r.get("field_name") or "?")

            for mi, (m_name, fnames) in enumerate(by_model.items()):
                is_last_m = mi == len(by_model) - 1
                conn_m = "└─" if is_last_m and True else "├─"
                lines.append(f"│  {conn_m} {m_name}: {len(fnames)} unreferenced fields")
                display = fnames[:8]
                if len(fnames) > 8:
                    display = display[:7] + [f"... +{len(fnames) - 7} more"]
                for fi, fname in enumerate(display):
                    conn_f = "└─" if fi == len(display) - 1 else "├─"
                    lines.append(f"│      {conn_f} {fname}")

    lines.append(
        "├─ Caveat: 'referenced' = static self.env/self.<field> mention, not executed coverage."
    )

    next_line = format_next_step([
        f"find_test_examples(query='{module}', odoo_version='{v}')"
        " for test examples in this module",
        f"tests_covering(model='{model or '<model>'}', odoo_version='{v}')"
        " for coverage per model",
    ])
    lines.append(next_line)
    return "\n".join(lines)


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload_neo4j
def test_coverage_audit(
    module: str,
    odoo_version: RequiredOdooVersion,
    model: str | None = None,
    profile_name: str | None = None,
) -> str:
    """List fields/methods with zero test coverage (static COVERS_* edges).

    TRIGGER when: "what fields have no test coverage in sale", "which methods
    are untested", "coverage gaps in module X", "fields chưa có test trong
    module X", "audit test coverage", "what is not tested in Y".
    PREFER over: tests_covering when you want a module-wide GAP report, not
    a per-entity lookup.
    SKIP when: you want to see which tests exist for a model — use
    tests_covering instead.

    Concurrent subagents: pass odoo_version explicitly to avoid session race.

    Args:
        module: Module name (e.g. 'sale'). Required.
        odoo_version: Odoo version (e.g. '17.0'). 'auto' resolves to active.
        model: Optional model filter within the module.

    Example:
        test_coverage_audit(module='sale', odoo_version='17.0')
        -> [illustrative shape]
        Test coverage audit - sale (Odoo 17.0)  [static reference coverage]
        ├─ sale.order: 41/52 fields referenced (79%)
        ├─ Caveat: static mention, not executed coverage.
        └─ Next: find_test_examples(...)
    """
    return _test_coverage_audit(
        module=module, odoo_version=odoo_version, model=model, profile_name=profile_name,
    )


# ---------------------------------------------------------------------------
# Tool 6: js_test_inspect
# ---------------------------------------------------------------------------


def _js_test_inspect(
    module: str,
    odoo_version: str = "auto",
    framework: str | None = None,
    profile_name: str | None = None,
    *,
    _driver=None,
) -> str:
    """List JsTestSuite nodes for a module, optionally filtered by framework."""
    driver = _driver or _srv._get_driver()
    with driver.session() as session:
        v = _srv._resolve_version(odoo_version, session)

    # H1 (ADR-0034): scope JsTestSuite reads + bind $own/$shared.
    cypher, params = build_js_test_inspect_query(
        module, v, framework=framework, scope_pred=_srv._scope_pred,
    )
    params.update(_srv._scope(profile_name))

    with driver.session() as session:
        from src.mcp.orm import OrmQueryTimeout
        try:
            rows = _srv._data_bounded(
                session, cypher, f"js_test_inspect({module})", **params,
            )
        except OrmQueryTimeout as exc:
            return _srv._nonorm_timeout_response(exc, "js_test_inspect")

    fw_label = f" [{framework}]" if framework else ""
    header = f"Frontend tests - {module} (Odoo {v}){fw_label}"
    lines = [header]

    if not rows:
        lines.append(f"├─ No JS test suites indexed for [{module}] at Odoo {v}.")
        lines.append(
            "│   JS tests are indexed as JsTestSuite nodes (framework per file)."
        )
        next_line = format_next_step([
            f"find_test_examples(query='JS {module}', kind='js', odoo_version='{v}')"
            " for semantic JS test search",
        ])
        lines.append(next_line)
        return "\n".join(lines)

    # Count by framework
    fw_counts: dict[str, int] = {}
    for r in rows:
        fw = r.get("framework") or "unknown"
        fw_counts[fw] = fw_counts.get(fw, 0) + 1

    fw_summary = " · ".join(f"{fw} ({cnt} file{'s' if cnt > 1 else ''})"
                            for fw, cnt in sorted(fw_counts.items()))
    lines.append(f"├─ Framework mix:  {fw_summary}")

    # Tour suites first (surface separately)
    tour_rows = [r for r in rows if r.get("framework") == "tour"]
    other_rows = [r for r in rows if r.get("framework") != "tour"]

    if other_rows or tour_rows:
        lines.append("├─ Suites:")

    for i, r in enumerate(other_rows[:6]):
        conn = "└─" if i == len(other_rows) - 1 and not tour_rows else "├─"
        fp = r.get("file_path") or "?"
        fw = r.get("framework") or "?"
        tags = r.get("tags") or []
        test_names = r.get("test_names") or []
        n_tests = len(test_names)
        mounts = r.get("mounts") or []
        tag_str = f"  tags: {', '.join(tags[:3])}" if tags else ""
        mount_str = f"  mounts: {', '.join(mounts[:2])}" if mounts else ""
        n_label = f"{n_tests} test{'s' if n_tests != 1 else ''}"
        lines.append(f"│  {conn} [{fw}] {fp}{tag_str}  ({n_label}){mount_str}")

    if tour_rows:
        for i, r in enumerate(tour_rows[:3]):
            conn = "└─" if i == len(tour_rows) - 1 else "├─"
            fp = r.get("file_path") or "?"
            n_tests = len(r.get("test_names") or [])
            lines.append(f"│  {conn} [tour] {fp}  ({n_tests} tour{'s' if n_tests != 1 else ''})")

    # Sample test name from first non-tour suite
    sample_row = next((r for r in rows if r.get("framework") != "tour"), None)
    if sample_row:
        desc_blocks = sample_row.get("describe_blocks") or []
        test_names = sample_row.get("test_names") or []
        if desc_blocks and test_names:
            fw = sample_row.get("framework") or "?"
            sample = f'describe("{desc_blocks[0]}", () => test("{test_names[0]}", ...))'
            lines.append(f"├─ Sample ({fw}): {sample}")

    next_line = format_next_step([
        f"find_test_examples(query='{module} test', kind='js', odoo_version='{v}')"
        " for semantic JS test search",
        f"module_inspect(name='{module}', method='js', odoo_version='{v}')"
        " for JS patch list",
    ])
    lines.append(next_line)
    return "\n".join(lines)


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload_neo4j
def js_test_inspect(
    module: str,
    odoo_version: RequiredOdooVersion,
    framework: str | None = None,
    profile_name: str | None = None,
) -> str:
    """List frontend JS test suites (Hoot/QUnit/tour) in a module.

    TRIGGER when: "what Hoot tests exist in account", "JS tests for sale",
    "QUnit tests in web", "tour tests in point_of_sale", "frontend test files
    in module X", "Hoot test nào trong account v18", "JS test suite của module
    X", "OWL component test nao".
    PREFER over: find_test_examples when you want the full structural map of
    JS test files (framework, describe blocks, test names) in a module.
    SKIP when: you want semantic JS test examples — use find_test_examples with
    kind='js'.

    Note: JsTestSuite nodes are file-grained. No JS->Model coverage edges are
    emitted (Hoot v18+ uses hand-rolled mock models, not real ORM models).
    Concurrent subagents: pass odoo_version explicitly to avoid session race.

    Args:
        module: Module name (e.g. 'account'). Required.
        odoo_version: Odoo version (e.g. '18.0'). 'auto' resolves to active.
        framework: Optional filter: 'hoot'|'qunit'|'tour'.

    Example:
        js_test_inspect(module='account', odoo_version='18.0')
        -> [illustrative shape]
        Frontend tests - account (Odoo 18.0)
        ├─ Framework mix:  hoot (12 files) · tour (3 files)
        ├─ Suites: ...
        └─ Next: find_test_examples(...)
    """
    return _js_test_inspect(
        module=module, odoo_version=odoo_version, framework=framework,
        profile_name=profile_name,
    )


# ---------------------------------------------------------------------------
# Server reference (bound at module-load time like other tool modules)
# ---------------------------------------------------------------------------

_srv = sys.modules["src.mcp.server"]
