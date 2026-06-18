# SPDX-License-Identifier: AGPL-3.0-or-later
# src/mcp/test_query.py
"""Cypher builders for the 6 test-surface MCP tools (WI-4).

All queries use flat per-hop OPTIONAL MATCH + collect(DISTINCT ...) — no VLP
*1..N, no CALL { WITH } (ADR-0048, Neo4j 5.x deprecation).  Mirrors the style
of src/mcp/orm_queries.py.

Each builder returns (cypher_string, params_dict) so callers pass them directly
to session.run(**params) under _bounded().

H1 multi-tenant fail-closed (ADR-0034): every builder that reads user-visible
test nodes accepts a ``scope_pred`` callable - the SSOT ``server._scope_pred``
that emits the canonical
``($own IS NULL OR (size(a.profile)>0 AND all(__p IN a.profile WHERE __p IN $own
OR __p IN $shared)))`` fragment. The caller binds the matching ``$own``/``$shared``
params via ``server._scope(profile_name)``. The previous ``profile IS NOT NULL``
fail-OPEN predicate leaked every tenant's private test surface to any API key.
"""
from collections.abc import Callable

ScopePred = Callable[[str], str]


def _default_scope_pred(alias: str) -> str:
    """Fallback fail-CLOSED predicate when no server SSOT pred is supplied.

    Mirrors server._scope_pred byte-for-byte. Callers SHOULD pass the real
    server._scope_pred so there is one source of truth; this default exists only
    so a builder used without a caller is still fail-closed, never the old
    fail-OPEN ``profile IS NOT NULL``.
    """
    return (
        f"($own IS NULL OR (size({alias}.profile) > 0 AND "
        f"all(__p IN {alias}.profile WHERE __p IN $own OR __p IN $shared)))"
    )


# ---------------------------------------------------------------------------
# find_test_examples - pgvector-side (no Cypher needed for ANN); Neo4j side
# is just the existing find_examples chunk_types filter.  No builder needed
# here (handled inline in test_tools._find_test_examples via _srv._find_examples).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# tests_covering
# ---------------------------------------------------------------------------

def build_tests_covering_query(
    model: str,
    odoo_version: str,
    field: str | None = None,
    method: str | None = None,
    scope_pred: ScopePred | None = None,
) -> tuple[str, dict]:
    """Return (cypher, params) listing TestMethods that reference model/field/method.

    Coverage is static reference coverage (COVERS_MODEL / COVERS_FIELD /
    COVERS_FIELD edge traversal), not runtime executed coverage.

    Flat per-hop OPTIONAL MATCH — no VLP (ADR-0048).

    scope_pred: ADR-0034 fail-closed tenant choke for the ``tm`` alias (H1). The
    caller binds the matching ``$own``/``$shared`` params via ``server._scope``.
    """
    _pred = (scope_pred or _default_scope_pred)("tm")
    params: dict = {
        "model": model,
        "version": odoo_version,
    }

    if field:
        params["field"] = field
        cypher = f"""
MATCH (tm:TestMethod {{odoo_version: $version}})
MATCH (tm)-[:COVERS_FIELD]->(f:Field {{name: $field, model: $model, odoo_version: $version}})
WHERE {_pred}
OPTIONAL MATCH (tm)-[:BELONGS_TO_TEST]->(tc:TestClass {{odoo_version: $version}})
RETURN
    tm.name          AS method_name,
    tm.test_class    AS class_name,
    tm.module        AS module,
    tm.file_path     AS file_path,
    tm.line          AS line,
    tm.asserts_count AS asserts_count,
    tm.via           AS via,
    tc.test_type     AS test_type,
    tc.commit_allowed AS commit_allowed
ORDER BY
    CASE tm.via WHEN 'assert' THEN 0 WHEN 'body' THEN 1 ELSE 2 END,
    tm.asserts_count DESC,
    tm.module ASC,
    tm.name ASC
"""
    elif method:
        params["method_name"] = method
        cypher = f"""
MATCH (tm:TestMethod {{odoo_version: $version}})
MATCH (tm)-[:COVERS_METHOD]->(md:Method
    {{name: $method_name, model: $model, odoo_version: $version}})
WHERE {_pred}
OPTIONAL MATCH (tm)-[:BELONGS_TO_TEST]->(tc:TestClass {{odoo_version: $version}})
RETURN
    tm.name          AS method_name,
    tm.test_class    AS class_name,
    tm.module        AS module,
    tm.file_path     AS file_path,
    tm.line          AS line,
    tm.asserts_count AS asserts_count,
    tm.via           AS via,
    tc.test_type     AS test_type,
    tc.commit_allowed AS commit_allowed
ORDER BY
    CASE tm.via WHEN 'assert' THEN 0 WHEN 'body' THEN 1 ELSE 2 END,
    tm.asserts_count DESC,
    tm.module ASC,
    tm.name ASC
"""
    else:
        # Model-level coverage: any TestMethod with COVERS_MODEL edge.
        cypher = f"""
MATCH (tm:TestMethod {{odoo_version: $version}})
MATCH (tm)-[:COVERS_MODEL]->(md:Model {{name: $model, odoo_version: $version, is_definition: true}})
WHERE {_pred}
OPTIONAL MATCH (tm)-[:BELONGS_TO_TEST]->(tc:TestClass {{odoo_version: $version}})
RETURN
    tm.name          AS method_name,
    tm.test_class    AS class_name,
    tm.module        AS module,
    tm.file_path     AS file_path,
    tm.line          AS line,
    tm.asserts_count AS asserts_count,
    tm.via           AS via,
    tc.test_type     AS test_type,
    tc.commit_allowed AS commit_allowed
ORDER BY
    CASE tm.via WHEN 'assert' THEN 0 WHEN 'body' THEN 1 ELSE 2 END,
    tm.asserts_count DESC,
    tm.module ASC,
    tm.name ASC
"""

    return cypher.strip(), params


# ---------------------------------------------------------------------------
# test_class_inspect
# ---------------------------------------------------------------------------

def build_test_class_inspect_query(
    name: str,
    odoo_version: str,
    module: str | None = None,
    file_path: str | None = None,
    scope_pred: ScopePred | None = None,
) -> tuple[str, dict]:
    """Return (cypher, params) for a TestClass or TestHelper node lookup.

    Finds the class node with base chain (INHERITS_TEST), methods (BELONGS_TO_TEST),
    and who subclasses it (reverse INHERITS_TEST).

    Flat per-hop OPTIONAL MATCH — no VLP (ADR-0048).

    scope_pred: ADR-0034 fail-closed tenant choke (H1) applied to BOTH the
    TestClass (``tc``) and TestHelper (``th``) candidate nodes, so a tenant can
    never inspect another tenant's private test class. Framework helpers carry
    the shared/global profile, so they remain visible to every scoped tenant.
    """
    _sp = scope_pred or _default_scope_pred
    params: dict = {"name": name, "version": odoo_version}
    module_pred = ""
    file_pred = ""
    if module:
        params["module"] = module
        module_pred = "AND tc.module = $module"
    if file_path:
        params["file_path"] = file_path
        file_pred = "AND tc.file_path = $file_path"

    cypher = f"""
// Try TestClass first, then TestHelper
OPTIONAL MATCH (tc:TestClass {{name: $name, odoo_version: $version}})
WHERE tc IS NOT NULL AND {_sp("tc")} {module_pred} {file_pred}
WITH tc
ORDER BY tc.module ASC
LIMIT 1
// Framework helpers (origin='framework') are PUBLIC Odoo source (like CoreSymbol)
// and bypass the per-tenant choke; addon-promoted helpers stay scoped (H1).
OPTIONAL MATCH (th:TestHelper {{name: $name, odoo_version: $version}})
WHERE th.origin = 'framework' OR {_sp("th")}
WITH coalesce(tc, th) AS node
WHERE node IS NOT NULL

// Collect base chain (one hop, flat)
OPTIONAL MATCH (node)-[:INHERITS_TEST]->(base1:TestHelper)
  WHERE base1.odoo_version = $version
WITH node, collect(DISTINCT base1.name) AS helper_bases

OPTIONAL MATCH (node)-[:INHERITS_TEST]->(base2:TestClass)
  WHERE base2.odoo_version = $version
WITH node, helper_bases, collect(DISTINCT base2.name) AS class_bases

// Methods belonging to this class
OPTIONAL MATCH (tm:TestMethod {{odoo_version: $version}})
  WHERE tm.test_class = node.name AND tm.module = node.module
WITH node, helper_bases, class_bases,
     collect(DISTINCT {{name: tm.name, line: tm.line, asserts: tm.asserts_count}}) AS methods_list

// Subclassed-by (reverse INHERITS_TEST)
OPTIONAL MATCH (child:TestClass {{odoo_version: $version}})-[:INHERITS_TEST]->(node)
WITH node, helper_bases, class_bases, methods_list,
     collect(DISTINCT {{name: child.name, module: child.module}}) AS children_list

OPTIONAL MATCH (child2:TestHelper {{odoo_version: $version}})-[:INHERITS_TEST]->(node)
WITH node, helper_bases, class_bases, methods_list, children_list,
     collect(DISTINCT {{name: child2.name, module: child2.module}}) AS helper_children

RETURN
    node.name               AS name,
    node.module             AS module,
    node.file_path          AS file_path,
    node.line               AS line,
    node.test_type          AS test_type,
    node.commit_allowed     AS commit_allowed,
    node.is_helper          AS is_helper,
    node.docstring          AS docstring,
    node.base_classes       AS base_classes,
    node.setup_summary      AS setup_summary,
    helper_bases + class_bases AS all_bases,
    methods_list            AS methods,
    children_list + helper_children AS subclassed_by
""".strip()

    return cypher, params


# ---------------------------------------------------------------------------
# test_base_classes
# ---------------------------------------------------------------------------

def build_test_base_classes_query(
    odoo_version: str,
    name: str | None = None,
) -> tuple[str, dict]:
    """Return (cypher, params) for framework TestHelper nodes at a version.

    When name is given, returns only that helper.  When None, returns all
    framework-origin helpers (origin='framework') sorted by name.

    Flat per-hop OPTIONAL MATCH — no VLP (ADR-0048).
    """
    params: dict = {"version": odoo_version}
    if name:
        params["name"] = name
        cypher = """
MATCH (th:TestHelper {name: $name, odoo_version: $version, origin: 'framework'})
OPTIONAL MATCH (th)-[:INHERITS_TEST]->(parent:TestHelper {odoo_version: $version})
RETURN
    th.name             AS name,
    th.test_type        AS test_type,
    th.commit_allowed   AS commit_allowed,
    th.setup_summary    AS setup_summary,
    th.file_path        AS file_path,
    th.line             AS line,
    collect(DISTINCT parent.name) AS parent_bases
ORDER BY th.name ASC
"""
    else:
        cypher = """
MATCH (th:TestHelper {origin: 'framework', odoo_version: $version})
OPTIONAL MATCH (th)-[:INHERITS_TEST]->(parent:TestHelper {odoo_version: $version})
RETURN
    th.name             AS name,
    th.test_type        AS test_type,
    th.commit_allowed   AS commit_allowed,
    th.setup_summary    AS setup_summary,
    th.file_path        AS file_path,
    th.line             AS line,
    collect(DISTINCT parent.name) AS parent_bases
ORDER BY th.name ASC
"""

    return cypher.strip(), params


# ---------------------------------------------------------------------------
# test_coverage_audit
# ---------------------------------------------------------------------------

def build_coverage_audit_query(
    module: str,
    odoo_version: str,
    model: str | None = None,
    scope_pred: ScopePred | None = None,
) -> tuple[str, dict]:
    """Return (cypher, params) for field/method coverage audit in a module.

    Lists Field nodes in the module (optionally scoped to one model) with
    zero inbound COVERS_FIELD edges (unreferenced fields).

    Flat per-hop OPTIONAL MATCH — no VLP (ADR-0048).

    scope_pred: ADR-0034 fail-closed tenant choke on the ``f`` (Field) alias (H1).
    Only the COVERS_FIELD edge sources that pass the choke count as coverage, so a
    tenant's audit is computed purely from its own visible Field + test surface.
    """
    _sp = scope_pred or _default_scope_pred
    params: dict = {"module": module, "version": odoo_version}
    model_filter = ""
    if model:
        params["model"] = model
        model_filter = "AND f.model = $model"

    # Fields with zero inbound COVERS_FIELD edges from a VISIBLE TestMethod.
    cypher = f"""
MATCH (f:Field {{module: $module, odoo_version: $version}})
WHERE {_sp("f")} {model_filter}
OPTIONAL MATCH (tm:TestMethod {{odoo_version: $version}})-[:COVERS_FIELD]->(f)
  WHERE {_sp("tm")}
WITH f, count(tm) AS coverage_count
WHERE coverage_count = 0
RETURN
    f.name      AS field_name,
    f.model     AS model,
    f.ttype     AS ttype,
    f.module    AS module
ORDER BY f.model ASC, f.name ASC
LIMIT 200
""".strip()

    return cypher, params


def build_coverage_audit_total_query(
    module: str,
    odoo_version: str,
    model: str | None = None,
    scope_pred: ScopePred | None = None,
) -> tuple[str, dict]:
    """Return (cypher, params) for counting total VISIBLE fields in a module.

    scope_pred: ADR-0034 fail-closed tenant choke on ``f`` (H1) so the denominator
    of the coverage percentage matches the visible set used by the unreferenced query.
    """
    _sp = scope_pred or _default_scope_pred
    params: dict = {"module": module, "version": odoo_version}
    model_filter = ""
    if model:
        params["model"] = model
        model_filter = "AND f.model = $model"
    cypher = f"""
MATCH (f:Field {{module: $module, odoo_version: $version}})
WHERE {_sp("f")} {model_filter}
RETURN count(f) AS total_count
""".strip()
    return cypher, params


# ---------------------------------------------------------------------------
# js_test_inspect
# ---------------------------------------------------------------------------

def build_js_test_inspect_query(
    module: str,
    odoo_version: str,
    framework: str | None = None,
    scope_pred: ScopePred | None = None,
) -> tuple[str, dict]:
    """Return (cypher, params) for JsTestSuite nodes in a module.

    Optionally filter by framework ('hoot', 'qunit', 'tour').

    Flat per-hop OPTIONAL MATCH — no VLP (ADR-0048).

    scope_pred: ADR-0034 fail-closed tenant choke on ``js`` (H1) - a tenant's
    private frontend test suites must not leak to another key.
    """
    _sp = scope_pred or _default_scope_pred
    params: dict = {"module": module, "version": odoo_version}
    fw_pred = ""
    if framework:
        params["framework"] = framework
        fw_pred = "AND js.framework = $framework"

    cypher = f"""
MATCH (js:JsTestSuite {{module: $module, odoo_version: $version}})
WHERE js.file_path IS NOT NULL AND {_sp("js")} {fw_pred}
RETURN
    js.file_path        AS file_path,
    js.framework        AS framework,
    js.describe_blocks  AS describe_blocks,
    js.test_names       AS test_names,
    js.tags             AS tags,
    js.mounts           AS mounts,
    js.mock_models      AS mock_models,
    js.line             AS line
ORDER BY js.framework ASC, js.file_path ASC
""".strip()

    return cypher, params
