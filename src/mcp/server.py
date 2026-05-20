# SPDX-License-Identifier: AGPL-3.0-or-later
# src/mcp/server.py
import math
import os
import re
import threading
import warnings
from contextlib import asynccontextmanager, contextmanager, nullcontext

from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent
from neo4j import GraphDatabase
from starlette.requests import Request

from src.constants import (
    CODE_PREVIEW_MAX_CHARS,
    DEFAULT_EMBEDDER_MODEL,
    EDITION_PRIORITY,
    EDITION_PRIORITY_ELSE,
    FIND_EXAMPLES_ANN_LIMIT,
    IMPACT_RISK_HIGH_THRESHOLD,
    IMPACT_RISK_MED_THRESHOLD,
    LIST_PREVIEW_FIELDS_MAX,
    LIST_PREVIEW_MAX_ITEMS,
    LIST_PREVIEW_PATCHES_MAX,
    PG_POOL_MAX_CONN,
    PG_POOL_MIN_CONN,
    REL_DEPENDS_ON,
    REL_INHERITS,
    REL_INHERITS_VIEW,
    REL_TARGETS_MODEL,
    REL_USES_CORE_SYMBOL,
    SNIPPET_PREVIEW_MAX_LINES,
    VALID_CHUNK_TYPES,
)
from src.mcp import session as _session
from src.mcp.dto import (
    DescribeModuleOutput,
    FieldRef,
    ListFieldsOutput,
    ListMethodsOutput,
    MethodRef,
    ModelRef,
    ModuleRef,
    ResolveFieldOutput,
    ResolveMethodOutput,
    ResolveModelOutput,
    ResolveViewOutput,
    ViewRef,
)
from src.mcp.hints import (  # noqa: F401  (hints_for is re-exported for external consumers)
    format_next_step,
    hints_for,
)
from src.mcp.inspect import _entity_lookup, _model_inspect, _module_inspect
from src.mcp.refs import RefError, mint_refs, resolve_ref
from src.mcp.resources import register_resources
from src.mcp.tool_log_middleware import UsageLogMiddleware as _UsageLogMiddleware
from src.mcp.tree_builder import render_list_block

# Sentinel api_key_id for direct _impl calls (tests, CLI) — refs are scoped
# to this namespace and do not collide with production tenant refs.
_ANONYMOUS_API_KEY_ID = "anonymous"


def _edition_rank_cypher(node_alias: str = "mod") -> str:
    """Build Cypher CASE expression for edition priority from EDITION_PRIORITY."""
    cases = " ".join(
        f"WHEN '{ed}' THEN {rank}"
        for ed, rank in sorted(EDITION_PRIORITY.items(), key=lambda x: x[1])
    )
    return f"CASE {node_alias}.edition {cases} ELSE {EDITION_PRIORITY_ELSE} END AS edition_rank"


def _render_capped(
    items: list,
    formatter,  # Callable[[Any], str]
    cap: int = LIST_PREVIEW_MAX_ITEMS,
    total: int | None = None,
    more_hint: str | None = None,
) -> list[str]:
    """Format `items` via `formatter`, capped at `cap`, with total disclosure.

    Returns a list of formatted lines. When `total` (or len(items)) exceeds
    `cap`, appends a trailing "... and {N-cap} more (use {more_hint})" line.

    `total` defaults to len(items) — pass explicitly when caller has already
    sliced items (e.g., from a Cypher LIMIT). `more_hint` is the suggested
    tool invocation to retrieve the full list, e.g.
    "list_fields(model='sale.order', odoo_version='17.0') for full list".
    Required when total > cap; raises ValueError otherwise.
    """
    real_total = total if total is not None else len(items)
    lines = [formatter(it) for it in items[:cap]]
    if real_total > cap:
        if not more_hint:
            raise ValueError(
                f"_render_capped: more_hint required when total ({real_total}) > cap ({cap})"
            )
        lines.append(f"... and {real_total - cap} more (use {more_hint})")
    return lines


# `format_next_step` + `hints_for` relocated to src/mcp/hints.py per ADR-0023 §4
# SSOT (WI-A2). Imported below alongside the FastMCP setup.


mcp = FastMCP("odoo-semantic")
# Register 7 MCP resources (odoo:// URIs) — Pattern 8, Wave F.
register_resources(mcp)
# Register FastMCP-layer usage logging middleware so that on_call_tool has
# access to context.message.name (the real tool name) — see F5 fix in
# src/mcp/tool_log_middleware.py.
mcp.add_middleware(_UsageLogMiddleware())

# All 21 OSM tools are read-only queries against a statically-indexed graph.
# Annotations advertise this to MCP clients (Claude Code, Cursor, VS Code,
# ChatGPT) so they can auto-approve and skip confirmation gates.
# (cross-server pattern: read-only annotations for auto-approval)
READONLY_TOOL_KWARGS = {
    "annotations": {
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
        "destructiveHint": False,
    }
}

# Session-mutating tools (set_active_version, set_active_profile) — write session
# state but are idempotent and non-destructive.  readOnlyHint=False because they
# perform a DB write (UPSERT into api_key_session_state).
MUTATING_TOOL_KWARGS = {
    "annotations": {
        "readOnlyHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "destructiveHint": False,
    }
}

_driver = None
_embedder_instance = None
_version_checked = False
_init_lock = threading.Lock()  # guards _driver + _embedder_instance lazy init

# ---------------------------------------------------------------------------
# Dual-mode ref helpers (WI-C3) — resolve_ref or canonical parse
# ---------------------------------------------------------------------------

# Regex for opaque ref IDs minted by list_* tools: f1, m12, v3, x99, p5.
# Max length ≤ 6 chars (1 prefix + up to 5 digits) to avoid false positives
# on canonical names that happen to start with f/m/v/x/p.
_REF_PATTERN = re.compile(r"^[fmvxp]\d{1,5}$")


# ---------------------------------------------------------------------------
# Deprecation banner helper (WI-D4) — for 10 legacy tools redirecting to
# supersets (model_inspect, module_inspect, entity_lookup)
# ---------------------------------------------------------------------------


def _deprecation_banner(new_tool: str, args: str) -> str:
    """Return a deprecation banner for legacy tools.

    Args:
        new_tool: Name of the new superset tool (e.g. 'model_inspect').
        args: Example arguments (e.g. "model='X', method='summary'").

    Returns:
        A banner string formatted as:
            "DEPRECATED: use <new_tool>(<args>) instead. Will be removed in
             v0.6 — see ADR-0028.\n\n"
    """
    return (
        f"DEPRECATED: use {new_tool}({args}) instead. Will be removed in "
        "v0.6 — see ADR-0028.\n\n"
    )


def _looks_like_ref(s: str) -> bool:
    """Return True when *s* looks like a ref minted by list_* tools (e.g. 'f12')."""
    return bool(_REF_PATTERN.match(s))


def _get_api_key_id() -> str:
    """Return the API key ID for the current sync context.

    Sync MCP tool wrappers run outside Starlette request context, so we
    cannot extract the real API key from the request.  Return a stable
    sentinel so all sync callers share one ref namespace — per-call refs
    minted by list_* tools (which DO have request context) are stored under
    the real api_key_id, while resolve_* tools look up under the same key.
    In production, the middleware writes the api_key_id into a thread-local;
    fall back to 'default' when not set (unit tests, CLI invocations).
    """
    return getattr(_api_key_id_local, "value", "default")


# Thread-local storage for API key ID — populated by middleware when available.
_api_key_id_local = threading.local()


_STALE_REF_RECOVERY: dict[str, str] = {
    "model": (
        "describe_module(name=<module>, odoo_version='X')"
        " or find_examples(query='model name')"
    ),
    "field": "list_fields(model='X', odoo_version='Y') to re-mint field refs",
    "method": "list_methods(model='X', odoo_version='Y') to re-mint method refs",
    "view": "list_views(model='X', odoo_version='Y') to re-mint view refs",
}


def _format_stale_ref_error(entity: str, ref: str, err: RefError) -> str:
    """Return a friendly error string for a stale or unknown ref.

    The error is returned as a tree-formatted string matching the not-found
    convention of other resolve_* tools (plain text, no exception raised).
    """
    hint = (
        err.recovery_hint
        or _STALE_REF_RECOVERY.get(entity, f"Re-run the list_{entity}s(...) call that minted it.")
    )
    return (
        f"resolve_{entity}: Ref {ref!r} is unknown or expired.\n"
        f"└─ Recovery: {hint}"
    )

# find_examples rerank coefficients — extracted so calibration harness can
# monkey-patch them. See _find_examples + tests/test_calibration_eval.py.
_RERANK_LOG_COEFF = 0.02
_RERANK_CHAIN_BOOST = 0.20


def _get_driver():
    global _driver, _version_checked
    if _driver is not None:  # fast path — no lock overhead on hot calls
        return _driver
    with _init_lock:
        if _driver is not None:  # re-check after acquiring lock
            return _driver
        from src import config
        # Resolution order per from_env_or_ini: env var → INI → fallback
        uri = config.from_env_or_ini(
            "NEO4J_URI", "database", "neo4j_uri",
            fallback="bolt://localhost:7687",
        )
        user = config.from_env_or_ini(
            "NEO4J_USER", "database", "neo4j_user", fallback="neo4j",
        )
        password = config.from_env_or_ini(
            "NEO4J_PASSWORD", "database", "neo4j_password", fallback=None,
        )
        if not password:
            raise RuntimeError(
                "Neo4j password missing. Set NEO4J_PASSWORD env var OR "
                "neo4j_password in [database] section of odoo-semantic.conf."
            )
        _driver = GraphDatabase.driver(uri, auth=(user, password))

        # Version check: fail-fast if Neo4j < 5.x (unless in CI with pinned image).
        # _version_checked is protected by _init_lock here — no separate flag needed.
        if not _version_checked and os.getenv("CI") != "true":
            with _driver.session() as _s:
                _row = _s.run(
                    "CALL dbms.components() YIELD versions RETURN versions[0] AS v"
                ).single()
                if _row:
                    _v = str(_row["v"])
                    _major = int(_v.split(".")[0])
                    if _major < 5:
                        raise RuntimeError(
                            f"Neo4j 5.x+ required (found {_v}). "
                            f"Update docker-compose.yml NEO4J_IMAGE and re-run."
                        )
            _version_checked = True
    return _driver


def _ensure_pg() -> None:
    """Initialize centralized PG pool on first call. No-op if already initialized.

    Single-attempt with `connect_timeout` (default 5s) — fails fast on an
    unreachable PG instead of hanging. The lifespan handler is responsible
    for tolerating the failure (degraded mode + background retry) so the
    MCP server keeps serving /health even when the DB tier is down.
    """
    from src.db.pg import get_pool, init_pool
    try:
        get_pool()
    except RuntimeError:
        from src import config
        dsn = config.from_env_or_ini("PG_DSN", "database", "pg_dsn", fallback=None)
        if not dsn:
            raise RuntimeError(config.dsn_missing_hint())
        pg_pool_max = int(config.from_env_or_ini(
            "PG_POOL_MAX", "database", "pg_pool_max",
            fallback=str(PG_POOL_MAX_CONN),
        ))
        init_pool(dsn, min_conn=PG_POOL_MIN_CONN, max_conn=pg_pool_max)


@contextmanager
def _checkout_pg():
    """Check out a pooled PG connection with pgvector registered."""
    _ensure_pg()
    from src.db.pg import get_pool
    with get_pool().checkout_vec() as conn:
        yield conn


def _get_embedder():
    global _embedder_instance
    if _embedder_instance is not None:  # fast path — no lock overhead on hot calls
        return _embedder_instance
    with _init_lock:
        if _embedder_instance is not None:  # re-check after acquiring lock
            return _embedder_instance
        from src import config
        from src.indexer.embedder import Qwen3Embedder
        url = config.from_env_or_ini(
            "EMBEDDER_URL", "embedder", "url",
            fallback="http://localhost:11434",
        )
        model = config.from_env_or_ini(
            "EMBEDDER_MODEL", "embedder", "model",
            fallback=DEFAULT_EMBEDDER_MODEL,
        )
        dim_str = config.from_env_or_ini(
            "EMBEDDER_DIM", "embedder", "dim", fallback="1024",
        )
        auth_token = config.from_env_or_ini(
            "EMBEDDER_AUTH_TOKEN", "embedder", "auth_token", fallback=None,
        )
        _embedder_instance = Qwen3Embedder(
            url, model, dim=int(dim_str), auth_token=auth_token,
        )
    return _embedder_instance


def _latest_version(session) -> str | None:
    """Return the latest Odoo version present in the index, by NUMERIC compare.

    Filters:
      - excludes 'unknown' and any non-semver-shaped string (must match `\\d+\\.\\d+`)
      - sorts by `toInteger(split(v,'.')[0])` then minor — handles 9.0 < 17.0 correctly
        (lexicographic compare would put '9.0' > '17.0', a Neo4j 5.x gotcha — see
        project CLAUDE.md).

    Returns None when no indexed data exists (no hardcoded fallback). Callers
    should surface a clear error instructing the user to run the indexer.
    """
    rec = session.run("""
        MATCH (m:Module)
        WITH DISTINCT m.odoo_version AS v
        WHERE v <> 'unknown' AND v =~ '\\d+\\.\\d+'
        RETURN v
        ORDER BY toInteger(split(v, '.')[0]) DESC,
                 toInteger(split(v, '.')[1]) DESC
        LIMIT 1
    """).single()
    return rec["v"] if rec else None


def _resolve_version(version_arg: str, session) -> str:
    """Session-aware version resolution — 3-tier order per ADR-0029.

    Resolution order (delegated to session.resolve_version_v2):
      1. Explicit *version_arg* after sentinel normalization (auto/default/
         latest/version/any/"" all treated as sentinel → None).
      2. Per-API-key session state (api_key_session_state table, 24h TTL).
      3. Latest indexed version via _latest_version() Neo4j query.

    Raises ValueError when all three tiers fail (empty index + no session
    + no explicit version).

    All 24 existing call sites are unchanged — this function's external
    signature is preserved.
    """
    api_key_id = _get_api_key_id()
    return _session.resolve_version_v2(version_arg, api_key_id, session)


def _resolve_model(
    model_name: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
) -> str:
    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        # Ranking tiers — see docs/adr/0013:
        # T1 is_def_rank: m.is_definition flag (post-reindex, authoritative).
        # T2 field_count: Field nodes declared on this model in this module —
        #                 100% accurate signal pre-reindex on real data
        #                 (defining module always has the most fields).
        # T3 dependents : DEPENDS_ON inbound on Module (manifest depends).
        # T4 edition    : community < enterprise < viindoo < oca < custom.
        # T5 mod_name   : alphabetical tiebreak — eliminates arbitrary order.
        layers = session.run(
            f"""
            MATCH (m:Model {{name: $name, odoo_version: $v}})-[:DEFINED_IN]->(mod:Module)
            WHERE ($profile_name IS NULL OR $profile_name IN m.profile)
            WITH m, mod,
                 CASE WHEN coalesce(m.is_definition, false) THEN 0 ELSE 1 END AS is_def_rank,
                 COUNT {{
                     (:Field {{model: $name, module: m.module, odoo_version: $v}})
                 }} AS field_count,
                 COUNT {{ ()-[:{REL_DEPENDS_ON}]->(mod) }} AS dependents,
                 {_edition_rank_cypher("mod")},
                 mod.name AS mod_name
            RETURN m.module AS module_name, mod.repo AS repo,
                   COUNT {{ (:Field {{model: $name, odoo_version: $v}}) }} AS fields_count,
                   COUNT {{ (:Method {{model: $name, odoo_version: $v}}) }} AS methods_count
            ORDER BY is_def_rank ASC, field_count DESC, dependents DESC,
                     edition_rank ASC, mod_name ASC
            """,
            name=model_name, v=odoo_version, profile_name=profile_name,
        ).data()

        if not layers:
            return f"Model '{model_name}' not found in Odoo {odoo_version}."

        base = layers[0]
        extensions = layers[1:]
        fields_count = base["fields_count"]
        methods_count = base["methods_count"]

        # DISTINCT on p.name only — the same parent (e.g. mail.thread) is reachable
        # via multiple INHERITS edges (one per module that declares _inherit), and
        # each one resolves to a separate (parent_name, module) pair. Without
        # collapsing here the rendered list shows duplicates like
        # "mail.thread, mail.thread, mail.thread, ..." (M5 install audit).
        parents = session.run(f"""
            MATCH (:Model {{name: $name, odoo_version: $v}})-[r:{REL_INHERITS}]->(p:Model)
            WHERE p.name <> $name
              AND NOT coalesce(r.unresolved, false)
            RETURN DISTINCT p.name AS pname
            ORDER BY pname
        """, name=model_name, v=odoo_version).data()

    lines = [f"{model_name} (Odoo {odoo_version})"]
    lines.append(f"├─ Defined in:     [{base['repo']}] {base['module_name']}")

    if parents:
        parents_str = ", ".join(p["pname"] for p in parents)
        lines.append(f"├─ Inherits from:  {parents_str}")

    if extensions:
        lines.append("├─ Extended by:")
        more_hint = (
            f"list_fields(model='{model_name}', odoo_version='{odoo_version}')"
            " for full overview"
        )
        rendered = _render_capped(
            extensions,
            lambda ext: f"[{ext['repo']}] {ext['module_name']}",
            cap=LIST_PREVIEW_MAX_ITEMS,
            more_hint=more_hint,
        )
        lines.extend(render_list_block(rendered))

    lines.append(f"├─ Fields:         {fields_count}")
    lines.append(f"├─ Methods:        {methods_count}")
    lines.append(format_next_step([
        f"list_fields(model='{model_name}', odoo_version='{odoo_version}')"
        " for full field list",
        f"list_methods(model='{model_name}', odoo_version='{odoo_version}')"
        " for behavior",
    ]))
    return "\n".join(lines)


def _resolve_field(
    model_name: str,
    field_name: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
) -> str:
    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        # 5-tier ranking via m_node proxy — see docs/adr/0013
        records = session.run(f"""
            MATCH (f:Field {{name: $fn, model: $mn, odoo_version: $v}})
            WHERE ($profile_name IS NULL OR $profile_name IN f.profile)
            OPTIONAL MATCH (mod:Module {{name: f.module, odoo_version: $v}})
            OPTIONAL MATCH (m_node:Model {{name: $mn, module: f.module, odoo_version: $v}})
            WITH f, mod, m_node,
                 CASE WHEN coalesce(m_node.is_definition, false) THEN 0 ELSE 1 END
                      AS is_def_rank,
                 COUNT {{
                     (:Field {{model: $mn, module: f.module, odoo_version: $v}})
                 }} AS field_count,
                 COUNT {{ ()-[:{REL_DEPENDS_ON}]->(mod) }} AS dependents,
                 {_edition_rank_cypher("mod")},
                 mod.name AS mod_name
            RETURN f, f.module AS module_name, mod.repo AS repo
            ORDER BY is_def_rank ASC, field_count DESC, dependents DESC,
                     edition_rank ASC, mod_name ASC
        """, fn=field_name, mn=model_name, v=odoo_version, profile_name=profile_name).data()

    if not records:
        return (
            f"Field '{field_name}' not found on model"
            f" '{model_name}' in Odoo {odoo_version}."
        )

    base_f = records[0]["f"]
    lines = [
        f"{model_name}.{field_name} (Odoo {odoo_version})",
        f"├─ Type:     {base_f.get('ttype', '?')}",
        f"├─ Computed: {'Yes' if base_f.get('compute') else 'No'}"
        + (f" ({base_f['compute']})" if base_f.get('compute') else ""),
        f"├─ Stored:   {'Yes' if base_f.get('stored', True) else 'No'}",
        f"├─ Required: {'Yes' if base_f.get('required', False) else 'No'}",
        f"├─ Related:  {base_f.get('related') or '—'}",
        "├─ Declared in:",
    ]
    last_idx = len(records) - 1
    for i, r in enumerate(records):
        repo_str = f"[{r['repo']}] " if r.get("repo") else ""
        connector = "└─" if i == last_idx else "├─"
        lines.append(f"│   {connector} {repo_str}{r['module_name']}")
    lines.append(format_next_step([
        f"find_examples(query='{model_name}.{field_name} usage'"
        f", odoo_version='{odoo_version}') for real-world patterns",
        f"impact_analysis(entity_type='field'"
        f", entity_name='{model_name}.{field_name}'"
        f", odoo_version='{odoo_version}') for blast radius",
    ]))
    return "\n".join(lines)


def _resolve_method(
    model_name: str,
    method_name: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
) -> str:
    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        # 5-tier ranking via m_node proxy — see docs/adr/0013
        records = session.run(f"""
            MATCH (mth:Method {{name: $mn, model: $model, odoo_version: $v}})
            WHERE ($profile_name IS NULL OR $profile_name IN mth.profile)
            OPTIONAL MATCH (mod:Module {{name: mth.module, odoo_version: $v}})
            OPTIONAL MATCH (m_node:Model {{name: $model, module: mth.module, odoo_version: $v}})
            WITH mth, mod, m_node,
                 CASE WHEN coalesce(m_node.is_definition, false) THEN 0 ELSE 1 END
                      AS is_def_rank,
                 COUNT {{
                     (:Field {{model: $model, module: mth.module, odoo_version: $v}})
                 }} AS field_count,
                 COUNT {{ ()-[:{REL_DEPENDS_ON}]->(mod) }} AS dependents,
                 {_edition_rank_cypher("mod")},
                 mod.name AS mod_name
            RETURN mth, mth.module AS module_name, mod.repo AS repo
            ORDER BY is_def_rank ASC, field_count DESC, dependents DESC,
                     edition_rank ASC, mod_name ASC
        """, mn=method_name, model=model_name, v=odoo_version, profile_name=profile_name).data()

    if not records:
        return (
            f"Method '{method_name}' not found on model"
            f" '{model_name}' in Odoo {odoo_version}."
        )

    lines = [
        f"{model_name}.{method_name}() (Odoo {odoo_version})",
        f"├─ Override chain ({len(records)}):",
    ]
    last_idx = len(records) - 1
    for i, r in enumerate(records):
        mth = r["mth"]
        super_info = "✓ calls super()" if mth.get("has_super_call") else "✗ no super()"
        decs = ", ".join(mth.get("decorators") or []) or "—"
        repo_str = f"[{r['repo']}] " if r.get("repo") else ""
        connector = "└─" if i == last_idx else "├─"
        lines.append(
            f"│   {connector} {repo_str}{r['module_name']}"
            f" — {super_info} — decorators: {decs}"
        )
    lines.append(format_next_step([
        f"find_override_point(model='{model_name}', method='{method_name}'"
        f", odoo_version='{odoo_version}') for safe hook spot",
        f"impact_analysis(entity_type='method'"
        f", entity_name='{model_name}.{method_name}'"
        f", odoo_version='{odoo_version}') for blast radius",
    ]))
    return "\n".join(lines)


def _resolve_view(
    xmlid: str, odoo_version: str = "auto",
    profile_name: str | None = None,
) -> str:
    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        view_rec = session.run("""
            MATCH (v:View {xmlid: $xmlid, odoo_version: $ver})
            WHERE ($profile_name IS NULL OR $profile_name IN v.profile)
            OPTIONAL MATCH (v)-[:DEFINED_IN]->(mod:Module)
            RETURN v, mod.name AS module_name, mod.repo AS repo
        """, xmlid=xmlid, ver=odoo_version, profile_name=profile_name).single()

        if not view_rec:
            return f"View '{xmlid}' not found in Odoo {odoo_version}."

        parent_rec = session.run(f"""
            MATCH (v:View {{xmlid: $xmlid, odoo_version: $ver}})
                  -[r:{REL_INHERITS_VIEW}]->(parent:View {{odoo_version: $ver}})
            WHERE NOT coalesce(r.unresolved, false)
            AND ($profile_name IS NULL OR $profile_name IN v.profile)
            RETURN parent.xmlid AS parent_xmlid
        """, xmlid=xmlid, ver=odoo_version, profile_name=profile_name).single()

        extensions = session.run(f"""
            MATCH (ext:View {{odoo_version: $ver}})-[:{REL_INHERITS_VIEW}]->
                  (v:View {{xmlid: $xmlid, odoo_version: $ver}})
            WHERE NOT coalesce(ext.unresolved, false)
            AND ($profile_name IS NULL OR $profile_name IN ext.profile)
            OPTIONAL MATCH (ext)-[:DEFINED_IN]->(mod:Module)
            RETURN ext.xmlid AS ext_xmlid,
                   ext.xpaths_exprs AS xpaths_exprs,
                   ext.xpaths_positions AS xpaths_positions,
                   mod.name AS module_name, mod.repo AS repo
        """, xmlid=xmlid, ver=odoo_version, profile_name=profile_name).data()

    v_props = view_rec["v"]
    repo_str = f"[{view_rec['repo']}] " if view_rec.get("repo") else ""
    mode_label = " (extension)" if v_props.get("mode") == "extension" else ""

    # Build the list of branch (kind, payload) tuples, then render with the
    # correct connector based on whether each branch is the last one.
    # ADR-0023 §1.6: empty Extended by is silently skipped (no "No extensions").
    branches: list[tuple[str, object]] = []
    branches.append(("type", v_props.get("type", "?")))
    branches.append(("model", v_props.get("model", "?")))
    branches.append(
        ("module", f"{repo_str}{view_rec.get('module_name', '?')}{mode_label}"),
    )
    if parent_rec:
        branches.append(("inherits", parent_rec["parent_xmlid"]))
        own_exprs = list(v_props.get("xpaths_exprs") or [])
        own_positions = list(v_props.get("xpaths_positions") or [])
        if own_exprs:
            branches.append(("xpaths", list(zip(own_exprs, own_positions))))
    if extensions:
        branches.append(("extensions", extensions))
    # Wave 5: append Next-step footer per ADR-0023 §4. Suggest list_views
    # scoped to the same model when known, plus find_examples for xpath
    # patterns.
    view_model = v_props.get("model")
    next_hints: list[str] = []
    if view_model:
        next_hints.append(
            f"list_views(model='{view_model}', odoo_version='{odoo_version}')"
            " for sibling views",
        )
    next_hints.append(
        f"find_examples(query='{xmlid} xpath', odoo_version='{odoo_version}')"
        " for inheritance patterns",
    )
    branches.append(("next", next_hints))

    lines = [f"{xmlid} (Odoo {odoo_version})"]
    last_branch_idx = len(branches) - 1
    for i, (kind, payload) in enumerate(branches):
        is_last = i == last_branch_idx
        connector = "└─" if is_last else "├─"
        # Sublist indent: 4 spaces when this parent is last; "│   " otherwise.
        sub_indent = "    " if is_last else "│   "
        if kind == "type":
            lines.append(f"{connector} Type:   {payload}")
        elif kind == "model":
            lines.append(f"{connector} Model:  {payload}")
        elif kind == "module":
            lines.append(f"{connector} Module: {payload}")
        elif kind == "inherits":
            lines.append(f"{connector} Inherits from: {payload}")
        elif kind == "xpaths":
            pairs = payload  # type: ignore[assignment]
            lines.append(f"{connector} XPath modifications ({len(pairs)}):")
            last_x = len(pairs) - 1
            for j, (expr, pos) in enumerate(pairs):
                xconn = "└─" if j == last_x else "├─"
                lines.append(f"{sub_indent}{xconn} {expr} [{pos}]")
        elif kind == "extensions":
            exts = payload  # type: ignore[assignment]
            lines.append(f"{connector} Extended by ({len(exts)} modules):")
            more_hint = (
                f"resolve_view(xmlid='{xmlid}', odoo_version='{odoo_version}')"
                " to drill into a specific view"
            )

            def _fmt_ext(ext):
                ext_repo = f"[{ext['repo']}] " if ext.get("repo") else ""
                return (
                    f"{ext['ext_xmlid']}  →  {ext_repo}"
                    f"{ext.get('module_name', '?')}"
                )

            rendered = _render_capped(
                exts,
                _fmt_ext,
                cap=LIST_PREVIEW_MAX_ITEMS,
                more_hint=more_hint,
            )
            last_e = len(rendered) - 1
            # Only the first `min(len(exts), cap)` entries map to real ext
            # records (with xpaths). The trailing "... and K more" line, when
            # present, has no xpath subtree — handle it separately.
            for j, row in enumerate(rendered):
                econn = "└─" if j == last_e else "├─"
                lines.append(f"{sub_indent}{econn} {row}")
                if j < min(len(exts), LIST_PREVIEW_MAX_ITEMS):
                    ext = exts[j]
                    exprs = list(ext.get("xpaths_exprs") or [])
                    positions = list(ext.get("xpaths_positions") or [])
                    # Sub-sub indent uses pipe when ext is non-last, spaces when last
                    sub_sub = "    " if j == last_e else "│   "
                    for expr, pos in zip(exprs, positions):
                        lines.append(
                            f"{sub_indent}{sub_sub}└─ xpath: {expr} [{pos}]"
                        )
        elif kind == "next":
            hints = payload  # type: ignore[assignment]
            lines.append(format_next_step(hints))

    return "\n".join(lines)


def _find_examples(
    query: str,
    odoo_version: str = "auto",
    limit: int = 5,
    context_module: str | None = None,
    chunk_types: list[str] | None = None,
    profile_name: str | None = None,
    *,
    _driver=None,
    _pg_conn=None,
    _embedder=None,
) -> str:
    if not query.strip():
        # ADR-0023 §2: tool output must be English-only.
        return (
            "find_examples: empty query — provide a description of the"
            " feature you want to find\nFound 0 results\n"
        )

    from src.embedding.instructions import INSTRUCT_NL_TO_CODE

    driver = _driver or _get_driver()
    try:
        embedder = _embedder or _get_embedder()
    except Exception as e:
        return (
            f"find_examples: embedder unavailable — {type(e).__name__}: {e}\n"
            "Hint: check Ollama server is running (default: http://localhost:11434) "
            "and EMBEDDER_MODEL is loaded.\nFound 0 results\n"
        )

    with driver.session() as session:
        if odoo_version in ("auto", "latest"):
            odoo_version = _resolve_version("auto", session)

    try:
        query_vec = embedder.embed([INSTRUCT_NL_TO_CODE + query])[0]
    except Exception as e:
        return (
            f"find_examples: embedding query failed — {type(e).__name__}: {e}\n"
            "Hint: Ollama may be down, model not loaded, or network issue. "
            "Verify with: curl http://localhost:11434/api/tags\n"
            "Found 0 results\n"
        )

    selected_types = [t for t in (chunk_types or []) if t in VALID_CHUNK_TYPES]

    # Use injected connection (test path) or check out from pool (production).
    _pg_ctx = nullcontext(_pg_conn) if _pg_conn is not None else _checkout_pg()
    with _pg_ctx as pg:
        with pg.cursor() as cur:
            if selected_types:
                placeholders = ",".join(["%s"] * len(selected_types))
                cur.execute(
                    f"""SELECT chunk_type, module, entity_name, model_name, file_path,
                               chunk_idx, content, 1 - (vec <=> %s::vector) AS cosine
                        FROM embeddings
                        WHERE odoo_version = %s AND chunk_type IN ({placeholders})
                        ORDER BY vec <=> %s::vector LIMIT %s""",
                    [query_vec, odoo_version]
                    + selected_types
                    + [query_vec, min(limit, FIND_EXAMPLES_ANN_LIMIT)],
                )
            else:
                cur.execute(
                    """SELECT chunk_type, module, entity_name, model_name, file_path,
                              chunk_idx, content, 1 - (vec <=> %s::vector) AS cosine
                       FROM embeddings WHERE odoo_version = %s
                       ORDER BY vec <=> %s::vector LIMIT %s""",
                    [query_vec, odoo_version, query_vec, min(limit, FIND_EXAMPLES_ANN_LIMIT)],
                )
            raw = [
                dict(chunk_type=r[0], module=r[1], entity_name=r[2], model_name=r[3],
                     file_path=r[4], chunk_idx=r[5], content=r[6], cosine=float(r[7]))
                for r in cur.fetchall()
            ]

    raw = [c for c in raw if c["module"] != "__unresolved__"]

    # Neo4j centrality rerank + optional context_module boost.
    # Two UNWIND batch queries replace the previous N+1 per-chunk loop.
    # Coefficients (_RERANK_LOG_COEFF, _RERANK_CHAIN_BOOST) extracted as
    # module-level constants so tests/test_calibration_eval.py grid sweep can
    # monkey-patch them. Baseline (0.02, 0.20) calibrated against 100-query
    # Vi+En eval set 2026-05-11.
    module_names = list({c["module"] for c in raw})
    with driver.session() as session:
        dep_rows = session.run(
            f"UNWIND $names AS name"
            f" MATCH (m:Module {{name: name, odoo_version: $v}})"
            f" WHERE ($profile_name IS NULL OR $profile_name IN m.profile)"
            f" WITH m, name"
            f" OPTIONAL MATCH (dep)-[:{REL_DEPENDS_ON}]->(m)"
            f" RETURN name, count(dep) AS dependents",
            names=module_names, v=odoo_version, profile_name=profile_name,
        ).data()
        dependents_map = {r["name"]: r["dependents"] for r in dep_rows}

        in_chain_set: set[str] = set()
        if context_module and module_names:
            chain_rows = session.run(
                "MATCH (ctx:Module {name: $ctx, odoo_version: $v})"
                " -[:DEPENDS_ON*1..]->(tgt:Module)"
                " WHERE tgt.name IN $names"
                " AND ($profile_name IS NULL OR $profile_name IN ctx.profile)"
                " RETURN DISTINCT tgt.name AS name",
                ctx=context_module, v=odoo_version, names=module_names,
                profile_name=profile_name,
            ).data()
            in_chain_set = {r["name"] for r in chain_rows}

    for chunk in raw:
        dependents = dependents_map.get(chunk["module"], 0)
        chunk["score"] = chunk["cosine"] * (1 + _RERANK_LOG_COEFF * math.log(dependents + 1))
        if chunk["module"] in in_chain_set:
            chunk["score"] += _RERANK_CHAIN_BOOST

    reranked = sorted(raw, key=lambda c: c["score"], reverse=True)[:limit]

    header = f'find_examples: "{query}" ({odoo_version})\nFound {len(reranked)} results\n'
    if not reranked:
        return header

    sep = "─" * 41
    lines = [header]
    for i, chunk in enumerate(reranked, 1):
        entity = f'[{chunk["module"]}] {chunk["entity_name"]}'
        # For view chunks, show the model so readers know which UI the view belongs to
        if chunk["model_name"] and chunk["chunk_type"] == "view":
            entity += f" (model: {chunk['model_name']})"
        # For sliding-window chunks, show the window index so readers know it's a partial
        chunk_label = chunk["chunk_type"]
        if chunk["chunk_idx"] > 0:
            chunk_label += f" chunk {chunk['chunk_idx'] + 1}"
        lines.append(sep)
        lines.append(f"#{i} · score {chunk['score']:.2f} · {chunk_label} · {entity}")
        lines.append(f"   File: {chunk['file_path']}")
        lines.append("   ┌" + "─" * 42)
        for line in chunk["content"].splitlines():
            lines.append(f"   │ {line}")
        lines.append("   └" + "─" * 42)
        lines.append("")
    # Wave 5: Next-step footer per ADR-0023 §4. find_examples is a drill-down
    # entry-point; suggest moving to curated patterns or the canonical method.
    lines.append(format_next_step([
        f"suggest_pattern(intent='{query}', odoo_version='{odoo_version}')"
        " for curated patterns",
    ]))
    return "\n".join(lines)


@mcp.tool(output_schema=ResolveModelOutput.model_json_schema(), **READONLY_TOOL_KWARGS)
def resolve_model(
    target: str | None = None,
    model_name: str | None = None,
    odoo_version: str = "auto",
    profile_name: str | None = None,
) -> ToolResult:
    """Return full inheritance chain, field count, and method count for an Odoo model.

    TRIGGER when: "show inheritance chain of sale.order", "what fields does
    account.move have", "which modules extend res.partner", "liệt kê các field
    của model X", "module nào extend model Y"
    PREFER over: asking LLM from training data — returns real indexed data
    SKIP when: user wants detail on one field → use resolve_field;
    or method chain → use resolve_method
    SKIP when (NEW): you want multi-faceted model inspection →
        use model_inspect(model='...', method='summary') (M11+)

    Args:
        target: Opaque ref ID (e.g. 'm5') or canonical model name
            (e.g. 'sale.order'). Preferred over legacy model_name.
        model_name: DEPRECATED — use target=.
        odoo_version: e.g. '17.0'. Default 'auto'.
        profile_name: Optional profile filter.

    Returns:
        Tree: Defined in, Inherits from, Extended by, Fields count, Methods count.

    Example:
        resolve_model("sale.order", "17.0")
        → Defined in: [odoo] sale
          ├─ Extended by: [odoo] viin_sale, [odoo] to_sale_custom
          ├─ Fields: 47
          └─ Methods: 23

    See also: odoo://{version}/model/{name}
    """
    # --- dual-mode dispatch ---
    if target is not None and model_name is not None:
        warnings.warn(
            "resolve_model: ignoring legacy model_name=; target= takes precedence.",
            DeprecationWarning,
            stacklevel=2,
        )
    if model_name is not None and target is None:
        warnings.warn(
            "resolve_model: model_name= is deprecated; use target='ref' or"
            " target='sale.order' instead.",
            DeprecationWarning,
            stacklevel=2,
        )

    effective_model: str | None = None

    if target is not None:
        if _looks_like_ref(target):
            try:
                canonical = resolve_ref(target, _get_api_key_id())
                effective_model = canonical.get("model") or canonical.get("name")
            except RefError as err:
                err_text = _format_stale_ref_error("model", target, err)
                return ToolResult(
                    content=[TextContent(type="text", text=err_text)],
                    structured_content=None,
                )
        else:
            # canonical name: "sale.order"
            effective_model = target
    elif model_name is not None:
        effective_model = model_name

    if not effective_model:
        err_text = (
            "resolve_model: target= or model_name= required.\n"
            "└─ Example: resolve_model(target='sale.order', odoo_version='17.0')"
        )
        return ToolResult(
            content=[TextContent(type="text", text=err_text)],
            structured_content=None,
        )

    text = _resolve_model(effective_model, odoo_version, profile_name)
    structured = _resolve_model_structured(effective_model, odoo_version, profile_name)
    text = _deprecation_banner("model_inspect", "model='...', method='summary'") + text
    return ToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=structured.model_dump() if structured is not None else None,
    )


@mcp.tool(output_schema=ResolveFieldOutput.model_json_schema(), **READONLY_TOOL_KWARGS)
def resolve_field(
    target: str | None = None,
    model_name: str | None = None,
    field_name: str | None = None,
    odoo_version: str = "auto",
    profile_name: str | None = None,
) -> ToolResult:
    """Return type, compute/related metadata, and declaring modules for one field.

    TRIGGER when: "what type is amount_total", "is field X computed",
    "field X có related không", "kiểu dữ liệu của field X là gì"
    PREFER over: resolve_model for detail
    SKIP when: user wants all fields → use list_fields or model_inspect

    Args:
        target: Opaque ref ID (e.g. 'f12') or canonical dotted path
            (e.g. 'sale.order.amount_total'). Preferred over legacy args.
        model_name: DEPRECATED — use target=.
        field_name: DEPRECATED — use target=.
        odoo_version: e.g. '17.0'. Default 'auto'.
        profile_name: Optional profile filter.

    Returns:
        Tree: Type, Computed, Stored, Required, Related, Declared in modules.

    Example:
        resolve_field("sale.order.amount_total", "17.0")
        → Type: monetary | Computed: Yes | Stored: Yes | Required: No
          └─ Declared in: [odoo] sale

    See also: odoo://{version}/field/{model}/{field}
    """
    # --- dual-mode dispatch ---
    has_legacy = model_name is not None or field_name is not None
    if target is not None and has_legacy:
        warnings.warn(
            "resolve_field: ignoring legacy model_name=/field_name=; target= takes precedence.",
            DeprecationWarning,
            stacklevel=2,
        )
    if has_legacy and target is None:
        warnings.warn(
            "resolve_field: model_name=/field_name= are deprecated; use"
            " target='ref_id' or target='model.field_name' instead.",
            DeprecationWarning,
            stacklevel=2,
        )

    effective_model: str | None = model_name
    effective_field: str | None = field_name

    if target is not None:
        if _looks_like_ref(target):
            try:
                canonical = resolve_ref(target, _get_api_key_id())
                effective_model = canonical.get("model")
                effective_field = canonical.get("field_name") or canonical.get("name")
            except RefError as err:
                err_text = _format_stale_ref_error("field", target, err)
                return ToolResult(
                    content=[TextContent(type="text", text=err_text)],
                    structured_content=None,
                )
        else:
            # canonical: "sale.order.amount_total" — split on last dot boundary
            # to separate model (may contain dots) from field name.
            parts = target.rsplit(".", 1)
            if len(parts) == 2:
                effective_model, effective_field = parts[0], parts[1]
            else:
                # No dot — treat whole string as field name, model unknown.
                effective_field = target

    if not (effective_model and effective_field):
        err_text = (
            "resolve_field: target= or model_name= + field_name= required.\n"
            "└─ Example: resolve_field(target='sale.order.amount_total')"
            " or resolve_field(model_name='sale.order', field_name='amount_total')"
        )
        return ToolResult(
            content=[TextContent(type="text", text=err_text)],
            structured_content=None,
        )

    text = _resolve_field(effective_model, effective_field, odoo_version, profile_name)
    structured = _resolve_field_structured(
        effective_model, effective_field, odoo_version, profile_name
    )
    text = _deprecation_banner(
        "model_inspect",
        "model='...', method='field', field='...'"
    ) + text
    return ToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=structured.model_dump() if structured is not None else None,
    )


@mcp.tool(output_schema=ResolveMethodOutput.model_json_schema(), **READONLY_TOOL_KWARGS)
def resolve_method(
    target: str | None = None,
    model_name: str | None = None,
    method_name: str | None = None,
    odoo_version: str = "auto",
    profile_name: str | None = None,
) -> ToolResult:
    """Return the full override chain of a method, ordered base to top.

    TRIGGER when: "show override chain of action_confirm", "where is method X",
    "method nào super() lên model kia", "ai override method X"
    PREFER over: grep — returns full chain with super() linkage
    SKIP when: user wants all methods → use list_methods or model_inspect

    Args:
        target: Opaque ref ID (e.g. 'm3') or canonical dotted path
            (e.g. 'sale.order.action_confirm'). Preferred over legacy args.
        model_name: DEPRECATED — use target=.
        method_name: DEPRECATED — use target=.
        odoo_version: e.g. '17.0'. Default 'auto'.
        profile_name: Optional profile filter.

    Returns:
        Tree: Override chain base→top with super() status and decorators.

    Example:
        resolve_method("sale.order.action_confirm", "17.0")
        → Override chain (3):
            ├─ [odoo] sale — ✗ no super()
            ├─ [odoo] viin_sale — ✓ super()
            └─ [odoo] to_sale_workflow — ✓ super()

    See also: odoo://{version}/method/{model}/{method}
    """
    # --- dual-mode dispatch ---
    has_legacy = model_name is not None or method_name is not None
    if target is not None and has_legacy:
        warnings.warn(
            "resolve_method: ignoring legacy model_name=/method_name=; target= takes precedence.",
            DeprecationWarning,
            stacklevel=2,
        )
    if has_legacy and target is None:
        warnings.warn(
            "resolve_method: model_name=/method_name= are deprecated; use"
            " target='ref_id' or target='model.method_name' instead.",
            DeprecationWarning,
            stacklevel=2,
        )

    effective_model: str | None = model_name
    effective_method: str | None = method_name

    if target is not None:
        if _looks_like_ref(target):
            try:
                canonical = resolve_ref(target, _get_api_key_id())
                effective_model = canonical.get("model")
                effective_method = canonical.get("method_name") or canonical.get("name")
            except RefError as err:
                err_text = _format_stale_ref_error("method", target, err)
                return ToolResult(
                    content=[TextContent(type="text", text=err_text)],
                    structured_content=None,
                )
        else:
            # canonical: "sale.order.action_confirm" — rsplit on last dot
            parts = target.rsplit(".", 1)
            if len(parts) == 2:
                effective_model, effective_method = parts[0], parts[1]
            else:
                effective_method = target

    if not (effective_model and effective_method):
        err_text = (
            "resolve_method: target= or model_name= + method_name= required.\n"
            "└─ Example: resolve_method(target='sale.order.action_confirm')"
            " or resolve_method(model_name='sale.order', method_name='action_confirm')"
        )
        return ToolResult(
            content=[TextContent(type="text", text=err_text)],
            structured_content=None,
        )

    text = _resolve_method(effective_model, effective_method, odoo_version, profile_name)
    structured = _resolve_method_structured(
        effective_model, effective_method, odoo_version, profile_name
    )
    text = _deprecation_banner(
        "model_inspect",
        "model='...', method='method', method_name='...'"
    ) + text
    return ToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=structured.model_dump() if structured is not None else None,
    )


@mcp.tool(output_schema=ResolveViewOutput.model_json_schema(), **READONLY_TOOL_KWARGS)
def resolve_view(
    target: str | None = None,
    xmlid: str | None = None,
    odoo_version: str = "auto",
    profile_name: str | None = None,
) -> ToolResult:
    """Return view inheritance chain and XPath modifications from all extension modules.

    TRIGGER when: "show xpath overrides for sale.order form", "which modules
    modify view X", "view bị override bởi module nào", "XPath chain của view X"
    PREFER over: searching XML files — aggregates cross-module XPath
    overrides into one merged skeleton
    SKIP when: user wants Python logic → resolve_method; field info → resolve_field

    Args:
        target: Opaque ref ID (e.g. 'v3') or canonical XML ID
            (e.g. 'sale.view_order_form'). Preferred over legacy xmlid.
        xmlid: DEPRECATED — use target=.
        odoo_version: e.g. '17.0'. Default 'auto'.
        profile_name: Optional profile filter.

    Returns:
        Tree: Type, model, defining module, parent view (if extension),
        XPath ops, extending views per module.

    Example:
        resolve_view("sale.view_order_form", "17.0")
        → Type: form | Model: sale.order | Module: [odoo] sale
          └─ Extended by (2):
              ├─ [odoo] viin_sale
              └─ [odoo] to_sale_custom

    See also: odoo://{version}/view/{xmlid}
    """
    # --- dual-mode dispatch ---
    if target is not None and xmlid is not None:
        warnings.warn(
            "resolve_view: ignoring legacy xmlid=; target= takes precedence.",
            DeprecationWarning,
            stacklevel=2,
        )
    if xmlid is not None and target is None:
        warnings.warn(
            "resolve_view: xmlid= is deprecated; use target='ref' or"
            " target='sale.view_order_form' instead.",
            DeprecationWarning,
            stacklevel=2,
        )

    effective_xmlid: str | None = xmlid

    if target is not None:
        if _looks_like_ref(target):
            try:
                canonical = resolve_ref(target, _get_api_key_id())
                effective_xmlid = canonical.get("xmlid")
            except RefError as err:
                err_text = _format_stale_ref_error("view", target, err)
                return ToolResult(
                    content=[TextContent(type="text", text=err_text)],
                    structured_content=None,
                )
        else:
            # canonical xmlid: "sale.view_order_form"
            effective_xmlid = target

    if not effective_xmlid:
        err_text = (
            "resolve_view: target= or xmlid= required.\n"
            "└─ Example: resolve_view(target='sale.view_order_form')"
            " or resolve_view(xmlid='sale.view_order_form')"
        )
        return ToolResult(
            content=[TextContent(type="text", text=err_text)],
            structured_content=None,
        )

    text = _resolve_view(effective_xmlid, odoo_version, profile_name)
    structured = _resolve_view_structured(effective_xmlid, odoo_version, profile_name)
    text = _deprecation_banner("entity_lookup", "kind='view', xmlid='...'") + text
    return ToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=structured.model_dump() if structured is not None else None,
    )


@mcp.tool(**READONLY_TOOL_KWARGS)
def find_examples(
    query: str,
    odoo_version: str = "auto",
    limit: int = 5,
    context_module: str | None = None,
    chunk_types: list[str] | None = None,
    profile_name: str | None = None,
) -> str:
    """Semantic search for real code examples from the indexed Odoo codebase.

    Requires Ollama running with model `qwen3-embedding-q5km`.

    TRIGGER when: "show me examples of wizard usage", "how is mail.thread used
    in codebase", "give me code example for X pattern", "ví dụ code dùng X
    trong codebase", "cách dùng X trong thực tế", "how to send email in Odoo"
    PREFER over: LLM-generated examples — returns real indexed code, not
    hallucinated patterns or outdated snippets from training data
    SKIP when: user wants to know if a module exists → use check_module_exists;
    user wants pattern guidance with gotchas → use suggest_pattern

    Args:
        query: Feature description (EN or VN).
        odoo_version: e.g. "17.0". Default "auto" = latest indexed.
        limit: Number of results (default 5, max 20).
        context_module: Boost results from modules this module depends on.
        chunk_types: Filter by type: method, field, view, qweb, js_era1,
            js_era2, js_era3. Default: all types.
        profile_name: Profile filter for Neo4j rerank (ADR-0016 D6).

    Returns:
        Header + N results ranked by cosine + centrality + context boost.
        Each result: score, type, module, entity, file path, content snippet.

    Example:
        find_examples("confirm sale order and send email", "17.0", limit=3)
        → find_examples: "confirm sale order and send email" (17.0)
          Found 3 results
          #1 · score 0.82 · method · [sale] sale.order.action_confirm
             File: sale/models/sale_order.py
    """
    return _find_examples(
        query, odoo_version, limit, context_module, chunk_types, profile_name
    )


def _compute_risk(view_count: int, method_count: int, js_count: int) -> str:
    """Risk thresholds v1 — validated 2026-05-11 against 25-case curated incident set.

    Dataset: tests/eval/impact_analysis_incidents.json (7 HIGH, 8 MEDIUM, 10 LOW cases).
    Macro-F1 = 1.0000 (perfect classification on all 25 cases).
    Sweep candidates: HIGH ∈ {7, 10, 12, 15} × MED ∈ {3, 4, 5, 6}.
    Current thresholds (HIGH>=10, MED>=4) are optimal vs all candidate pairs.
    (HIGH>=10, MED>=3 also achieves macro-F1=1.0 but MED=4 preserves the original
    "4-9 = module-scope review" semantics without information loss.)
    Re-validate: pytest tests/test_calibration_eval.py::test_risk_threshold_validation -v

    HIGH >= 10 affected entities, MEDIUM 4-9, LOW < 4.
    Rationale: <4 = isolated change, 4-9 = module-scope review needed,
    >=10 = cross-module impact requiring full regression.
    """
    total = view_count + method_count + js_count
    if total >= IMPACT_RISK_HIGH_THRESHOLD:
        return "HIGH"
    if total >= IMPACT_RISK_MED_THRESHOLD:
        return "MEDIUM"
    return "LOW"


def _impact_analysis(
    entity_type: str,
    entity_name: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
) -> str:
    """Return everything affected by changing the given entity. Risk-scored."""
    valid_types = ("field", "method", "model")
    if entity_type not in valid_types:
        return (
            f"Invalid entity_type '{entity_type}'. Use: field, method, model."
        )

    # ------------------------------------------------------------------ #
    # Parse entity_name per entity_type — validate before touching DB    #
    # ------------------------------------------------------------------ #
    if entity_type in ("field", "method"):
        if "." not in entity_name:
            return (
                f"Entity '{entity_name}' not found. "
                f"Expected format: '<model>.<{entity_type}>' "
                f"(e.g. 'sale.order.amount_total' for a field)."
            )
        # Split on LAST dot: model has dots, field/method does not
        last_dot = entity_name.rfind(".")
        model_name = entity_name[:last_dot]
        member_name = entity_name[last_dot + 1:]
    else:
        # entity_type == "model"
        model_name = entity_name
        member_name = None

    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        # ------------------------------------------------------------------ #
        # Query 1: verify entity exists                                        #
        # ------------------------------------------------------------------ #
        if entity_type == "field":
            exists = session.run(
                "MATCH (f:Field {name: $fn, model: $mn, odoo_version: $v}) "
                "WHERE ($profile_name IS NULL OR $profile_name IN f.profile) "
                "RETURN count(f) AS c",
                fn=member_name, mn=model_name, v=odoo_version,
                profile_name=profile_name,
            ).single()["c"]
            if not exists:
                return (
                    f"Entity '{entity_name}' not found in Odoo {odoo_version}."
                )
        elif entity_type == "method":
            exists = session.run(
                "MATCH (mth:Method {name: $mn, model: $model, odoo_version: $v}) "
                "WHERE ($profile_name IS NULL OR $profile_name IN mth.profile) "
                "RETURN count(mth) AS c",
                mn=member_name, model=model_name, v=odoo_version,
                profile_name=profile_name,
            ).single()["c"]
            if not exists:
                return (
                    f"Entity '{entity_name}' not found in Odoo {odoo_version}."
                )
        else:  # model
            exists = session.run(
                "MATCH (m:Model {name: $mn, odoo_version: $v}) "
                "WHERE coalesce(m.unresolved, false) = false "
                "AND m.module <> '__unresolved__' "
                "AND ($profile_name IS NULL OR $profile_name IN m.profile) "
                "RETURN count(m) AS c",
                mn=model_name, v=odoo_version, profile_name=profile_name,
            ).single()["c"]
            if not exists:
                return (
                    f"Entity '{entity_name}' not found in Odoo {odoo_version}."
                )

        # ------------------------------------------------------------------ #
        # Query 2: views targeting model (DISTINCT to avoid TARGETS_MODEL fan-out)
        # ------------------------------------------------------------------ #
        views = session.run(f"""
            MATCH (m:Model {{name: $mn, odoo_version: $v}})<-[:{REL_TARGETS_MODEL}]-(view:View)
            WHERE ($profile_name IS NULL OR $profile_name IN view.profile)
            RETURN DISTINCT view.xmlid AS xmlid, view.module AS module
            ORDER BY view.module, view.xmlid
        """, mn=model_name, v=odoo_version, profile_name=profile_name).data()

        # ------------------------------------------------------------------ #
        # Query 3: methods on this model (with super call filter for field;   #
        #          all overrides for method entity_type)                       #
        # ------------------------------------------------------------------ #
        if entity_type == "field":
            methods = session.run("""
                MATCH (mth:Method {model: $mn, odoo_version: $v})
                WHERE mth.has_super_call = true
                AND ($profile_name IS NULL OR $profile_name IN mth.profile)
                RETURN DISTINCT mth.name AS name, mth.module AS module
                ORDER BY mth.module, mth.name
            """, mn=model_name, v=odoo_version, profile_name=profile_name).data()
        elif entity_type == "method":
            methods = session.run("""
                MATCH (mth:Method {name: $mn2, model: $mn, odoo_version: $v})
                WHERE ($profile_name IS NULL OR $profile_name IN mth.profile)
                RETURN DISTINCT mth.name AS name, mth.module AS module
                ORDER BY mth.module
            """, mn2=member_name, mn=model_name, v=odoo_version,
                profile_name=profile_name).data()
        else:  # model
            methods = session.run("""
                MATCH (mth:Method {model: $mn, odoo_version: $v})
                WHERE ($profile_name IS NULL OR $profile_name IN mth.profile)
                RETURN DISTINCT mth.name AS name, mth.module AS module
                ORDER BY mth.module, mth.name
            """, mn=model_name, v=odoo_version, profile_name=profile_name).data()

        # ------------------------------------------------------------------ #
        # Query 4: JS patches on components bound to this model               #
        # ------------------------------------------------------------------ #
        js_patches = session.run("""
            MATCH (m:Model {name: $mn, odoo_version: $v})<-[:BOUND_TO]-(comp:OWLComp)
                  <-[:PATCHES]-(jp:JSPatch)
            WHERE ($profile_name IS NULL OR $profile_name IN jp.profile)
            RETURN DISTINCT jp.target AS target, jp.patch_name AS patch_name,
                   jp.module AS module, jp.era AS era
            ORDER BY jp.module, jp.target
        """, mn=model_name, v=odoo_version, profile_name=profile_name).data()

        # ------------------------------------------------------------------ #
        # Query 5: dependent modules of all modules defining this model       #
        # ------------------------------------------------------------------ #
        dep_modules = session.run(f"""
            MATCH (m:Model {{name: $mn, odoo_version: $v}})-[:DEFINED_IN]->(defmod:Module)
                  <-[:{REL_DEPENDS_ON}]-(depmod:Module)
            WHERE ($profile_name IS NULL OR $profile_name IN depmod.profile)
            RETURN DISTINCT depmod.name AS dep_name
            ORDER BY depmod.name
        """, mn=model_name, v=odoo_version, profile_name=profile_name).data()

        # For model entity_type: also collect defining modules as "extensions"
        if entity_type == "model":
            def_modules = session.run("""
                MATCH (m:Model {name: $mn, odoo_version: $v})-[:DEFINED_IN]->(mod:Module)
                WHERE ($profile_name IS NULL OR $profile_name IN m.profile)
                RETURN DISTINCT m.module AS module_name
                ORDER BY m.module
            """, mn=model_name, v=odoo_version, profile_name=profile_name).data()
        else:
            def_modules = []

    # ---------------------------------------------------------------------- #
    # Build output tree                                                        #
    # ---------------------------------------------------------------------- #
    view_count = len(views)
    method_count = len(methods)
    js_count = len(js_patches)
    total = view_count + method_count + js_count
    risk = _compute_risk(view_count, method_count, js_count)

    lines = [f"impact_analysis({entity_type}, {entity_name}, {odoo_version})"]
    lines.append(f"├─ Risk: {risk} ({total} affected entities)")

    # Views section
    if views:
        lines.append(f"├─ Views ({view_count}):")
        for i, v_item in enumerate(views):
            connector = "└─" if i == view_count - 1 else "├─"
            lines.append(f"│   {connector} [{v_item['module']}] {v_item['xmlid']}")
    else:
        lines.append("├─ Views: none")

    # Methods section
    if entity_type == "field":
        methods_label = (
            f"Methods on {model_name} with super() ({method_count})"
            f" — field-level filter not yet implemented (M5)"
        )
    elif entity_type == "method":
        methods_label = "Override chain"
    else:
        methods_label = "Methods"

    if entity_type == "field":
        # For field: use pre-built label that already contains count
        if methods:
            lines.append(f"├─ {methods_label}:")
            for i, m_item in enumerate(methods):
                connector = "└─" if i == method_count - 1 else "├─"
                lines.append(f"│   {connector} [{m_item['module']}] {m_item['name']}")
        else:
            lines.append(f"├─ {methods_label}: none")
        lines.append(
            "│   Note: field-level impact requires F4 USES_FIELD edge"
            " (deferred to M5). Current scope: model-level."
        )
    elif methods:
        lines.append(f"├─ {methods_label} ({method_count}):")
        for i, m_item in enumerate(methods):
            connector = "└─" if i == method_count - 1 else "├─"
            lines.append(f"│   {connector} [{m_item['module']}] {m_item['name']}")
    else:
        lines.append(f"├─ {methods_label}: none")

    # JS patches section
    if js_patches:
        lines.append(f"├─ JS patches ({js_count}):")
        for i, jp in enumerate(js_patches):
            connector = "└─" if i == js_count - 1 else "├─"
            lines.append(
                f"│   {connector} [{jp['module']}] {jp['target']}"
                f" via {jp['patch_name']} (era: {jp['era']})"
            )
    else:
        lines.append("├─ JS patches: none")

    # For model entity_type: extension modules section
    if entity_type == "model" and def_modules:
        mod_names = [d["module_name"] for d in def_modules]
        lines.append(f"├─ Defined/extended in ({len(mod_names)}): {', '.join(mod_names)}")

    # Dependent modules section
    if dep_modules:
        dep_names = [d["dep_name"] for d in dep_modules]
        lines.append(f"├─ Dependent modules ({len(dep_names)}): {', '.join(dep_names)}")
    else:
        lines.append("├─ Dependent modules: none")

    # Wave 5: Next-step footer per ADR-0023 §4.
    if entity_type == "method":
        next_hints = [
            f"find_override_point(model='{model_name}', method='{member_name}'"
            f", odoo_version='{odoo_version}') for safe extension spot",
            f"find_deprecated_usage(odoo_version='{odoo_version}')"
            " to widen for deprecated calls",
        ]
    elif entity_type == "field":
        next_hints = [
            f"resolve_field(model_name='{model_name}', field_name='{member_name}'"
            f", odoo_version='{odoo_version}') for field detail",
            f"find_deprecated_usage(odoo_version='{odoo_version}')"
            " to widen for deprecated calls",
        ]
    else:  # model
        next_hints = [
            f"list_methods(model='{model_name}', odoo_version='{odoo_version}')"
            " for behavior surface",
            f"find_deprecated_usage(odoo_version='{odoo_version}')"
            " to widen for deprecated calls",
        ]
    lines.append(format_next_step(next_hints))
    return "\n".join(lines)


@mcp.tool(**READONLY_TOOL_KWARGS)
def impact_analysis(
    entity_type: str,
    entity_name: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
) -> str:
    """List everything affected by changing an entity. Risk-scored LOW/MEDIUM/HIGH.

    TRIGGER when: "what breaks if I change amount_total", "impact of modifying
    field X", "dependencies of method Y", "thay đổi field X ảnh hưởng đến gì",
    "rủi ro khi sửa method Y", "blast radius of removing field Z"
    PREFER over: manual grep — traces transitive dependencies (views, methods,
    JS patches, dependent modules) across all indexed repos automatically
    SKIP when: user wants to see who extends a model → use resolve_model;
    user wants deprecation warnings → use find_deprecated_usage

    Args:
        entity_type: One of 'field', 'method', 'model'.
        entity_name: For field/method: '<model>.<name>' e.g.
            'sale.order.amount_total'. For model: '<model>' e.g. 'sale.order'.
        odoo_version: e.g. '17.0'. Default 'auto'.
        profile_name: Profile filter for all 5 sub-queries
            (Field/Method/View/JSPatch/Module). Default None = all profiles.

    Returns:
        Risk score (LOW/MEDIUM/HIGH) + breakdown of affected views, methods,
        JS patches across modules. Use BEFORE renaming or removing entities.

    Example:
        impact_analysis("field", "sale.order.amount_total", "17.0")
        → impact_analysis(field, sale.order.amount_total, 17.0)
          ├─ Risk: MEDIUM (7 affected entities)
          ├─ Views (3): ...
          ├─ Methods (4): ...
          └─ Dependent modules (2): viin_sale, to_sale_custom
    """
    return _impact_analysis(entity_type, entity_name, odoo_version, profile_name)


# --- M4.5 spec layer tools ----------------------------------------------

def _format_core_symbol(rec: dict, version: str) -> str:
    """Tree-format a single CoreSymbol query record."""
    qn = rec.get("qualified_name") or "?"
    kind = rec.get("kind") or "?"
    status = rec.get("status") or "stable"
    sig = rec.get("signature")
    repl = rec.get("replacement_qname")
    file_path = rec.get("file_path")
    line = rec.get("line")
    added_in = rec.get("added_in")
    removed_in = rec.get("removed_in")
    deprecated_in = rec.get("deprecated_in")

    lines = [f"{qn} (Odoo {version})"]
    lines.append(f"├─ Kind:        {kind}")
    lines.append(f"├─ Status:      {status}")
    if sig:
        lines.append(f"├─ Signature:   {sig}")
    if repl:
        lines.append(f"├─ Replacement: {repl}")
    if added_in:
        lines.append(f"├─ Added in:    {added_in}")
    if deprecated_in:
        lines.append(f"├─ Deprecated:  {deprecated_in}")
    if removed_in:
        lines.append(f"├─ Removed in:  {removed_in}")
    if file_path:
        loc = file_path + (f":{line}" if line else "")
        lines.append(f"├─ Source:      {loc}")
    # Wave 5: Next-step footer per ADR-0023 §4. Always ├─ above and append
    # the Next line as the final └─.
    next_hints = [
        f"find_examples(query='{qn}', odoo_version='{version}')"
        " for in-the-wild usage patterns",
        f"find_deprecated_usage(odoo_version='{version}')"
        " to scan for deprecated calls",
    ]
    lines.append(format_next_step(next_hints))
    return "\n".join(lines)


def _lookup_core_api(name: str, odoo_version: str = "auto") -> str:
    """Return signature + status + replacement for a single Odoo core API symbol."""
    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)
        rec = session.run("""
            MATCH (cs:CoreSymbol {odoo_version: $v})
            WHERE cs.qualified_name = $name
               OR cs.qualified_name ENDS WITH '.' + $name
            RETURN cs.qualified_name AS qualified_name,
                   cs.kind AS kind,
                   cs.status AS status,
                   cs.signature AS signature,
                   cs.replacement_qname AS replacement_qname,
                   cs.file_path AS file_path,
                   cs.line AS line,
                   cs.added_in AS added_in,
                   cs.removed_in AS removed_in,
                   cs.deprecated_in AS deprecated_in
            ORDER BY size(cs.qualified_name) ASC
            LIMIT 1
        """, name=name, v=odoo_version).single()
    if rec is None:
        next_line = format_next_step([
            f"find_examples(query='{name}', odoo_version='{odoo_version}')"
            " for in-the-wild usage patterns",
        ])
        return (
            f"lookup_core_api({name!r}, {odoo_version!r})\n"
            f"├─ not found in indexed Odoo core for version {odoo_version}\n"
            + next_line
        )
    return _format_core_symbol(dict(rec), odoo_version)


def _format_api_diff(
    sym_old: dict | None,
    sym_new: dict | None,
    name: str,
    from_version: str,
    to_version: str,
) -> str:
    """Render the diff of one symbol between two versions."""
    header = f"api_version_diff({name!r}: {from_version} → {to_version})"
    lines = [header]
    if sym_old and not sym_new:
        lines.append(f"├─ Status:    removed in {to_version}")
        lines.append(f"├─ Was:       {sym_old.get('signature') or '?'}")
        repl = sym_old.get("replacement_qname")
        if repl:
            lines.append(f"└─ Replaced by: {repl}")
        else:
            lines[-1] = lines[-1].replace("├─", "└─")
        return "\n".join(lines)
    if sym_new and not sym_old:
        lines.append(f"├─ Status:    added in {to_version}")
        lines.append(f"└─ Now:       {sym_new.get('signature') or '?'}")
        return "\n".join(lines)
    # Both exist
    sig_old = sym_old.get("signature") if sym_old else None
    sig_new = sym_new.get("signature") if sym_new else None
    lines.append(f"├─ {from_version}: {sig_old or '?'} (status={sym_old.get('status')})")
    lines.append(f"├─ {to_version}: {sig_new or '?'} (status={sym_new.get('status')})")
    if sig_old and sig_new and sig_old != sig_new:
        lines.append("└─ Signature changed")
    else:
        lines.append("└─ Stable across versions")
    return "\n".join(lines)


def _fetch_core_symbol(session, name: str, version: str) -> dict | None:
    rec = session.run("""
        MATCH (cs:CoreSymbol {odoo_version: $v})
        WHERE cs.qualified_name = $name
           OR cs.qualified_name ENDS WITH '.' + $name
        RETURN cs.qualified_name AS qualified_name,
               cs.kind AS kind,
               cs.status AS status,
               cs.signature AS signature,
               cs.replacement_qname AS replacement_qname,
               cs.file_path AS file_path,
               cs.line AS line,
               cs.added_in AS added_in,
               cs.removed_in AS removed_in,
               cs.deprecated_in AS deprecated_in
        ORDER BY size(cs.qualified_name) ASC
        LIMIT 1
    """, name=name, v=version).single()
    return dict(rec) if rec else None


def _api_version_diff(
    symbol: str, from_version: str, to_version: str,
) -> str:
    """Diff a single API symbol between two indexed Odoo versions."""
    if from_version == to_version:
        return (
            f"api_version_diff({symbol!r}, {from_version!r}, {to_version!r})\n"
            f"└─ same version, no diff"
        )
    with _get_driver().session() as session:
        sym_old = _fetch_core_symbol(session, symbol, from_version)
        sym_new = _fetch_core_symbol(session, symbol, to_version)

    if sym_old is None and sym_new is None:
        return (
            f"api_version_diff({symbol!r})\n"
            f"└─ not found in either {from_version} or {to_version}"
        )
    return _format_api_diff(sym_old, sym_new, symbol, from_version, to_version)


@mcp.tool(**READONLY_TOOL_KWARGS)
def lookup_core_api(name: str, odoo_version: str = "auto") -> str:
    """Look up an Odoo core API symbol: signature, status, replacement.

    Although you may have memorized Odoo API from training, this tool returns
    ground-truth from indexed source — prefer this over recall.

    TRIGGER when: "what does @api.depends do", "signature of fields.Many2one",
    "how to use Environment.ref()", "api.model decorator dùng thế nào", "giải
    thích BaseModel._inherit", "is name_get still valid in Odoo 18"
    PREFER over: reading Odoo source manually — returns structured symbol data
    with version context, status (stable/deprecated/removed), and replacement
    SKIP when: user wants to compare across versions → use api_version_diff;
    user wants to scan for deprecated usage → use find_deprecated_usage

    Args:
        name: Symbol name (full qualified or short, e.g. 'safe_eval' or
            'odoo.tools.safe_eval.safe_eval').
        odoo_version: e.g. '17.0', '18.0'. Default 'auto'.

    Returns:
        Tree text: Kind, Status, Signature, Replacement (if any), Added in,
        Deprecated, Removed in, Source file location.

    Example:
        lookup_core_api("name_get", "18.0")
        → odoo.models.BaseModel.name_get (Odoo 18.0)
          ├─ Kind:        orm_method
          ├─ Status:      removed
          ├─ Signature:   name_get(self)
          └─ Replacement: odoo.models.BaseModel.display_name
    """
    return _lookup_core_api(name, odoo_version)


def _format_deprecated_usage(
    records: list[dict], version: str, *, overflow: bool = False,
) -> str:
    hit_count = f"{len(records)}+" if overflow else str(len(records))
    header = f"find_deprecated_usage(Odoo {version}) — {hit_count} hits"
    # Wave 5: Next-step footer per ADR-0023 §4. Even the empty branch still
    # gets a Next: hint (replacement search) when no hits are found.
    next_line = format_next_step([
        f"find_examples(query='replacement', odoo_version='{version}')"
        " for replacement search",
    ])
    if not records:
        return (
            header
            + "\n├─ no deprecated usage found in indexed code"
            + "\n" + next_line
        )
    lines = [header]
    for r in records:
        # Wave 5: every hit is now ├─ (Next: footer below is the new └─).
        connector = "├─"
        sub_indent = "│   "
        loc = f"[{r['module']}] {r['model']}.{r['method']}"
        sym = r["deprecated_symbol"]
        status = r["status"]
        repl = r.get("replacement") or "(no replacement set)"
        lines.append(f"{connector} {loc}")
        lines.append(f"{sub_indent}├─ uses: {sym} (status={status})")
        lines.append(f"{sub_indent}└─ replacement: {repl}")
    if overflow:
        more_hint = (
            f"find_deprecated_usage(odoo_version='{version}', kind=<filter>)"
            " to narrow the scan"
        )
        lines.append(
            f"├─ ... more results may exist beyond preview cap (refine filter via {more_hint})"
        )
    lines.append(next_line)
    return "\n".join(lines)


def _find_deprecated_usage(
    odoo_version: str = "auto", kind: str | None = None,
    profile_name: str | None = None,
) -> str:
    """Scan user code for usage of CoreSymbol entries with deprecated/removed status."""
    cap_plus_one = LIST_PREVIEW_MAX_ITEMS + 1
    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)
        cypher = f"""
            MATCH (mth:Method {{odoo_version: $v}})-[:{REL_USES_CORE_SYMBOL}]->(cs:CoreSymbol)
            WHERE cs.status IN ['deprecated', 'removed']
            AND ($profile_name IS NULL OR $profile_name IN mth.profile)
        """
        params: dict = {"v": odoo_version, "profile_name": profile_name,
                        "cap_plus_one": cap_plus_one}
        if kind:
            cypher += " AND cs.kind = $kind"
            params["kind"] = kind
        cypher += """
            RETURN mth.module AS module, mth.model AS model, mth.name AS method,
                   cs.qualified_name AS deprecated_symbol,
                   cs.status AS status,
                   cs.replacement_qname AS replacement
            ORDER BY mth.module, mth.model, mth.name
            LIMIT $cap_plus_one
        """
        records = session.run(cypher, **params).data()
    overflow = len(records) > LIST_PREVIEW_MAX_ITEMS
    if overflow:
        records = records[:LIST_PREVIEW_MAX_ITEMS]
    return _format_deprecated_usage(records, odoo_version, overflow=overflow)


_VALID_LINT_LANGUAGES = {"python", "javascript", "xml"}


def _format_lint_check(
    violations: list[dict], version: str, code: str, language: str = "python",
) -> str:
    header = f"lint_check(Odoo {version}, language={language}) — {len(violations)} violations"
    code_preview = (code or "")[:CODE_PREVIEW_MAX_CHARS].replace("\n", " ")
    lines = [_LINT_V0_BANNER, header, f"├─ Code: {code_preview!r}"]
    if not violations:
        lines.append("└─ no violations")
        return "\n".join(lines)
    last_idx = len(violations) - 1
    for i, r in enumerate(violations):
        connector = "└─" if i == last_idx else "├─"
        rule_id = r.get("rule_id") or "?"
        sev = r.get("severity") or "warning"
        msg = (r.get("message") or "").strip()
        lines.append(f"{connector} {rule_id} ({sev}): {msg}")
    return "\n".join(lines)


# V0 lint matcher constant — surface in every lint_check output so users know this
# is a fuzzy approximation requiring manual verification.
_LINT_V0_BANNER = (
    "⚠ V0 fuzzy matcher — verify manually before action. "
    "Requires ≥2 significant token overlap between rule message and code."
)

_LINT_STOPWORDS = frozenset({
    "with", "from", "this", "that", "have", "must", "should",
    "function", "usage", "literal", "string", "alias", "option",
    "the", "and", "use", "not", "for", "are", "when", "avoid",
    "call", "called", "calling", "instead", "please", "using",
})


def _match_lint_rule(code: str, rule: dict) -> bool:
    """V0 lint match: ≥2 significant token overlap between rule.message and code.

    Significant token: >3 chars, alpha-only (after split on [^a-z_]), not in stopword set.
    Requires at least 2 such tokens from the rule message to appear in the code.
    This reduces single-word false positives common in the previous ≥1 threshold.

    Returns False if rule.message is empty or has fewer than 2 significant tokens.
    """
    import re as _re

    msg = (rule.get("message") or "").lower()
    if not msg:
        return False
    code_lc = (code or "").lower()
    # Tokenize on non-alpha-underscore boundaries, keep tokens > 3 chars.
    rule_tokens = {
        t for t in _re.split(r"[^a-z_]+", msg)
        if len(t) > 3 and t not in _LINT_STOPWORDS
    }
    if len(rule_tokens) < 2:
        # Not enough significant tokens in the rule message itself → never fires.
        return False
    code_tokens = set(_re.split(r"[^a-z_]+", code_lc))
    overlap = rule_tokens & code_tokens
    return len(overlap) >= 2


def _lint_check(
    code: str, odoo_version: str = "auto", language: str = "python",
) -> str:
    """Pattern-match user code against indexed LintRule.message (V0)."""
    if language not in _VALID_LINT_LANGUAGES:
        valid = ", ".join(sorted(_VALID_LINT_LANGUAGES))
        return (
            f"lint_check: invalid language {language!r}. "
            f"Valid options: {valid}."
        )
    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)
        kind_prefix = (
            "pylint" if language == "python"
            else "eslint" if language == "javascript"
            else "static-xml"
        )
        rules = session.run("""
            MATCH (l:LintRule {odoo_version: $v})
            WHERE l.kind STARTS WITH $kp
            RETURN l.rule_id AS rule_id,
                   l.severity AS severity,
                   l.message AS message,
                   l.kind AS kind
        """, v=odoo_version, kp=kind_prefix).data()
        curate_rec = session.run("""
            MATCH (sm:SpecMetadata {kind: 'lint', odoo_version: $v})
            RETURN sm.curate_status AS curate_status
        """, v=odoo_version).single()
        curate_status = curate_rec["curate_status"] if curate_rec else None
    violations = [r for r in rules if _match_lint_rule(code, r)]
    result = _format_lint_check(violations, odoo_version, code, language)
    if curate_status == "pending":
        result = (
            f"ℹ Spec data v{odoo_version} pending curation — limited results.\n" + result
        )
    return result


@mcp.tool(**READONLY_TOOL_KWARGS)
def find_deprecated_usage(
    odoo_version: str = "auto",
    kind: str | None = None,
    profile_name: str | None = None,
) -> str:
    """Scan indexed code for methods that call deprecated or removed Odoo APIs.

    If you've memorized which Odoo APIs are deprecated, prefer this tool
    instead — it returns ground-truth scans of the indexed corpus.

    TRIGGER when: "find deprecated API usage in my codebase", "which modules
    use old-style _columns", "upgrade risk scan", "code nào dùng API cũ sắp bị
    xóa", "kiểm tra deprecated usage trước khi upgrade", "what needs to change
    before upgrading to Odoo 18"
    PREFER over: manual search — cross-repo scan with version-aware deprecation
    database, shows replacement for each hit
    SKIP when: user wants full API reference for one symbol → use lookup_core_api;
    user wants version-level diff → use api_version_diff

    Args:
        odoo_version: e.g. '17.0', '18.0'. Default 'auto'.
        kind: Optional filter — restrict to one CoreSymbol.kind
            (e.g. 'orm_method', 'function').
        profile_name: Optional profile filter (e.g. 'viindoo_internal_17').
            When set, only Method nodes whose profile array contains this name
            are scanned. Default None scans across all profiles.

    Returns:
        Tree text grouped by module → model.method → deprecated symbol →
        replacement. Use BEFORE upgrading to plan code changes.

    Example:
        find_deprecated_usage("18.0")
        → find_deprecated_usage(Odoo 18.0) — 12 hits
          ├─ [viin_sale] sale.order.legacy_label
          │   ├─ uses: odoo.models.BaseModel.name_get (status=deprecated)
          │   └─ replacement: odoo.models.BaseModel.display_name
    """
    return _find_deprecated_usage(odoo_version, kind=kind, profile_name=profile_name)


@mcp.tool(**READONLY_TOOL_KWARGS)
def lint_check(
    code: str, odoo_version: str = "auto", language: str = "python",
) -> str:
    """Check a code snippet against indexed Odoo-specific lint rules (V0 fuzzy).

    TRIGGER when: "lint check this module", "OCA style violations in module X",
    "check coding standards", "module X có vi phạm coding convention không",
    "kiểm tra code quality", "does this code follow Odoo guidelines"
    PREFER over: running ruff/pylint directly — applies Odoo-specific lint rules
    from indexed LintRule catalogue, not generic Python linters
    SKIP when: user wants deprecated API scan → use find_deprecated_usage;
    user wants module existence check → use check_module_exists

    Args:
        code: Source code chunk to check.
        odoo_version: e.g. '17.0', '18.0'. Default 'auto'.
        language: 'python' | 'javascript' | 'xml'.

    Returns:
        Tree text listing matched rule violations (rule_id, severity, message).
        V0 matcher is fuzzy token-overlap — use as first-pass screen, not as
        authoritative pylint/ruff/eslint output.

    Example:
        lint_check("raise UserError('Hello %s' % name)", "17.0", "python")
        → lint_check(Odoo 17.0, language=python) — 1 violations
          └─ E8502 (error): Bad usage of _, _lt function...
    """
    return _lint_check(code, odoo_version, language)


def _format_cli_flag_detail(rec: dict, replacement: str | None, version: str) -> str:
    """Format a single CLIFlag detail."""
    flag = rec.get("flag_name") or "?"
    cmd = rec.get("command_name") or "?"
    status = rec.get("status") or "stable"
    typ = rec.get("type")
    default = rec.get("default")
    help_text = rec.get("help")
    lines = [f"cli_help({cmd!r}, {flag!r}, Odoo {version})"]
    lines.append(f"├─ Status:      {status}")
    if typ:
        lines.append(f"├─ Type:        {typ}")
    if default is not None:
        lines.append(f"├─ Default:     {default}")
    if help_text:
        lines.append(f"├─ Help:        {help_text}")
    if replacement:
        lines.append(f"└─ Replacement: {replacement}")
    else:
        lines[-1] = lines[-1].replace("├─", "└─")
    return "\n".join(lines)


def _format_cli_command_summary(
    cmd_rec: dict, flags: list[dict], version: str,
) -> str:
    name = cmd_rec.get("name") or "?"
    desc = cmd_rec.get("description")
    lines = [f"cli_help({name!r}, Odoo {version})"]
    if desc:
        lines.append(f"├─ Description: {desc}")
    if not flags:
        lines.append("└─ no flags indexed")
        return "\n".join(lines)
    # ADR-0023 §1.3: Flags is the last branch → sublist indent is 4 spaces.
    lines.append(f"└─ Flags ({len(flags)}):")
    last_idx = len(flags) - 1
    for i, f in enumerate(flags):
        connector = "└─" if i == last_idx else "├─"
        flag = f.get("flag_name") or "?"
        status = f.get("status") or "stable"
        suffix = f" (status={status})" if status != "stable" else ""
        lines.append(f"    {connector} {flag}{suffix}")
    return "\n".join(lines)


def _cli_help(
    command: str | None,
    flag: str | None = None,
    odoo_version: str = "auto",
) -> str:
    """Return CLICommand spec or CLIFlag status + replacement."""
    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        # Query SpecMetadata curation status for CLI at this version.
        curate_rec = session.run("""
            MATCH (sm:SpecMetadata {kind: 'cli', odoo_version: $v})
            RETURN sm.curate_status AS curate_status
        """, v=odoo_version).single()
        curate_status = curate_rec["curate_status"] if curate_rec else None

        if command and flag:
            rec = session.run("""
                MATCH (f:CLIFlag {flag_name: $flag, command_name: $cmd, odoo_version: $v})
                OPTIONAL MATCH (f)-[:REPLACED_BY]->(repl:CLIFlag)
                RETURN f.flag_name AS flag_name,
                       f.command_name AS command_name,
                       f.status AS status,
                       f.type AS type,
                       f.default AS default,
                       f.help AS help,
                       repl.flag_name AS replacement
            """, flag=flag, cmd=command, v=odoo_version).single()
            if rec is None:
                result = (
                    f"cli_help({command!r}, {flag!r}, Odoo {odoo_version})\n"
                    f"└─ flag {flag!r} not found on command {command!r}"
                )
            else:
                data = dict(rec)
                replacement = data.pop("replacement", None)
                # Fallback: replacement_flag_name property when no REPLACED_BY edge.
                if not replacement:
                    fallback = session.run("""
                        MATCH (f:CLIFlag {flag_name: $flag, command_name: $cmd,
                                          odoo_version: $v})
                        RETURN f.replacement_flag_name AS r
                    """, flag=flag, cmd=command, v=odoo_version).single()
                    replacement = fallback["r"] if fallback else None
                result = _format_cli_flag_detail(data, replacement, odoo_version)
            if curate_status == "pending":
                result = (
                    f"ℹ Spec data v{odoo_version} pending curation — limited results.\n"
                    + result
                )
            return result

        if command:
            cmd_rec = session.run("""
                MATCH (c:CLICommand {name: $cmd, odoo_version: $v})
                RETURN c.name AS name, c.description AS description
            """, cmd=command, v=odoo_version).single()
            if cmd_rec is None:
                result = (
                    f"cli_help({command!r}, Odoo {odoo_version})\n"
                    f"└─ command {command!r} not found"
                )
            else:
                flags = session.run("""
                    MATCH (f:CLIFlag {command_name: $cmd, odoo_version: $v})
                    RETURN f.flag_name AS flag_name, f.status AS status
                    ORDER BY f.flag_name
                """, cmd=command, v=odoo_version).data()
                result = _format_cli_command_summary(dict(cmd_rec), flags, odoo_version)
            if curate_status == "pending":
                result = (
                    f"ℹ Spec data v{odoo_version} pending curation — limited results.\n"
                    + result
                )
            return result

        # No command — list all CLI commands at this version.
        cmds = session.run("""
            MATCH (c:CLICommand {odoo_version: $v})
            RETURN c.name AS name
            ORDER BY c.name
        """, v=odoo_version).data()
    if not cmds:
        result = (
            f"cli_help(Odoo {odoo_version})\n"
            f"└─ no CLI commands indexed for this version"
        )
        if curate_status == "pending":
            result = (
                f"ℹ Spec data v{odoo_version} pending curation — limited results.\n"
                + result
            )
        return result
    lines = [f"cli_help(Odoo {odoo_version}) — {len(cmds)} commands"]
    last_idx = len(cmds) - 1
    for i, c in enumerate(cmds):
        connector = "└─" if i == last_idx else "├─"
        lines.append(f"{connector} {c['name']}")
    result = "\n".join(lines)
    if curate_status == "pending":
        result = (
            f"ℹ Spec data v{odoo_version} pending curation — limited results.\n" + result
        )
    return result


@mcp.tool(**READONLY_TOOL_KWARGS)
def cli_help(
    command: str | None = None,
    flag: str | None = None,
    odoo_version: str = "auto",
) -> str:
    """Look up odoo-bin subcommand or flag: status, help text, replacement.

    TRIGGER when: "how to run odoo-bin scaffold", "what CLI options does
    odoo-bin have", "odoo-bin command for database update", "cách dùng
    odoo-bin shell", "tham số nào để cài module mới", "is --longpolling-port
    still valid in Odoo 18"
    PREFER over: reading Odoo docs — returns version-specific CLI info from
    indexed CLICommand catalogue, including deprecated flag replacements
    SKIP when: user wants API reference → use lookup_core_api; user wants to
    check module existence → use check_module_exists

    Args:
        command: Subcommand name (e.g. 'server', 'shell', 'scaffold').
            If None, lists all known commands at this version.
        flag: Optional flag (e.g. '--http-port'). With command, returns full
            flag details including replacement when deprecated.
        odoo_version: e.g. '17.0', '18.0'. Default 'auto'.

    Returns:
        Tree text: flag status, type, default, help text, replacement.

    Example:
        cli_help("server", "--longpolling-port", "18.0")
        → cli_help('server', '--longpolling-port', Odoo 18.0)
          ├─ Status:      removed
          ├─ Help:        Deprecated alias to the gevent-port option
          └─ Replacement: --gevent-port
    """
    return _cli_help(command, flag, odoo_version)


@mcp.tool(**READONLY_TOOL_KWARGS)
def api_version_diff(symbol: str, from_version: str, to_version: str) -> str:
    """Diff a single Odoo core API symbol between two indexed versions.

    TRIGGER when: "what changed in Odoo 17 vs 16 API", "new decorators in
    version 17", "breaking changes between versions", "API nào bị xóa từ v16
    sang v17", "tính năng mới trong Odoo 17", "did name_get change from 17 to 18"
    PREFER over: reading changelogs — structured diff of CoreSymbol additions,
    removals, deprecations, and signature changes per version
    SKIP when: user wants runtime deprecated usage → use find_deprecated_usage;
    user wants full API reference for one version → use lookup_core_api

    Args:
        symbol: Symbol name (full qualified or short).
        from_version: Older Odoo version, e.g. '16.0'.
        to_version: Newer Odoo version, e.g. '17.0'.

    Returns:
        Tree text: added/removed/stable status, old and new signatures,
        replacement symbol if applicable.

    Example:
        api_version_diff("name_get", "17.0", "18.0")
        → api_version_diff('name_get': 17.0 → 18.0)
          ├─ Status:    removed in 18.0
          ├─ Was:       name_get(self)
          └─ Replaced by: odoo.models.BaseModel.display_name
    """
    return _api_version_diff(symbol, from_version, to_version)


# --- M4.6 Pattern Wow tools -------------------------------------------------

_VALID_PATTERN_LANGUAGES = ("python", "xml", "js", "all")


def _suggest_pattern(
    intent: str,
    odoo_version: str = "auto",
    language: str = "python",
    limit: int = 5,
    *,
    _driver=None,
    _pg_conn=None,
    _embedder=None,
) -> str:
    """ANN-rank curated PatternExample chunks by intent string.

    Per ADR-0003: pgvector ANN over embeddings (chunk_type='pattern_example') →
    Neo4j batch fetch metadata via UNWIND on pattern_id list. Language filter
    via entity_name slug LIKE '<language>__%'.
    """
    if not intent.strip():
        return (
            "suggest_pattern: intent is required (empty input).\n"
            "Hint: pass a natural-language description, e.g. "
            "'computed field cross-model partner'."
        )
    if language not in _VALID_PATTERN_LANGUAGES:
        valid = ", ".join(_VALID_PATTERN_LANGUAGES)
        return (
            f"suggest_pattern: invalid language={language!r}. Valid: {valid}."
        )

    from src.embedding.instructions import INSTRUCT_NL_TO_CODE

    driver = _driver or _get_driver()
    try:
        embedder = _embedder or _get_embedder()
    except Exception as e:
        return (
            f"suggest_pattern: embedder unavailable — {type(e).__name__}: {e}\n"
            "Hint: check Ollama is running (default: http://localhost:11434)."
        )

    with driver.session() as session:
        v = _resolve_version(odoo_version, session)

    try:
        intent_vec = embedder.embed([INSTRUCT_NL_TO_CODE + intent])[0]
    except Exception as e:
        return (
            f"suggest_pattern: embedding query failed — {type(e).__name__}: {e}"
        )

    # Use injected connection (test path) or check out from pool (production).
    _pg_ctx = nullcontext(_pg_conn) if _pg_conn is not None else _checkout_pg()
    with _pg_ctx as pg:
        with pg.cursor() as cur:
            if language == "all":
                cur.execute(
                    """SELECT entity_name, file_path,
                              1 - (vec <=> %s::vector) AS cosine
                       FROM embeddings
                       WHERE chunk_type = 'pattern_example'
                         AND module = '__patterns__'
                       ORDER BY vec <=> %s::vector
                       LIMIT %s""",
                    [intent_vec, intent_vec, limit],
                )
            else:
                cur.execute(
                    """SELECT entity_name, file_path,
                              1 - (vec <=> %s::vector) AS cosine
                       FROM embeddings
                       WHERE chunk_type = 'pattern_example'
                         AND module = '__patterns__'
                         AND entity_name LIKE %s
                       ORDER BY vec <=> %s::vector
                       LIMIT %s""",
                    [intent_vec, f"{language}__%", intent_vec, limit],
                )
            ranked = cur.fetchall()

    if not ranked:
        next_line = format_next_step([
            f"find_examples(query='{intent}', odoo_version='{v}')"
            " for real-world variants",
        ])
        return (
            f"suggest_pattern({intent!r}, {v!r}, language={language})\n"
            "├─ no patterns indexed. Run: "
            "python -m src.indexer.seed_patterns\n"
            + next_line
        )

    # Decode pattern_id from entity_name slug (<language>__<id>)
    pattern_ids = []
    score_map: dict[str, float] = {}
    for entity_name, _file, cosine in ranked:
        if "__" in entity_name:
            _lang, pid = entity_name.split("__", 1)
        else:
            pid = entity_name
        pattern_ids.append(pid)
        score_map[pid] = float(cosine)

    with driver.session() as session:
        records = session.run("""
            UNWIND $ids AS pid
            MATCH (p:PatternExample {pattern_id: pid})
            RETURN p.pattern_id AS id, p.intent_keywords AS kw,
                   p.file_ref AS fr, p.snippet_text AS sn,
                   p.gotchas AS g, p.language AS lang,
                   p.odoo_version_min AS vmin
        """, ids=pattern_ids).data()

    by_id = {r["id"]: r for r in records}
    return _format_suggest_pattern(
        ordered_ids=pattern_ids, by_id=by_id, score_map=score_map,
        intent=intent, version=v, language=language,
    )


def _format_suggest_pattern(
    *, ordered_ids: list[str], by_id: dict[str, dict],
    score_map: dict[str, float], intent: str, version: str, language: str,
) -> str:
    lines = [
        f"suggest_pattern({intent!r}, {version}, language={language}) "
        f"— {len(ordered_ids)} matches",
    ]
    # Wave 5: all pattern branches become ├─ so the Next: footer is the
    # final └─ (ADR-0023 §4).
    for i, pid in enumerate(ordered_ids):
        rec = by_id.get(pid)
        if not rec:
            continue
        connector = "├─"
        score = score_map.get(pid, 0.0)
        lines.append(f"{connector} #{i + 1} · score {score:.2f} · {pid}")
        prefix = "│   "
        lines.append(f"{prefix}├─ Language: {rec['lang']} (min v{rec['vmin']})")
        lines.append(f"{prefix}├─ File:     {rec['fr']}")
        snippet_lines = (rec.get("sn") or "").splitlines()
        if snippet_lines:
            lines.append(f"{prefix}├─ Snippet:")
            # Snippet is a non-last child → sublist indent is "│   " (4 chars).
            for sl in snippet_lines[:SNIPPET_PREVIEW_MAX_LINES]:
                lines.append(f"{prefix}│   {sl}")
            if len(snippet_lines) > SNIPPET_PREVIEW_MAX_LINES:
                extra = len(snippet_lines) - SNIPPET_PREVIEW_MAX_LINES
                lines.append(f"{prefix}│   ... ({extra} more lines)")
        gotchas = rec.get("g") or []
        if gotchas:
            lines.append(f"{prefix}└─ Gotchas:")
            # Gotchas is the last child → sublist indent is "    " (4 spaces).
            for g in gotchas:
                lines.append(f"{prefix}    • {g}")
    lines.append(format_next_step([
        f"find_examples(query='{intent}', odoo_version='{version}')"
        " for real-world variants",
    ]))
    return "\n".join(lines)


def _check_module_exists(
    name: str, odoo_version: str = "auto", *,
    profile_name: str | None = None,
    _driver=None,
) -> str:
    """Report whether `name` is indexed + flag EE-confusion (per ADR-0003 §2).

    Edition-first strategy: query Neo4j for indexed edition (OEEL-1 detected),
    fallback to hardcoded dict if not indexed. Both paths produce same EE warning.
    """
    from src.data.ee_modules import EE_CONFUSION

    driver = _driver or _get_driver()
    with driver.session() as session:
        v = _resolve_version(odoo_version, session)
        rec = session.run("""
            MATCH (m:Module {name: $n, odoo_version: $v})
            WHERE ($profile_name IS NULL OR $profile_name IN m.profile)
            RETURN m.edition AS edition,
                   m.viindoo_equivalent_qname AS vvq,
                   m.repo AS repo
        """, n=name, v=v, profile_name=profile_name).single()

    indexed = rec is not None
    edition = rec["edition"] if rec else None
    repo = rec.get("repo") if rec else None
    vvq_db = rec.get("vvq") if rec else None

    # Edition-first: check Neo4j for 'enterprise' (from OEEL-1 license detection)
    is_ee_confusion = False
    ee_source = ""  # track source for output messaging
    viindoo_equivalent = None

    if indexed and edition == "enterprise":
        # Indexed data has OEEL-1 → is EE module
        is_ee_confusion = True
        ee_source = "indexed"
        viindoo_equivalent = vvq_db or EE_CONFUSION.get(name)
    elif name in EE_CONFUSION:
        # Not indexed (or not marked 'enterprise') but in hardcoded dict
        is_ee_confusion = True
        ee_source = "dict"
        viindoo_equivalent = EE_CONFUSION.get(name)

    return _format_check_module_exists(
        name=name, version=v, indexed=indexed, edition=edition, repo=repo,
        is_ee_confusion=is_ee_confusion, viindoo_equivalent=viindoo_equivalent,
        ee_source=ee_source,
    )


def _format_check_module_exists(
    *, name: str, version: str, indexed: bool, edition: str | None,
    repo: str | None, is_ee_confusion: bool, viindoo_equivalent: str | None,
    ee_source: str = "",
) -> str:
    lines = [f"check_module_exists({name!r}, {version})"]
    lines.append(f"├─ Indexed:         {'Yes' if indexed else 'No'}")
    if indexed and edition:
        repo_suffix = f" [{repo}]" if repo else ""
        lines.append(f"├─ Edition:         {edition}{repo_suffix}")
    lines.append(
        f"├─ Is EE confusion: {'Yes' if is_ee_confusion else 'No'}"
    )
    if is_ee_confusion:
        if viindoo_equivalent:
            lines.append(f"├─ Viindoo equiv:   {viindoo_equivalent}")
        else:
            lines.append("├─ Viindoo equiv:   (none — feature not in Viindoo stack)")
        # Differentiate source for debugging
        source_hint = ""
        if ee_source == "indexed":
            source_hint = " (license=OEEL-1)"
        elif ee_source == "dict":
            source_hint = " (legacy hardcoded dict)"
        # ADR-0023 §2: English-only tool output.
        lines.append(
            f"├─ ⚠ WARNING: this is an Odoo Enterprise module{source_hint}. "
            "Do NOT depend on it in a Viindoo Community stack — "
            "this violates the GPL/Enterprise license boundary."
        )
    elif not indexed:
        # ADR-0023 §4: NO branch is terminal (no useful drill-down).
        lines.append(
            "└─ Hint: module not indexed in this profile. "
            "If it should be, run: python -m src.indexer index-repo --profile <name>"
        )
        return "\n".join(lines)
    # Wave 5: YES branch emits Next: footer (ADR-0023 §4).
    lines.append(format_next_step([
        f"describe_module(name='{name}', odoo_version='{version}')"
        " for full overview",
    ]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Wave 1 — new list_* / describe_module / UI tools (ADR-0023 §5).
# Read-only Cypher; share the _render_capped / format_next_step helpers.
# All tree text English-only per ADR-0023 §2.
# ---------------------------------------------------------------------------


def _describe_module(
    name: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
) -> str:
    """Layer-0 module overview: manifest + model/view/JS counts.

    Distinct from check_module_exists (1–3 lines, YES/NO + edition) — this
    tool returns the full architecture tree (~10–15 lines) per ADR-0023 §1.7.
    Runs 1 Module query + 4 aggregate queries (Models defined, Models
    extended, Views by type, JS patches).
    """
    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        mod_rec = session.run(
            """
            MATCH (m:Module {name: $n, odoo_version: $v})
            WHERE ($profile_name IS NULL OR $profile_name IN m.profile)
            RETURN m.repo AS repo, m.path AS path, m.version_raw AS version_raw,
                   m.edition AS edition,
                   m.viindoo_equivalent_qname AS vvq
            """,
            n=name, v=odoo_version, profile_name=profile_name,
        ).single()

        if not mod_rec:
            return (
                f"No module named '{name}' indexed for Odoo {odoo_version}."
            )

        depends = session.run(
            f"""
            MATCH (m:Module {{name: $n, odoo_version: $v}})
                  -[:{REL_DEPENDS_ON}]->(d:Module)
            RETURN d.name AS name
            ORDER BY d.name ASC
            """,
            n=name, v=odoo_version,
        ).data()

        defines = session.run(
            """
            MATCH (model:Model {module: $n, odoo_version: $v})
            WHERE coalesce(model.is_definition, false) = true
              AND model.module <> '__unresolved__'
              AND ($profile_name IS NULL OR $profile_name IN model.profile)
            RETURN model.name AS name
            ORDER BY model.name ASC
            """,
            n=name, v=odoo_version, profile_name=profile_name,
        ).data()

        extends = session.run(
            """
            MATCH (model:Model {module: $n, odoo_version: $v})
            WHERE coalesce(model.is_definition, false) = false
              AND model.module <> '__unresolved__'
              AND ($profile_name IS NULL OR $profile_name IN model.profile)
            RETURN model.name AS name
            ORDER BY model.name ASC
            """,
            n=name, v=odoo_version, profile_name=profile_name,
        ).data()

        view_breakdown = session.run(
            """
            MATCH (view:View {module: $n, odoo_version: $v})
            WHERE ($profile_name IS NULL OR $profile_name IN view.profile)
            RETURN view.type AS type, count(view) AS c
            ORDER BY c DESC, type ASC
            """,
            n=name, v=odoo_version, profile_name=profile_name,
        ).data()

        js_count = session.run(
            """
            MATCH (j:JSPatch {module: $n, odoo_version: $v})
            WHERE ($profile_name IS NULL OR $profile_name IN j.profile)
            RETURN count(j) AS c
            """,
            n=name, v=odoo_version, profile_name=profile_name,
        ).single()["c"]

    lines = [f"{name} (Odoo {odoo_version})"]

    # Manifest sub-tree (non-last parent → "│   " sublist indent).
    lines.append("├─ Manifest:")
    manifest_rows: list[tuple[str, str]] = []
    if depends:
        # Inline list with cap (no extra disclosure — depends is rarely > 20).
        dep_names = ", ".join(d["name"] for d in depends[:20])
        if len(depends) > 20:
            dep_names += f", ... and {len(depends) - 20} more"
        manifest_rows.append(("Depends", dep_names))
    else:
        manifest_rows.append(("Depends", "—"))
    edition_str = mod_rec.get("edition") or "community"
    if mod_rec.get("vvq"):
        edition_str += f" (Viindoo equivalent: {mod_rec['vvq']})"
    manifest_rows.append(("Edition", edition_str))
    manifest_rows.append(("Version", mod_rec.get("version_raw") or "—"))
    last_m = len(manifest_rows) - 1
    for i, (label, value) in enumerate(manifest_rows):
        conn = "└─" if i == last_m else "├─"
        lines.append(f"│   {conn} {label}: {value}")

    # Defines models — count + capped inline preview.
    def_total = len(defines)
    if def_total > 0:
        def_preview_names = [d["name"] for d in defines[:LIST_PREVIEW_MAX_ITEMS]]
        def_preview = ", ".join(def_preview_names)
        if def_total > LIST_PREVIEW_MAX_ITEMS:
            overflow = def_total - LIST_PREVIEW_MAX_ITEMS
            first_def = defines[0]["name"]
            def_preview += (
                f", ... and {overflow} more"
                f" (use list_fields(model='{first_def}', module='{name}',"
                f" odoo_version='{odoo_version}'))"
            )
        lines.append(f"├─ Defines models: {def_total} ({def_preview})")
    else:
        lines.append("├─ Defines models: 0")

    # Extends models — count + capped inline preview.
    ext_total = len(extends)
    if ext_total > 0:
        ext_preview_names = [e["name"] for e in extends[:LIST_PREVIEW_MAX_ITEMS]]
        ext_preview = ", ".join(ext_preview_names)
        if ext_total > LIST_PREVIEW_MAX_ITEMS:
            overflow = ext_total - LIST_PREVIEW_MAX_ITEMS
            first_ext = extends[0]["name"]
            ext_preview += (
                f", ... and {overflow} more"
                f" (use list_fields(model='{first_ext}', module='{name}',"
                f" odoo_version='{odoo_version}'))"
            )
        lines.append(f"├─ Extends models: {ext_total} ({ext_preview})")
    else:
        lines.append("├─ Extends models: 0")

    # Views — total + by-type breakdown.
    view_total = sum(row["c"] for row in view_breakdown)
    if view_total > 0:
        breakdown_str = ", ".join(
            f"{row['c']} {row['type'] or 'unknown'}" for row in view_breakdown
        )
        lines.append(f"├─ Views: {view_total} ({breakdown_str})")
    else:
        lines.append("├─ Views: 0")

    # JS patches — last data branch. Marked ├─ so Wave 5 can append Next: footer.
    lines.append(f"├─ JS patches: {js_count}")

    # Wave 5: Next-step footer per ADR-0023 §4. Prefer the first defined model
    # (drill into its fields/views); fall back to extends if no defined model.
    # NOTE: cannot suggest check_module_exists (regression per §4.2 alignment).
    first_target = None
    if defines:
        first_target = defines[0]["name"]
    elif extends:
        first_target = extends[0]["name"]
    if first_target:
        next_hints = [
            f"list_fields(model='{first_target}', module='{name}'"
            f", odoo_version='{odoo_version}') for declared fields",
            f"list_views(model='{first_target}', odoo_version='{odoo_version}')"
            " for module views",
        ]
    else:
        # No models defined or extended — skip footer entirely (no useful drill-down).
        next_hints = []
    if footer := format_next_step(next_hints):
        lines.append(footer)

    return "\n".join(lines)


def _list_fields(
    model: str,
    odoo_version: str = "auto",
    module: str | None = None,
    kind: str | None = None,
    profile_name: str | None = None,
    limit: int = 200,
    start_index: int = 0,
    api_key_id: str = _ANONYMOUS_API_KEY_ID,
) -> str:
    """Layer-2 — enumerate fields on a model, grouped by module.

    `kind` filters by Field.ttype (e.g. 'monetary', 'many2one').
    `module` restricts to one declaring module.
    `limit` caps the Cypher query size; the render cap is LIST_PREVIEW_FIELDS_MAX.
    `start_index` is a zero-based pagination cursor (Cypher SKIP).
    `api_key_id` scopes minted refs to the calling tenant (default: 'anonymous').
    """
    cap = LIST_PREVIEW_FIELDS_MAX
    # Fetch at most cap rows via Cypher with SKIP for pagination.
    effective_limit = min(limit, cap)

    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        rows = session.run(
            f"""
            MATCH (f:Field {{model: $m, odoo_version: $v}})
            WHERE ($profile_name IS NULL OR $profile_name IN f.profile)
              AND ($module IS NULL OR f.module = $module)
              AND ($kind IS NULL OR f.ttype = $kind)
              AND f.module <> '__unresolved__'
            OPTIONAL MATCH (mod:Module {{name: f.module, odoo_version: $v}})
            WITH f, mod,
                 {_edition_rank_cypher("mod")},
                 mod.name AS mod_name
            RETURN f.name AS name, f.ttype AS ttype,
                   f.module AS module, mod.repo AS repo,
                   edition_rank, mod_name
            ORDER BY edition_rank ASC, mod_name ASC, f.name ASC
            SKIP $skip
            LIMIT $limit
            """,
            m=model, v=odoo_version, module=module, kind=kind,
            profile_name=profile_name, skip=start_index, limit=effective_limit,
        ).data()

        # Separate count query so we know the true total when Cypher SKIP/LIMIT trims.
        total_rec = session.run(
            """
            MATCH (f:Field {model: $m, odoo_version: $v})
            WHERE ($profile_name IS NULL OR $profile_name IN f.profile)
              AND ($module IS NULL OR f.module = $module)
              AND ($kind IS NULL OR f.ttype = $kind)
              AND f.module <> '__unresolved__'
            RETURN count(f) AS c
            """,
            m=model, v=odoo_version, module=module, kind=kind,
            profile_name=profile_name,
        ).single()
        total = total_rec["c"] if total_rec else 0

    header = f"Fields of {model} (Odoo {odoo_version})"
    if total == 0:
        # Wave 5: Next-step footer (empty result still gets a sensible hint).
        next_line = format_next_step([
            f"list_methods(model='{model}', odoo_version='{odoo_version}')"
            " for behavior",
        ])
        return f"{header}\n├─ (none)\n{next_line}"

    # Mint opaque refs for each returned row (field kind).
    field_items = [{"field_name": r["name"], "model": model} for r in rows]
    ref_ids = mint_refs(field_items, api_key_id, kind="field")

    # Group rows by (repo, module) preserving order.
    groups: dict[tuple[str, str], list[tuple[dict, str]]] = {}
    order: list[tuple[str, str]] = []
    for r, ref_id in zip(rows, ref_ids):
        key = (r.get("repo") or "?", r.get("module") or "?")
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((r, ref_id))

    lines = [header]
    for key in order:
        repo, mod_name = key
        lines.append(f"├─ [{repo}] {mod_name}")
        sub_items = groups[key]
        more_hint = (
            f"list_fields(model='{model}', odoo_version='{odoo_version}'"
            f", limit={max(limit * 2, total)}) for full list"
        )
        # Build rendered strings with inline refs.
        raw_rows = [r for r, _ in sub_items]
        rendered_strs = _render_capped(
            raw_rows,
            lambda r: f"{r['name']} : {r['ttype']}",
            cap=cap,
            more_hint=more_hint,
        )
        # Inject [ref=fN] prefix for non-hint rows.
        ref_iter = iter([ref_id for _, ref_id in sub_items])
        tagged: list[str] = []
        for row_str in rendered_strs:
            if row_str.startswith("... and "):
                tagged.append(row_str)
            else:
                ref_id = next(ref_iter, None)
                prefix = f"[ref={ref_id}] " if ref_id else ""
                tagged.append(f"{prefix}{row_str}")
        lines.extend(render_list_block(tagged))

    shown = len(rows)
    end_index = start_index + shown
    has_more = total > end_index

    if has_more:
        # Pagination continuation hint (plain text, NOT <error> tag — ADR-0023
        # §Appendix B item #2: pagination is routine, not failure).
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total}."
            f" Call list_fields(model='{model}', odoo_version='{odoo_version}',"
            f" start_index={end_index}) for next {min(cap, total - end_index)}."
        )
    elif start_index > 0:
        # Final page of a paginated sequence — disclose position.
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total} (last page)."
        )

    # Wave 5: Next-step footer per ADR-0023 §4. Drill into the first
    # rendered field for its full chain, and into list_methods for behavior.
    first_field = rows[0]["name"] if rows else None
    next_hints: list[str] = []
    if first_field:
        next_hints.append(
            f"resolve_field(model_name='{model}', field_name='{first_field}'"
            f", odoo_version='{odoo_version}') for full chain",
        )
    next_hints.append(
        f"list_methods(model='{model}', odoo_version='{odoo_version}')"
        " for behavior",
    )
    lines.append(format_next_step(next_hints))
    return "\n".join(lines)


def _list_methods(
    model: str,
    odoo_version: str = "auto",
    module: str | None = None,
    profile_name: str | None = None,
    limit: int = 200,
    start_index: int = 0,
    api_key_id: str = _ANONYMOUS_API_KEY_ID,
) -> str:
    """Layer-4 — enumerate methods on a model, grouped by module.

    Methods appearing in ≥2 modules for the same model are marked with `(*)`
    per ADR-0023 §5.3 to flag override-points.
    `start_index` is a zero-based pagination cursor (Cypher SKIP).
    `api_key_id` scopes minted refs to the calling tenant (default: 'anonymous').
    """
    cap = LIST_PREVIEW_MAX_ITEMS
    # Fetch at most cap rows via Cypher with SKIP for pagination.
    effective_limit = min(limit, cap)

    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        rows = session.run(
            f"""
            MATCH (mth:Method {{model: $m, odoo_version: $v}})
            WHERE ($profile_name IS NULL OR $profile_name IN mth.profile)
              AND ($module IS NULL OR mth.module = $module)
              AND mth.module <> '__unresolved__'
            OPTIONAL MATCH (mod:Module {{name: mth.module, odoo_version: $v}})
            WITH mth, mod,
                 {_edition_rank_cypher("mod")},
                 mod.name AS mod_name
            RETURN mth.name AS name, mth.convention_kind AS kind,
                   mth.module AS module, mod.repo AS repo,
                   edition_rank, mod_name
            ORDER BY edition_rank ASC, mod_name ASC, mth.name ASC
            SKIP $skip
            LIMIT $limit
            """,
            m=model, v=odoo_version, module=module,
            profile_name=profile_name, skip=start_index, limit=effective_limit,
        ).data()

        total_rec = session.run(
            """
            MATCH (mth:Method {model: $m, odoo_version: $v})
            WHERE ($profile_name IS NULL OR $profile_name IN mth.profile)
              AND ($module IS NULL OR mth.module = $module)
              AND mth.module <> '__unresolved__'
            RETURN count(mth) AS c
            """,
            m=model, v=odoo_version, module=module,
            profile_name=profile_name,
        ).single()
        total = total_rec["c"] if total_rec else 0

        # Override-marker: count distinct modules per method name on this model.
        override_rec = session.run(
            """
            MATCH (mth:Method {model: $m, odoo_version: $v})
            WHERE ($profile_name IS NULL OR $profile_name IN mth.profile)
              AND mth.module <> '__unresolved__'
            WITH mth.name AS name, count(DISTINCT mth.module) AS modcount
            WHERE modcount >= 2
            RETURN collect(name) AS overrides
            """,
            m=model, v=odoo_version, profile_name=profile_name,
        ).single()
        override_names = set(override_rec["overrides"]) if override_rec else set()

    header = f"Methods of {model} (Odoo {odoo_version})"
    if total == 0:
        next_line = format_next_step([
            f"list_fields(model='{model}', odoo_version='{odoo_version}')"
            " for shape",
        ])
        return f"{header}\n├─ (none)\n{next_line}"

    # Mint opaque refs for each returned row (method kind).
    method_items = [{"method_name": r["name"], "model": model} for r in rows]
    ref_ids = mint_refs(method_items, api_key_id, kind="method")

    groups: dict[tuple[str, str], list[tuple[dict, str]]] = {}
    order: list[tuple[str, str]] = []
    for r, ref_id in zip(rows, ref_ids):
        key = (r.get("repo") or "?", r.get("module") or "?")
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((r, ref_id))

    lines = [header]
    for key in order:
        repo, mod_name = key
        lines.append(f"├─ [{repo}] {mod_name}")
        sub_indent = "│   "
        sub_items = groups[key]
        more_hint = (
            f"list_methods(model='{model}', odoo_version='{odoo_version}'"
            f", limit={max(limit * 2, total)}) for full list"
        )

        raw_rows = [r for r, _ in sub_items]

        def _fmt_method(r):
            marker = "(*)" if r["name"] in override_names else ""
            kind_str = r.get("kind") or "private"
            return f"{r['name']}{marker} : {kind_str}"

        rendered = _render_capped(raw_rows, _fmt_method, cap=cap, more_hint=more_hint)
        # Inject [ref=mN] prefix for non-hint rows.
        ref_iter = iter([ref_id for _, ref_id in sub_items])
        tagged: list[str] = []
        for row_str in rendered:
            if row_str.startswith("... and "):
                tagged.append(row_str)
            else:
                ref_id = next(ref_iter, None)
                prefix = f"[ref={ref_id}] " if ref_id else ""
                tagged.append(f"{prefix}{row_str}")

        last_r = len(tagged) - 1
        for j, row in enumerate(tagged):
            r_conn = "└─" if j == last_r else "├─"
            lines.append(f"{sub_indent}{r_conn} {row}")

    shown = len(rows)
    end_index = start_index + shown
    has_more = total > end_index

    if has_more:
        # Pagination continuation hint (plain text, NOT <error> tag).
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total}."
            f" Call list_methods(model='{model}', odoo_version='{odoo_version}',"
            f" start_index={end_index}) for next {min(cap, total - end_index)}."
        )
    elif start_index > 0:
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total} (last page)."
        )

    # Wave 5: Next-step footer per ADR-0023 §4.
    first_method = rows[0]["name"] if rows else None
    next_hints: list[str] = []
    if first_method:
        next_hints.append(
            f"resolve_method(model_name='{model}', method_name='{first_method}'"
            f", odoo_version='{odoo_version}') for override chain",
        )
        next_hints.append(
            f"find_override_point(model='{model}', method='{first_method}'"
            f", odoo_version='{odoo_version}') for hook spot",
        )
    if footer := format_next_step(next_hints):
        lines.append(footer)
    return "\n".join(lines)


def _list_views_core(
    *,
    model: str | None = None,
    module: str | None = None,
    odoo_version: str = "auto",
    view_type: str | None = None,
    profile_name: str | None = None,
    limit: int = 200,
    start_index: int = 0,
    api_key_id: str = _ANONYMOUS_API_KEY_ID,
) -> str:
    """Shared core for view listing — takes EITHER model OR module filter (not both).

    `view_type` filters by View.type (form/tree/kanban/search/...).
    `start_index` is a zero-based pagination cursor (Cypher SKIP).
    `api_key_id` scopes minted refs to the calling tenant (default: 'anonymous').
    """
    if (model is None) == (module is None):
        raise ValueError(
            "_list_views_core requires exactly one of model= / module= (not both, not neither)"
        )

    cap = LIST_PREVIEW_MAX_ITEMS
    effective_limit = min(limit, cap)

    is_model_scoped = model is not None

    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        if is_model_scoped:
            rows = session.run(
                f"""
                MATCH (v:View {{model: $filter_val, odoo_version: $ver}})
                WHERE ($profile_name IS NULL OR $profile_name IN v.profile)
                  AND ($view_type IS NULL OR v.type = $view_type)
                  AND v.module <> '__unresolved__'
                OPTIONAL MATCH (mod:Module {{name: v.module, odoo_version: $ver}})
                WITH v, mod,
                     {_edition_rank_cypher("mod")},
                     mod.name AS mod_name
                RETURN v.xmlid AS xmlid, v.type AS type,
                       v.module AS module, mod.repo AS repo,
                       edition_rank, mod_name
                ORDER BY edition_rank ASC, mod_name ASC, v.xmlid ASC
                SKIP $skip
                LIMIT $limit
                """,
                filter_val=model, ver=odoo_version, view_type=view_type,
                profile_name=profile_name, skip=start_index, limit=effective_limit,
            ).data()

            total_rec = session.run(
                """
                MATCH (v:View {model: $filter_val, odoo_version: $ver})
                WHERE ($profile_name IS NULL OR $profile_name IN v.profile)
                  AND ($view_type IS NULL OR v.type = $view_type)
                  AND v.module <> '__unresolved__'
                RETURN count(v) AS c
                """,
                filter_val=model, ver=odoo_version, view_type=view_type,
                profile_name=profile_name,
            ).single()
        else:
            rows = session.run(
                f"""
                MATCH (v:View {{module: $filter_val, odoo_version: $ver}})
                WHERE ($profile_name IS NULL OR $profile_name IN v.profile)
                  AND ($view_type IS NULL OR v.type = $view_type)
                  AND v.module <> '__unresolved__'
                OPTIONAL MATCH (mod:Module {{name: v.module, odoo_version: $ver}})
                WITH v, mod,
                     {_edition_rank_cypher("mod")},
                     mod.name AS mod_name
                RETURN v.xmlid AS xmlid, v.type AS type,
                       v.module AS module, mod.repo AS repo,
                       edition_rank, mod_name
                ORDER BY edition_rank ASC, mod_name ASC, v.xmlid ASC
                SKIP $skip
                LIMIT $limit
                """,
                filter_val=module, ver=odoo_version, view_type=view_type,
                profile_name=profile_name, skip=start_index, limit=effective_limit,
            ).data()

            total_rec = session.run(
                """
                MATCH (v:View {module: $filter_val, odoo_version: $ver})
                WHERE ($profile_name IS NULL OR $profile_name IN v.profile)
                  AND ($view_type IS NULL OR v.type = $view_type)
                  AND v.module <> '__unresolved__'
                RETURN count(v) AS c
                """,
                filter_val=module, ver=odoo_version, view_type=view_type,
                profile_name=profile_name,
            ).single()

        total = total_rec["c"] if total_rec else 0

    if is_model_scoped:
        header = f"Views of {model} (Odoo {odoo_version})"
        empty_hint = (
            f"list_methods(model='{model}', odoo_version='{odoo_version}')"
            " for behavior"
        )
        pager_tool = f"list_views(model='{model}', odoo_version='{odoo_version}'"
    else:
        header = f"Views in module '{module}' (Odoo {odoo_version})"
        empty_hint = (
            f"list_fields(module='{module}', odoo_version='{odoo_version}')"
            " for model fields"
        )
        pager_tool = f"list_views(module='{module}', odoo_version='{odoo_version}'"

    if total == 0:
        next_line = format_next_step([empty_hint])
        return f"{header}\n├─ (none)\n{next_line}"

    # Mint opaque refs for each returned row (view kind).
    view_items = [{"xmlid": r["xmlid"]} for r in rows]
    ref_ids = mint_refs(view_items, api_key_id, kind="view")

    groups: dict[tuple[str, str], list[tuple[dict, str]]] = {}
    order: list[tuple[str, str]] = []
    for r, ref_id in zip(rows, ref_ids):
        key = (r.get("repo") or "?", r.get("module") or "?")
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((r, ref_id))

    lines = [header]
    for key in order:
        repo, mod_name = key
        lines.append(f"├─ [{repo}] {mod_name}")
        sub_indent = "│   "
        sub_items = groups[key]
        more_hint = (
            f"{pager_tool}, limit={max(limit * 2, total)}) for full list"
        )
        raw_rows = [r for r, _ in sub_items]
        rendered = _render_capped(
            raw_rows,
            lambda r: f"{r['xmlid']} : {r.get('type') or 'unknown'}",
            cap=cap,
            more_hint=more_hint,
        )
        # Inject [ref=vN] prefix for non-hint rows.
        ref_iter = iter([ref_id for _, ref_id in sub_items])
        tagged: list[str] = []
        for row_str in rendered:
            if row_str.startswith("... and "):
                tagged.append(row_str)
            else:
                ref_id = next(ref_iter, None)
                prefix = f"[ref={ref_id}] " if ref_id else ""
                tagged.append(f"{prefix}{row_str}")

        last_r = len(tagged) - 1
        for j, row in enumerate(tagged):
            r_conn = "└─" if j == last_r else "├─"
            lines.append(f"{sub_indent}{r_conn} {row}")

    shown = len(rows)
    end_index = start_index + shown
    has_more = total > end_index

    if has_more:
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total}."
            f" Call {pager_tool},"
            f" start_index={end_index}) for next {min(cap, total - end_index)}."
        )
    elif start_index > 0:
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total} (last page)."
        )

    # Wave 5: Next-step footer per ADR-0023 §4.
    first_xmlid = rows[0]["xmlid"] if rows else None
    next_hints: list[str] = []
    if first_xmlid:
        next_hints.append(
            f"resolve_view(xmlid='{first_xmlid}', odoo_version='{odoo_version}')"
            " for full xpath chain",
        )
    if is_model_scoped:
        next_hints.append(
            f"find_examples(query='{model} view', odoo_version='{odoo_version}')"
            " for inheritance patterns",
        )
    else:
        next_hints.append(
            f"find_examples(query='{module} view', odoo_version='{odoo_version}')"
            " for inheritance patterns",
        )
    lines.append(format_next_step(next_hints))
    return "\n".join(lines)


def _list_views(
    model: str,
    odoo_version: str = "auto",
    view_type: str | None = None,
    profile_name: str | None = None,
    limit: int = 200,
    start_index: int = 0,
    api_key_id: str = _ANONYMOUS_API_KEY_ID,
) -> str:
    """Facade: model-scoped view listing (existing API — backward-compatible)."""
    return _list_views_core(
        model=model,
        odoo_version=odoo_version,
        view_type=view_type,
        profile_name=profile_name,
        limit=limit,
        start_index=start_index,
        api_key_id=api_key_id,
    )


def _list_views_by_module(
    module: str,
    odoo_version: str = "auto",
    view_type: str | None = None,
    profile_name: str | None = None,
    limit: int = 200,
    start_index: int = 0,
    api_key_id: str = _ANONYMOUS_API_KEY_ID,
) -> str:
    """Facade: module-scoped view listing (new API for module_inspect router)."""
    return _list_views_core(
        module=module,
        odoo_version=odoo_version,
        view_type=view_type,
        profile_name=profile_name,
        limit=limit,
        start_index=start_index,
        api_key_id=api_key_id,
    )


def _list_owl_components(
    module: str,
    odoo_version: str = "auto",
    bound_model: str | None = None,
    profile_name: str | None = None,
    limit: int = 200,
    start_index: int = 0,
    api_key_id: str = _ANONYMOUS_API_KEY_ID,
) -> str:
    """Layer-5b — enumerate OWL components declared in a module.

    Era-aware: returns empty + warning for Odoo majors <= 13 (Widget era,
    no OWL components). When `bound_model` filter is set, emits a warning
    footer because parser_js.py:415 bound_model resolution is heuristic.
    `start_index` is a zero-based pagination cursor (Cypher SKIP).
    `api_key_id` scopes minted refs to the calling tenant (default: 'anonymous').
    """
    cap = LIST_PREVIEW_MAX_ITEMS
    effective_limit = min(limit, cap)

    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        # Era guard: v8-v13 had Widget, not OWL. Return early with hint.
        try:
            major = int(odoo_version.split(".")[0])
        except (ValueError, AttributeError):
            major = 0
        if major and major <= 13:
            # Wave 5: still emit Next: footer suggesting list_js_patches for
            # era1 widget extensions (the natural era-aware drill-down).
            next_line = format_next_step([
                f"list_js_patches(module='{module}', era='era1'"
                f", odoo_version='{odoo_version}') for legacy widget extends",
            ])
            return (
                f"OWL components of {module} (Odoo {odoo_version})\n"
                "├─ (none) — Warning: No OWL components in v8-v13"
                " (Widget era). Use list_js_patches(era='era1') for legacy"
                " widget extensions.\n"
                + next_line
            )

        rows = session.run(
            """
            MATCH (c:OWLComp {module: $mod, odoo_version: $v})
            WHERE ($profile_name IS NULL OR $profile_name IN c.profile)
              AND ($bound_model IS NULL OR c.bound_model = $bound_model)
              AND c.module <> '__unresolved__'
            RETURN c.name AS name, c.bound_model AS bound_model,
                   c.template AS template
            ORDER BY c.name ASC
            SKIP $skip
            LIMIT $limit
            """,
            mod=module, v=odoo_version, bound_model=bound_model,
            profile_name=profile_name, skip=start_index, limit=effective_limit,
        ).data()

        total_rec = session.run(
            """
            MATCH (c:OWLComp {module: $mod, odoo_version: $v})
            WHERE ($profile_name IS NULL OR $profile_name IN c.profile)
              AND ($bound_model IS NULL OR c.bound_model = $bound_model)
              AND c.module <> '__unresolved__'
            RETURN count(c) AS c
            """,
            mod=module, v=odoo_version, bound_model=bound_model,
            profile_name=profile_name,
        ).single()
        total = total_rec["c"] if total_rec else 0

    header = f"OWL components of {module} (Odoo {odoo_version})"
    if total == 0:
        lines = [header]
        if bound_model is not None:
            lines.append(
                "├─ Warning: bound_model resolution is heuristic"
                " — may miss components using dynamic this.props.resModel",
            )
        lines.append("├─ (none)")
        # Wave 5: suggest list_qweb_templates / list_js_patches as siblings.
        lines.append(format_next_step([
            f"list_qweb_templates(module='{module}'"
            f", odoo_version='{odoo_version}') for QWeb templates",
            f"list_js_patches(module='{module}', odoo_version='{odoo_version}')"
            " for related patches",
        ]))
        return "\n".join(lines)

    # Mint opaque refs for each returned row.
    # Use field_name key so _infer_kind detects 'field' (prefix 'f').
    # OWL components have no native kind in PREFIX_BY_KIND; 'field' prefix
    # is acceptable for non-model-entity refs (future wave can add 'owl' kind).
    comp_items = [{"field_name": r["name"], "module": module} for r in rows]
    ref_ids = mint_refs(comp_items, api_key_id, kind="field")

    lines = [header]
    more_hint = (
        f"list_owl_components(module='{module}'"
        f", odoo_version='{odoo_version}', limit={max(limit * 2, total)})"
        " for full list"
    )
    raw_rows = rows
    rendered = _render_capped(
        raw_rows,
        lambda r: f"{r['name']} : {r.get('bound_model') or '(unbound)'}",
        cap=cap,
        more_hint=more_hint,
    )
    # Inject [ref=fN] prefix for non-hint rows.
    ref_iter = iter(ref_ids)
    tagged: list[str] = []
    for row_str in rendered:
        if row_str.startswith("... and "):
            tagged.append(row_str)
        else:
            ref_id = next(ref_iter, None)
            prefix = f"[ref={ref_id}] " if ref_id else ""
            tagged.append(f"{prefix}{row_str}")

    # If bound_model filter used, the warning must precede the data (as ├─)
    # so the final data branch can still terminate cleanly.
    if bound_model is not None:
        lines.append(
            "├─ Warning: bound_model resolution is heuristic"
            " — may miss components using dynamic this.props.resModel"
        )

    for row in tagged:
        # Wave 5: All rows are ├─; Next: footer becomes the final └─.
        lines.append(f"├─ {row}")

    shown = len(rows)
    end_index = start_index + shown
    has_more = total > end_index

    if has_more:
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total}."
            f" Call list_owl_components(module='{module}',"
            f" odoo_version='{odoo_version}',"
            f" start_index={end_index}) for next {min(cap, total - end_index)}."
        )
    elif start_index > 0:
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total} (last page)."
        )

    # Wave 5: Next-step footer per ADR-0023 §4.
    lines.append(format_next_step([
        f"list_qweb_templates(module='{module}', odoo_version='{odoo_version}')"
        " for QWeb templates",
        f"list_js_patches(module='{module}', odoo_version='{odoo_version}')"
        " for related patches",
    ]))
    return "\n".join(lines)


def _list_qweb_templates(
    module: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
    limit: int = 200,
    start_index: int = 0,
    api_key_id: str = _ANONYMOUS_API_KEY_ID,
) -> str:
    """Layer-5c — enumerate QWeb templates declared in a module.

    Renders `xmlid : t-inherit=<parent or (root)>` per ADR-0023 §5.3.
    `start_index` is a zero-based pagination cursor (Cypher SKIP).
    `api_key_id` scopes minted refs to the calling tenant (default: 'anonymous').
    """
    cap = LIST_PREVIEW_MAX_ITEMS
    effective_limit = min(limit, cap)

    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        rows = session.run(
            """
            MATCH (t:QWebTmpl {module: $mod, odoo_version: $v})
            WHERE ($profile_name IS NULL OR $profile_name IN t.profile)
              AND t.module <> '__unresolved__'
            OPTIONAL MATCH (t)-[:EXTENDS_TMPL]->(parent:QWebTmpl)
            WHERE NOT coalesce(parent.unresolved, false)
            RETURN t.xmlid AS xmlid, parent.xmlid AS parent_xmlid
            ORDER BY t.xmlid ASC
            SKIP $skip
            LIMIT $limit
            """,
            mod=module, v=odoo_version, profile_name=profile_name,
            skip=start_index, limit=effective_limit,
        ).data()

        total_rec = session.run(
            """
            MATCH (t:QWebTmpl {module: $mod, odoo_version: $v})
            WHERE ($profile_name IS NULL OR $profile_name IN t.profile)
              AND t.module <> '__unresolved__'
            RETURN count(t) AS c
            """,
            mod=module, v=odoo_version, profile_name=profile_name,
        ).single()
        total = total_rec["c"] if total_rec else 0

    header = f"QWeb templates of {module} (Odoo {odoo_version})"
    if total == 0:
        next_line = format_next_step([
            f"list_owl_components(module='{module}', odoo_version='{odoo_version}')"
            " for OWL components",
            f"describe_module(name='{module}', odoo_version='{odoo_version}')"
            " for module overview",
        ])
        return f"{header}\n├─ (none)\n{next_line}"

    # Mint opaque refs for each returned row.
    # QWeb templates have xmlid — use view kind (prefix 'v').
    tmpl_items = [{"xmlid": r["xmlid"]} for r in rows]
    ref_ids = mint_refs(tmpl_items, api_key_id, kind="view")

    lines = [header]
    more_hint = (
        f"list_qweb_templates(module='{module}'"
        f", odoo_version='{odoo_version}', limit={max(limit * 2, total)})"
        " for full list"
    )
    rendered = _render_capped(
        rows,
        lambda r: (
            f"{r['xmlid']} : t-inherit="
            f"{r.get('parent_xmlid') or '(root)'}"
        ),
        cap=cap,
        more_hint=more_hint,
    )
    # Inject [ref=vN] prefix for non-hint rows.
    ref_iter = iter(ref_ids)
    for row_str in rendered:
        if row_str.startswith("... and "):
            lines.append(f"├─ {row_str}")
        else:
            ref_id = next(ref_iter, None)
            prefix = f"[ref={ref_id}] " if ref_id else ""
            lines.append(f"├─ {prefix}{row_str}")

    shown = len(rows)
    end_index = start_index + shown
    has_more = total > end_index

    if has_more:
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total}."
            f" Call list_qweb_templates(module='{module}',"
            f" odoo_version='{odoo_version}',"
            f" start_index={end_index}) for next {min(cap, total - end_index)}."
        )
    elif start_index > 0:
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total} (last page)."
        )

    # Wave 5: Next-step footer per ADR-0023 §4.
    lines.append(format_next_step([
        f"list_owl_components(module='{module}', odoo_version='{odoo_version}')"
        " for OWL components",
        f"find_examples(query='QWeb {module}', odoo_version='{odoo_version}')"
        " for inheritance patterns",
    ]))
    return "\n".join(lines)


# Era param mapping per ADR-0023 §5.3: user-facing era1/era2/era3 ↔
# stored JSPatch.era values ('extend'/'include'/'patch').
_JS_ERA_MAP = {
    "era1": "extend",
    "era2": "include",
    "era3": "patch",
    "extend": "extend",
    "include": "include",
    "patch": "patch",
}


def _list_js_patches(
    odoo_version: str = "auto",
    target: str | None = None,
    module: str | None = None,
    era: str | None = None,
    profile_name: str | None = None,
    limit: int = 200,
    start_index: int = 0,
    api_key_id: str = _ANONYMOUS_API_KEY_ID,
) -> str:
    """Layer-5d — enumerate JS patches across eras (Widget extend, mixin
    include, OWL patch).

    `era` accepts era1/era2/era3 (preferred) or extend/include/patch (stored
    values). `target` filters by patched component/widget name.
    `start_index` is a zero-based pagination cursor (Cypher SKIP).
    `api_key_id` scopes minted refs to the calling tenant (default: 'anonymous').
    """
    cap = LIST_PREVIEW_PATCHES_MAX
    effective_limit = min(limit, cap)

    era_filter: str | None = None
    if era is not None:
        era_filter = _JS_ERA_MAP.get(era.lower())
        if era_filter is None:
            return (
                f"Invalid era '{era}'. Use era1, era2, or era3"
                " (or extend/include/patch)."
            )

    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        rows = session.run(
            f"""
            MATCH (j:JSPatch {{odoo_version: $v}})
            WHERE ($profile_name IS NULL OR $profile_name IN j.profile)
              AND ($target IS NULL OR j.target = $target)
              AND ($module IS NULL OR j.module = $module)
              AND ($era IS NULL OR j.era = $era)
              AND j.module <> '__unresolved__'
            OPTIONAL MATCH (mod:Module {{name: j.module, odoo_version: $v}})
            WITH j, mod,
                 {_edition_rank_cypher("mod")},
                 mod.name AS mod_name
            RETURN j.target AS target, j.patch_name AS patch_name,
                   j.era AS era, j.module AS module, mod.repo AS repo,
                   edition_rank, mod_name
            ORDER BY edition_rank ASC, mod_name ASC, j.target ASC, j.patch_name ASC
            SKIP $skip
            LIMIT $limit
            """,
            v=odoo_version, target=target, module=module, era=era_filter,
            profile_name=profile_name, skip=start_index, limit=effective_limit,
        ).data()

        total_rec = session.run(
            """
            MATCH (j:JSPatch {odoo_version: $v})
            WHERE ($profile_name IS NULL OR $profile_name IN j.profile)
              AND ($target IS NULL OR j.target = $target)
              AND ($module IS NULL OR j.module = $module)
              AND ($era IS NULL OR j.era = $era)
              AND j.module <> '__unresolved__'
            RETURN count(j) AS c
            """,
            v=odoo_version, target=target, module=module, era=era_filter,
            profile_name=profile_name,
        ).single()
        total = total_rec["c"] if total_rec else 0

    parent = target or module or "all targets"
    header = f"JS patches on {parent} (Odoo {odoo_version})"
    if total == 0:
        # Wave 5: Next-step footer per ADR-0023 §4 — suggest OWL components
        # when module is known (era3 drill-down).
        if module:
            next_line = format_next_step([
                f"list_owl_components(module='{module}'"
                f", odoo_version='{odoo_version}') for v15+ components",
            ])
        else:
            next_line = format_next_step([
                f"find_examples(query='JS patch', odoo_version='{odoo_version}')"
                " for patch patterns",
            ])
        return f"{header}\n├─ (none)\n{next_line}"

    # Mint opaque refs for each returned row.
    # JS patches have module_name key → 'module' kind (prefix 'x').
    patch_items = [
        {"module_name": r.get("module") or "?", "target": r.get("target") or "?"}
        for r in rows
    ]
    ref_ids = mint_refs(patch_items, api_key_id, kind="module")

    groups: dict[tuple[str, str], list[tuple[dict, str]]] = {}
    order: list[tuple[str, str]] = []
    for r, ref_id in zip(rows, ref_ids):
        key = (r.get("repo") or "?", r.get("module") or "?")
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((r, ref_id))

    lines = [header]
    for key in order:
        repo, mod_name = key
        lines.append(f"├─ [{repo}] {mod_name}")
        sub_indent = "│   "
        sub_items = groups[key]
        more_hint = (
            f"list_js_patches(odoo_version='{odoo_version}'"
            f", limit={max(limit * 2, total)}) for full list"
        )
        raw_rows = [r for r, _ in sub_items]
        rendered = _render_capped(
            raw_rows,
            lambda r: (
                f"{r['target']}.{r['patch_name']} : era={r.get('era') or '?'}"
            ),
            cap=cap,
            more_hint=more_hint,
        )
        # Inject [ref=xN] prefix for non-hint rows.
        ref_iter = iter([ref_id for _, ref_id in sub_items])
        tagged: list[str] = []
        for row_str in rendered:
            if row_str.startswith("... and "):
                tagged.append(row_str)
            else:
                ref_id = next(ref_iter, None)
                prefix = f"[ref={ref_id}] " if ref_id else ""
                tagged.append(f"{prefix}{row_str}")

        last_r = len(tagged) - 1
        for j, row in enumerate(tagged):
            r_conn = "└─" if j == last_r else "├─"
            lines.append(f"{sub_indent}{r_conn} {row}")

    shown = len(rows)
    end_index = start_index + shown
    has_more = total > end_index

    if has_more:
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total}."
            f" Call list_js_patches(odoo_version='{odoo_version}',"
            f" start_index={end_index}) for next {min(cap, total - end_index)}."
        )
    elif start_index > 0:
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total} (last page)."
        )

    # Wave 5: Next-step footer per ADR-0023 §4. Prefer module-scoped OWL
    # drill-down when module is known; otherwise suggest find_examples.
    if module:
        next_hints = [
            f"list_owl_components(module='{module}'"
            f", odoo_version='{odoo_version}') for v15+ components",
            f"find_examples(query='JS patch', odoo_version='{odoo_version}')"
            " for patch patterns",
        ]
    else:
        next_hints = [
            f"find_examples(query='JS patch', odoo_version='{odoo_version}')"
            " for patch patterns",
        ]
    lines.append(format_next_step(next_hints))
    return "\n".join(lines)


_ANTI_PATTERNS_BASE = [
    "Old-style super(ClassName, self) — use plain super() in Python 3",
    "Missing return after super() — caller gets None, breaks chain",
]


def _anti_patterns_for_convention(kind: str) -> list[str]:
    """Return convention-specific anti-pattern hints for find_override_point."""
    if kind == "compute":
        return [
            "Calling super() in compute method — Odoo rebinds via @api.depends, "
            "super-chain semantically meaningless",
            "Forgetting @api.depends — silent stale data on field reads",
        ]
    if kind in ("inverse", "search", "default"):
        return [
            f"Calling super() in {kind} method — Odoo rebinds via decorator, "
            "super-chain has no effect",
        ]
    if kind == "action":
        return list(_ANTI_PATTERNS_BASE) + [
            "Returning bool/None instead of action_window dict — UI can't refresh",
        ]
    if kind == "crud":
        return list(_ANTI_PATTERNS_BASE) + [
            "Missing @api.model_create_multi on create() override — slow batch import",
            "Treating vals as single dict instead of vals_list — silent data loss",
        ]
    return list(_ANTI_PATTERNS_BASE)


def _fetch_method_for_diff(session, model: str, method: str, version: str) -> dict | None:
    """Fetch a single Method node's properties for cross-version diff.

    Returns a dict with keys: decorators, convention_kind, super_safety,
    has_super_call, signature. Returns None when no Method found.
    Aggregates across all modules (decorators union, super_call OR).
    """
    rows = session.run("""
        MATCH (mth:Method {name: $method, model: $model, odoo_version: $v})
        RETURN mth.decorators AS decorators,
               mth.convention_kind AS ck,
               mth.super_safety AS ss,
               coalesce(mth.has_super_call, false) AS has_super,
               mth.signature AS signature
        ORDER BY mth.module
    """, method=method, model=model, v=version).data()
    if not rows:
        return None
    # Merge across override chain: union decorators, OR has_super, first non-null sig
    all_decs: list[str] = []
    seen_decs: set[str] = set()
    has_super = False
    sig: str | None = None
    ck = rows[0]["ck"] or "private"
    ss = rows[0]["ss"] or "usually"
    for r in rows:
        for d in (r["decorators"] or []):
            if d not in seen_decs:
                seen_decs.add(d)
                all_decs.append(d)
        if r["has_super"]:
            has_super = True
        if sig is None and r["signature"] is not None:
            sig = r["signature"]
    return {
        "decorators": all_decs,
        "convention_kind": ck,
        "super_safety": ss,
        "has_super_call": has_super,
        "signature": sig,
    }


def _diff_method_across_versions(
    model: str, method: str, from_version: str, to_version: str,
    *, _driver=None,
) -> str:
    """Diff a method between two Odoo versions.

    Compares decorator set, convention_kind, super_safety, and signature
    between from_version and to_version. Returns tree-formatted string.
    """
    driver = _driver or _get_driver()
    with driver.session() as session:
        from_data = _fetch_method_for_diff(session, model, method, from_version)
        to_data = _fetch_method_for_diff(session, model, method, to_version)

    header = f"Method version diff ({model}.{method}: {from_version} → {to_version})"
    lines = [header]

    # Presence
    if from_data and to_data:
        presence_label = "both versions present"
    elif from_data and not to_data:
        presence_label = f"deleted in {to_version} (not found)"
    elif not from_data and not to_data:
        presence_label = (
            f"absent in both {from_version} and {to_version}"
            " (model/method may not be indexed)"
        )
        lines.append(f"├─ Status:           {presence_label}")
        lines.append(format_next_step([
            f"list_methods(model='{model}', odoo_version='{to_version}')"
            " to verify the method name",
        ]))
        return "\n".join(lines)
    else:
        presence_label = f"added in {to_version} (not in {from_version})"
    lines.append(f"├─ Status:           {presence_label}")

    # Decorator diff
    from_decs = set(from_data["decorators"]) if from_data else set()
    to_decs = set(to_data["decorators"]) if to_data else set()
    removed = sorted(from_decs - to_decs)
    added = sorted(to_decs - from_decs)
    if removed or added:
        lines.append("├─ Decorator changes:")
        items = [f"Removed in {to_version}: {d}" for d in removed]
        items += [f"Added in {to_version}:   {d}" for d in added]
        last_idx = len(items) - 1
        for i, text in enumerate(items):
            connector = "└─" if i == last_idx else "├─"
            lines.append(f"│   {connector} {text}")
    else:
        lines.append("├─ Decorator changes: none")

    # Convention diff
    from_ck = from_data["convention_kind"] if from_data else "?"
    to_ck = to_data["convention_kind"] if to_data else "?"
    if from_ck != to_ck:
        lines.append(f"├─ Convention:        changed ({from_ck} → {to_ck})")
    else:
        lines.append(f"├─ Convention:        unchanged ({from_ck})")

    # Signature diff
    _NULL_HINT = "(not stored, run 'index-repo --full' to populate)"
    from_sig = from_data["signature"] if from_data else None
    to_sig = to_data["signature"] if to_data else None
    from_sig_str = from_sig if from_sig is not None else _NULL_HINT
    to_sig_str = to_sig if to_sig is not None else _NULL_HINT
    if from_sig is None or to_sig is None:
        lines.append(
            f"├─ Signature:         {from_version}={from_sig_str}"
            f" → {to_version}={to_sig_str}"
        )
    elif from_sig != to_sig:
        lines.append(
            f"├─ Signature:         {from_version}={from_sig}"
            f" → {to_version}={to_sig}"
        )
    else:
        lines.append(f"├─ Signature:         unchanged ({from_sig})")

    # Super safety
    from_ss = from_data["super_safety"] if from_data else "?"
    to_ss = to_data["super_safety"] if to_data else "?"
    if from_ss != to_ss:
        lines.append(f"├─ Super safety:      changed ({from_ss} → {to_ss})")
    else:
        lines.append(f"├─ Super safety:      unchanged ({from_ss})")

    # Wave 5: Next-step footer per ADR-0023 §4.
    lines.append(format_next_step([
        f"resolve_method(model_name='{model}', method_name='{method}'"
        f", odoo_version='{to_version}') for full chain detail",
        f"find_examples(query='{method} override', odoo_version='{to_version}')"
        " for prior art",
    ]))
    return "\n".join(lines)


def _find_override_point(
    model: str, method: str, odoo_version: str = "auto",
    *, to_version: str = "", _driver=None,
) -> str:
    """Inspect Method override chain + surface convention hints + anti-patterns.

    When to_version is non-empty and differs from odoo_version, performs a
    cross-version diff instead of single-version inspection.
    """
    driver = _driver or _get_driver()
    with driver.session() as session:
        v = _resolve_version(odoo_version, session)

    # Cross-version diff mode
    if to_version and to_version != v:
        return _diff_method_across_versions(
            model, method, from_version=v, to_version=to_version, _driver=driver,
        )

    # Single-version mode (existing behaviour)
    with driver.session() as session:
        records = session.run("""
            MATCH (mth:Method {name: $method, model: $model, odoo_version: $v})
            OPTIONAL MATCH (mod:Module {name: mth.module, odoo_version: $v})
            RETURN mth.module AS module, mth.convention_kind AS ck,
                   mth.super_safety AS ss, mth.return_required AS rr,
                   coalesce(mth.has_super_call, false) AS has_super,
                   mod.repo AS repo, mod.edition AS edition
            ORDER BY mth.module
        """, method=method, model=model, v=v).data()

    if not records:
        next_line = format_next_step([
            f"list_methods(model='{model}', odoo_version='{v}')"
            " to find the actual method name",
        ])
        return (
            f"find_override_point({model!r}, {method!r}, {v})\n"
            f"├─ method not found on model {model!r} in Odoo {v}\n"
            + next_line
        )

    convention_kind = records[0]["ck"] or "private"
    super_safety = records[0]["ss"] or "usually"
    return_required = bool(records[0]["rr"])
    super_count = sum(1 for r in records if r["has_super"])
    super_ratio = f"{super_count}/{len(records)}"
    anti_patterns = _anti_patterns_for_convention(convention_kind)

    return _format_find_override_point(
        model=model, method=method, version=v, records=records,
        super_ratio=super_ratio, convention_kind=convention_kind,
        super_safety=super_safety, return_required=return_required,
        anti_patterns=anti_patterns,
    )


def _format_find_override_point(
    *, model: str, method: str, version: str, records: list[dict],
    super_ratio: str, convention_kind: str, super_safety: str,
    return_required: bool, anti_patterns: list[str],
) -> str:
    lines = [f"find_override_point({model!r}, {method!r}, {version})"]
    lines.append(f"├─ Convention:      {convention_kind}")
    lines.append(f"├─ Super safety:    {super_safety}")
    lines.append(f"├─ Return required: {'Yes' if return_required else 'No'}")
    lines.append(f"├─ Super ratio:     {super_ratio} (overrides calling super)")
    lines.append(f"├─ Override chain ({len(records)}):")
    for i, r in enumerate(records):
        connector = "└─" if i == len(records) - 1 else "├─"
        repo = f"[{r['repo']}] " if r.get("repo") else ""
        ed = f" ({r['edition']})" if r.get("edition") else ""
        super_mark = "✓" if r["has_super"] else "✗"
        lines.append(
            f"│   {connector} {repo}{r['module']}{ed} — {super_mark} super()"
        )
    lines.append(f"├─ Anti-patterns ({len(anti_patterns)}):")
    for i, ap in enumerate(anti_patterns):
        connector = "└─" if i == len(anti_patterns) - 1 else "├─"
        lines.append(f"│   {connector} {ap}")
    # Wave 5: Next-step footer per ADR-0023 §4.
    lines.append(format_next_step([
        f"resolve_method(model_name='{model}', method_name='{method}'"
        f", odoo_version='{version}') for full chain detail",
        f"find_examples(query='{method} override', odoo_version='{version}')"
        " for prior art",
    ]))
    return "\n".join(lines)


@mcp.tool(**READONLY_TOOL_KWARGS)
def suggest_pattern(
    intent: str,
    odoo_version: str = "auto",
    language: str = "python",
    limit: int = 5,
) -> str:
    """Recommend curated Odoo patterns with gotchas from a natural-language intent.

    TRIGGER when: "best pattern for wizard in Odoo", "how to implement
    multi-company in Odoo", "pattern for override without breaking upstream",
    "cách tốt nhất để implement X", "design pattern cho Odoo module",
    "what's the right way to add computed field"
    PREFER over: LLM knowledge — returns curated patterns from indexed catalogue
    with real code snippets and versioned gotchas, not hallucinated patterns
    SKIP when: user wants existing code examples from codebase → use
    find_examples; user wants method override chain → use find_override_point

    Args:
        intent: NL description of intent, e.g. 'computed field cross-model
            partner'.
        odoo_version: '17.0' / '18.0' / 'auto'.
        language: 'python' | 'xml' | 'js' | 'all'. Default 'python'.
        limit: Max patterns to return (default 5).

    Returns:
        Tree list of patterns ranked by cosine score, each with snippet (first
        5 lines), file ref, and gotchas. Empty index → instruction to seed.

    Example:
        suggest_pattern("override write to read old value", "17.0")
        → suggest_pattern('override write to read old value', 17.0, ...) — 1 matches
          └─ #1 · score 0.81 · write-read-before-super
              ├─ Language: python (min v17.0)
              └─ Gotchas:
                   • Reading old values AFTER super().write() returns new value
    """
    return _suggest_pattern(intent, odoo_version, language, limit)


@mcp.tool(**READONLY_TOOL_KWARGS)
def check_module_exists(
    name: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
) -> str:
    """Verify if a module is indexed and flag EE-confusion for Viindoo stack.

    TRIGGER when: "does module sale_management exist in Odoo 17", "is
    viin_sale available", "check if feature X is in standard Odoo", "module X
    có trong OCA không", "Odoo 17 có tính năng X chưa", "is helpdesk an EE
    module"
    PREFER over: searching manually — instant cross-version, cross-repo module
    existence check with Enterprise edition detection and Viindoo equivalent
    SKIP when: caller needs the module's contents (models, views, JS) — use
    describe_module instead, which returns a full architecture overview in
    one round-trip. user wants module field/method details → use resolve_model;
    user wants code examples from a module → use find_examples

    Args:
        name: Module technical name (e.g. 'sale', 'helpdesk', 'viin_helpdesk').
        odoo_version: '17.0' / '18.0' / 'auto'.
        profile_name: Optional profile filter (e.g. 'viindoo_internal_17').
            When set, only Module nodes whose profile array contains this name
            are checked. Default None checks across all profiles.

    Returns:
        Tree text: Indexed yes/no, edition, EE-confusion flag, Viindoo
        equivalent (if any), and WARNING when name is an EE-only module.

    Example:
        check_module_exists('helpdesk', '17.0')
        → check_module_exists('helpdesk', 17.0)
          ├─ Indexed:         No
          ├─ Is EE confusion: Yes
          ├─ Viindoo equiv:   viin_helpdesk
          └─ ⚠ WARNING: this is an Odoo Enterprise module (legacy hardcoded dict).
    """
    return _check_module_exists(name, odoo_version, profile_name=profile_name)


@mcp.tool(**READONLY_TOOL_KWARGS)
def find_override_point(
    model: str, method: str, odoo_version: str = "auto", to_version: str = "",
) -> str:
    """Show override chain + super-call convention + anti-patterns for a method.

    TRIGGER when: "where should I override action_confirm in sale.order", "best
    override point for partner creation", "how to extend method X without
    breaking OCA", "override field X ở đâu là đúng", "điểm override phù hợp
    cho method Y", "is super() required for write override"
    PREFER over: resolve_method — gives recommended injection points with
    super() safety guidance and anti-patterns, not just chain listing
    SKIP when: user wants full override chain only → use resolve_method; user
    wants design pattern guidance → use suggest_pattern

    Args:
        model: Odoo model dotted name (e.g. 'sale.order').
        method: Method name (e.g. 'action_confirm', '_compute_amount').
        odoo_version: '17.0' / '18.0' / 'auto'. From-version in diff mode.
        to_version: Optional. When set, activates cross-version diff mode
            (e.g. '18.0' to diff 17.0 → 18.0). Default '' = single-version.

    Returns:
        Single-version: convention_kind, super_safety, return_required,
        super_ratio, override chain, and anti-patterns.
        Cross-version diff: presence, decorator changes, signature diff,
        convention and super safety change.

    Example:
        find_override_point('sale.order', 'action_confirm', '17.0')
        → find_override_point('sale.order', 'action_confirm', 17.0)
          ├─ Convention:      action
          ├─ Super safety:    always
          ├─ Return required: Yes
          ├─ Super ratio:     7/7
          └─ Anti-patterns (3): ...
    """
    return _find_override_point(model, method, odoo_version, to_version=to_version)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Dual-channel companion functions (M10.5 WI-B3, Option A — independent Cypher).
#
# Each _X_structured() mirrors the data fetched by its _X() counterpart and
# returns a typed *Output DTO (or None when the entity is not found).  They
# intentionally re-issue the same Cypher queries so the text channel (_X)
# remains byte-identical and unmodified (AC-B3-3).  The 2x DB cost is
# accepted for this PoC wave; a future wave can collapse to Option B (shared
# rows).
#
# next_step_hint is populated via hints_for() using the same ctx keys that
# the NEXT_STEP_HINTS registry templates use (see src/mcp/hints.py).
# ---------------------------------------------------------------------------


def _resolve_model_structured(
    model_name: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
) -> ResolveModelOutput | None:
    """Structured companion for _resolve_model. Returns None when not found."""
    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        layers = session.run(
            f"""
            MATCH (m:Model {{name: $name, odoo_version: $v}})-[:DEFINED_IN]->(mod:Module)
            WHERE ($profile_name IS NULL OR $profile_name IN m.profile)
            WITH m, mod,
                 CASE WHEN coalesce(m.is_definition, false) THEN 0 ELSE 1 END AS is_def_rank,
                 COUNT {{
                     (:Field {{model: $name, module: m.module, odoo_version: $v}})
                 }} AS field_count,
                 COUNT {{ ()-[:{REL_DEPENDS_ON}]->(mod) }} AS dependents,
                 {_edition_rank_cypher("mod")},
                 mod.name AS mod_name
            RETURN m.module AS module_name, mod.repo AS repo,
                   coalesce(m.is_definition, false) AS is_definition,
                   mod.edition AS edition,
                   COUNT {{ (:Field {{model: $name, odoo_version: $v}}) }} AS fields_count,
                   COUNT {{ (:Method {{model: $name, odoo_version: $v}}) }} AS methods_count
            ORDER BY is_def_rank ASC, field_count DESC, dependents DESC,
                     edition_rank ASC, mod_name ASC
            """,
            name=model_name, v=odoo_version, profile_name=profile_name,
        ).data()

        if not layers:
            return None

        parents = session.run(f"""
            MATCH (:Model {{name: $name, odoo_version: $v}})-[r:{REL_INHERITS}]->(p:Model)
            WHERE p.name <> $name
              AND NOT coalesce(r.unresolved, false)
            RETURN DISTINCT p.name AS pname
            ORDER BY pname
        """, name=model_name, v=odoo_version).data()

    base = layers[0]
    extensions = layers[1:]

    defined_in = ModuleRef(
        name=base["module_name"],
        odoo_version=odoo_version,
        profile=None,
    )
    extended_by = [
        ModuleRef(name=ext["module_name"], odoo_version=odoo_version, profile=None)
        for ext in extensions
    ]
    inherits_from = [p["pname"] for p in parents]

    return ResolveModelOutput(
        ref=ModelRef(name=model_name, module=base["module_name"], odoo_version=odoo_version),
        is_definition=bool(base.get("is_definition", False)),
        defined_in=defined_in,
        extended_by=extended_by,
        inherits_from=inherits_from,
        field_count=base["fields_count"],
        method_count=base["methods_count"],
        next_step_hint=format_next_step([
            f"list_fields(model='{model_name}', odoo_version='{odoo_version}')"
            " for full field list",
            f"list_methods(model='{model_name}', odoo_version='{odoo_version}')"
            " for behavior",
        ]),
    )


def _resolve_field_structured(
    model_name: str,
    field_name: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
) -> ResolveFieldOutput | None:
    """Structured companion for _resolve_field. Returns None when not found."""
    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        records = session.run(f"""
            MATCH (f:Field {{name: $fn, model: $mn, odoo_version: $v}})
            WHERE ($profile_name IS NULL OR $profile_name IN f.profile)
            OPTIONAL MATCH (mod:Module {{name: f.module, odoo_version: $v}})
            OPTIONAL MATCH (m_node:Model {{name: $mn, module: f.module, odoo_version: $v}})
            WITH f, mod, m_node,
                 CASE WHEN coalesce(m_node.is_definition, false) THEN 0 ELSE 1 END
                      AS is_def_rank,
                 COUNT {{
                     (:Field {{model: $mn, module: f.module, odoo_version: $v}})
                 }} AS field_count,
                 COUNT {{ ()-[:{REL_DEPENDS_ON}]->(mod) }} AS dependents,
                 {_edition_rank_cypher("mod")},
                 mod.name AS mod_name
            RETURN f, f.module AS module_name
            ORDER BY is_def_rank ASC, field_count DESC, dependents DESC,
                     edition_rank ASC, mod_name ASC
        """, fn=field_name, mn=model_name, v=odoo_version, profile_name=profile_name).data()

    if not records:
        return None

    base_f = records[0]["f"]
    declared_in = [
        FieldRef(
            model=model_name,
            name=field_name,
            module=r["module_name"],
            odoo_version=odoo_version,
        )
        for r in records
    ]

    return ResolveFieldOutput(
        ref=FieldRef(
            model=model_name,
            name=field_name,
            module=records[0]["module_name"],
            odoo_version=odoo_version,
        ),
        ttype=base_f.get("ttype") or "?",
        computed=bool(base_f.get("compute")),
        compute_method=base_f.get("compute") or None,
        stored=bool(base_f.get("stored", True)),
        required=bool(base_f.get("required", False)),
        related=base_f.get("related") or None,
        declared_in=declared_in,
        next_step_hint=format_next_step([
            f"find_examples(query='{model_name}.{field_name} usage'"
            f", odoo_version='{odoo_version}') for real-world patterns",
            f"impact_analysis(entity_type='field'"
            f", entity_name='{model_name}.{field_name}'"
            f", odoo_version='{odoo_version}') for blast radius",
        ]),
    )


def _resolve_method_structured(
    model_name: str,
    method_name: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
) -> ResolveMethodOutput | None:
    """Structured companion for _resolve_method. Returns None when not found."""
    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        records = session.run(f"""
            MATCH (mth:Method {{name: $mn, model: $model, odoo_version: $v}})
            WHERE ($profile_name IS NULL OR $profile_name IN mth.profile)
            OPTIONAL MATCH (mod:Module {{name: mth.module, odoo_version: $v}})
            OPTIONAL MATCH (m_node:Model {{name: $model, module: mth.module, odoo_version: $v}})
            WITH mth, mod, m_node,
                 CASE WHEN coalesce(m_node.is_definition, false) THEN 0 ELSE 1 END
                      AS is_def_rank,
                 COUNT {{
                     (:Field {{model: $model, module: mth.module, odoo_version: $v}})
                 }} AS field_count,
                 COUNT {{ ()-[:{REL_DEPENDS_ON}]->(mod) }} AS dependents,
                 {_edition_rank_cypher("mod")},
                 mod.name AS mod_name
            RETURN mth.module AS module_name
            ORDER BY is_def_rank ASC, field_count DESC, dependents DESC,
                     edition_rank ASC, mod_name ASC
        """, mn=method_name, model=model_name, v=odoo_version, profile_name=profile_name).data()

    if not records:
        return None

    override_chain = [
        MethodRef(
            model=model_name,
            name=method_name,
            module=r["module_name"],
            odoo_version=odoo_version,
        )
        for r in records
    ]

    return ResolveMethodOutput(
        ref=MethodRef(
            model=model_name,
            name=method_name,
            module=records[0]["module_name"],
            odoo_version=odoo_version,
        ),
        override_chain=override_chain,
        next_step_hint=format_next_step([
            f"find_override_point(model='{model_name}', method='{method_name}'"
            f", odoo_version='{odoo_version}') for safe hook spot",
            f"impact_analysis(entity_type='method'"
            f", entity_name='{model_name}.{method_name}'"
            f", odoo_version='{odoo_version}') for blast radius",
        ]),
    )


def _resolve_view_structured(
    xmlid: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
) -> ResolveViewOutput | None:
    """Structured companion for _resolve_view. Returns None when not found."""
    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        view_rec = session.run("""
            MATCH (v:View {xmlid: $xmlid, odoo_version: $ver})
            WHERE ($profile_name IS NULL OR $profile_name IN v.profile)
            OPTIONAL MATCH (v)-[:DEFINED_IN]->(mod:Module)
            RETURN v, mod.name AS module_name
        """, xmlid=xmlid, ver=odoo_version, profile_name=profile_name).single()

        if not view_rec:
            return None

        extensions = session.run(f"""
            MATCH (ext:View {{odoo_version: $ver}})-[:{REL_INHERITS_VIEW}]->
                  (v:View {{xmlid: $xmlid, odoo_version: $ver}})
            WHERE NOT coalesce(ext.unresolved, false)
            AND ($profile_name IS NULL OR $profile_name IN ext.profile)
            RETURN ext.xmlid AS ext_xmlid,
                   ext.model AS ext_model,
                   coalesce(ext.xpaths_exprs, []) AS xpaths_exprs
        """, xmlid=xmlid, ver=odoo_version, profile_name=profile_name).data()

        parent_rec = session.run(f"""
            MATCH (v:View {{xmlid: $xmlid, odoo_version: $ver}})
                  -[r:{REL_INHERITS_VIEW}]->(parent:View {{odoo_version: $ver}})
            WHERE NOT coalesce(r.unresolved, false)
            AND ($profile_name IS NULL OR $profile_name IN v.profile)
            RETURN parent.xmlid AS parent_xmlid
        """, xmlid=xmlid, ver=odoo_version, profile_name=profile_name).single()

    v_props = view_rec["v"]
    own_exprs = list(v_props.get("xpaths_exprs") or [])

    extended_by = [
        ViewRef(
            xmlid=ext["ext_xmlid"],
            model=ext.get("ext_model"),
            odoo_version=odoo_version,
        )
        for ext in extensions
    ]

    return ResolveViewOutput(
        ref=ViewRef(
            xmlid=xmlid,
            model=v_props.get("model"),
            odoo_version=odoo_version,
        ),
        view_type=v_props.get("type") or "?",
        module=view_rec.get("module_name") or "?",
        mode=v_props.get("mode") or None,
        inherits_from=parent_rec["parent_xmlid"] if parent_rec else None,
        xpath_count=len(own_exprs),
        extended_by=extended_by,
        next_step_hint=format_next_step(
            [
                f"list_views(model='{v_props.get('model')}', odoo_version='{odoo_version}')"
                " for sibling views",
                f"find_examples(query='{xmlid} xpath', odoo_version='{odoo_version}')"
                " for inheritance patterns",
            ]
            if v_props.get("model")
            else [
                f"find_examples(query='{xmlid} xpath', odoo_version='{odoo_version}')"
                " for inheritance patterns",
            ]
        ),
    )


def _describe_module_structured(
    name: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
) -> DescribeModuleOutput | None:
    """Structured companion for _describe_module. Returns None when not found."""
    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        mod_rec = session.run(
            """
            MATCH (m:Module {name: $n, odoo_version: $v})
            WHERE ($profile_name IS NULL OR $profile_name IN m.profile)
            RETURN m.edition AS edition, m.version_raw AS version_raw
            """,
            n=name, v=odoo_version, profile_name=profile_name,
        ).single()

        if not mod_rec:
            return None

        depends = session.run(
            f"""
            MATCH (m:Module {{name: $n, odoo_version: $v}})
                  -[:{REL_DEPENDS_ON}]->(d:Module)
            RETURN d.name AS name ORDER BY d.name ASC
            """,
            n=name, v=odoo_version,
        ).data()

        defines = session.run(
            """
            MATCH (model:Model {module: $n, odoo_version: $v})
            WHERE coalesce(model.is_definition, false) = true
              AND model.module <> '__unresolved__'
              AND ($profile_name IS NULL OR $profile_name IN model.profile)
            RETURN model.name AS name ORDER BY model.name ASC
            """,
            n=name, v=odoo_version, profile_name=profile_name,
        ).data()

        extends = session.run(
            """
            MATCH (model:Model {module: $n, odoo_version: $v})
            WHERE coalesce(model.is_definition, false) = false
              AND model.module <> '__unresolved__'
              AND ($profile_name IS NULL OR $profile_name IN model.profile)
            RETURN model.name AS name ORDER BY model.name ASC
            """,
            n=name, v=odoo_version, profile_name=profile_name,
        ).data()

        view_total_rec = session.run(
            """
            MATCH (view:View {module: $n, odoo_version: $v})
            WHERE ($profile_name IS NULL OR $profile_name IN view.profile)
            RETURN count(view) AS c
            """,
            n=name, v=odoo_version, profile_name=profile_name,
        ).single()

        js_count_rec = session.run(
            """
            MATCH (j:JSPatch {module: $n, odoo_version: $v})
            WHERE ($profile_name IS NULL OR $profile_name IN j.profile)
            RETURN count(j) AS c
            """,
            n=name, v=odoo_version, profile_name=profile_name,
        ).single()

    return DescribeModuleOutput(
        ref=ModuleRef(name=name, odoo_version=odoo_version, profile=None),
        edition=mod_rec.get("edition") or "community",
        version_raw=mod_rec.get("version_raw") or None,
        depends=[d["name"] for d in depends],
        defines_models=[d["name"] for d in defines],
        extends_models=[e["name"] for e in extends],
        view_total=view_total_rec["c"] if view_total_rec else 0,
        js_patch_count=js_count_rec["c"] if js_count_rec else 0,
        next_step_hint=format_next_step(
            [
                f"list_fields(model='{defines[0]['name']}', module='{name}'"
                f", odoo_version='{odoo_version}') for declared fields",
                f"list_views(model='{defines[0]['name']}', odoo_version='{odoo_version}')"
                " for module views",
            ]
            if defines
            else (
                [
                    f"list_fields(model='{extends[0]['name']}', module='{name}'"
                    f", odoo_version='{odoo_version}') for declared fields",
                    f"list_views(model='{extends[0]['name']}', odoo_version='{odoo_version}')"
                    " for module views",
                ]
                if extends
                else []
            )
        ),
    )


def _list_fields_structured(
    model: str,
    odoo_version: str = "auto",
    module: str | None = None,
    kind: str | None = None,
    profile_name: str | None = None,
    limit: int = 200,
    start_index: int = 0,
    api_key_id: str = _ANONYMOUS_API_KEY_ID,
) -> ListFieldsOutput | None:
    """Structured companion for _list_fields. Returns None when model has no fields."""
    cap = LIST_PREVIEW_FIELDS_MAX
    effective_limit = min(limit, cap)

    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        rows = session.run(
            f"""
            MATCH (f:Field {{model: $m, odoo_version: $v}})
            WHERE ($profile_name IS NULL OR $profile_name IN f.profile)
              AND ($module IS NULL OR f.module = $module)
              AND ($kind IS NULL OR f.ttype = $kind)
              AND f.module <> '__unresolved__'
            OPTIONAL MATCH (mod:Module {{name: f.module, odoo_version: $v}})
            WITH f, mod,
                 {_edition_rank_cypher("mod")},
                 mod.name AS mod_name
            RETURN f.name AS name, f.ttype AS ttype, f.module AS module_name,
                   edition_rank, mod_name
            ORDER BY edition_rank ASC, mod_name ASC, f.name ASC
            SKIP $skip
            LIMIT $limit
            """,
            m=model, v=odoo_version, module=module, kind=kind,
            profile_name=profile_name, skip=start_index, limit=effective_limit,
        ).data()

        total_rec = session.run(
            """
            MATCH (f:Field {model: $m, odoo_version: $v})
            WHERE ($profile_name IS NULL OR $profile_name IN f.profile)
              AND ($module IS NULL OR f.module = $module)
              AND ($kind IS NULL OR f.ttype = $kind)
              AND f.module <> '__unresolved__'
            RETURN count(f) AS c
            """,
            m=model, v=odoo_version, module=module, kind=kind,
            profile_name=profile_name,
        ).single()
        total = total_rec["c"] if total_rec else 0

    # Mint refs for the structured channel — same api_key_id as text channel.
    field_items = [{"field_name": r["name"], "model": model} for r in rows]
    ref_ids = mint_refs(field_items, api_key_id, kind="field")

    fields = [
        FieldRef(
            model=model,
            name=r["name"],
            module=r["module_name"],
            odoo_version=odoo_version,
            ref=ref_id,
        )
        for r, ref_id in zip(rows, ref_ids)
    ]

    first_field = rows[0]["name"] if rows else None
    next_hints: list[str] = []
    if first_field:
        next_hints.append(
            f"resolve_field(model_name='{model}', field_name='{first_field}'"
            f", odoo_version='{odoo_version}') for full chain",
        )
    next_hints.append(
        f"list_methods(model='{model}', odoo_version='{odoo_version}')"
        " for behavior",
    )
    return ListFieldsOutput(
        model=model,
        odoo_version=odoo_version,
        total=total,
        shown=len(fields),
        fields=fields,
        next_step_hint=format_next_step(next_hints),
    )


def _list_methods_structured(
    model: str,
    odoo_version: str = "auto",
    module: str | None = None,
    profile_name: str | None = None,
    limit: int = 200,
    start_index: int = 0,
    api_key_id: str = _ANONYMOUS_API_KEY_ID,
) -> ListMethodsOutput | None:
    """Structured companion for _list_methods. Returns None when model has no methods."""
    cap = LIST_PREVIEW_MAX_ITEMS
    effective_limit = min(limit, cap)

    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        rows = session.run(
            f"""
            MATCH (mth:Method {{model: $m, odoo_version: $v}})
            WHERE ($profile_name IS NULL OR $profile_name IN mth.profile)
              AND ($module IS NULL OR mth.module = $module)
              AND mth.module <> '__unresolved__'
            OPTIONAL MATCH (mod:Module {{name: mth.module, odoo_version: $v}})
            WITH mth, mod,
                 {_edition_rank_cypher("mod")},
                 mod.name AS mod_name
            RETURN mth.name AS name, mth.module AS module_name,
                   edition_rank, mod_name
            ORDER BY edition_rank ASC, mod_name ASC, mth.name ASC
            SKIP $skip
            LIMIT $limit
            """,
            m=model, v=odoo_version, module=module,
            profile_name=profile_name, skip=start_index, limit=effective_limit,
        ).data()

        total_rec = session.run(
            """
            MATCH (mth:Method {model: $m, odoo_version: $v})
            WHERE ($profile_name IS NULL OR $profile_name IN mth.profile)
              AND ($module IS NULL OR mth.module = $module)
              AND mth.module <> '__unresolved__'
            RETURN count(mth) AS c
            """,
            m=model, v=odoo_version, module=module,
            profile_name=profile_name,
        ).single()
        total = total_rec["c"] if total_rec else 0

        override_rec = session.run(
            """
            MATCH (mth:Method {model: $m, odoo_version: $v})
            WHERE ($profile_name IS NULL OR $profile_name IN mth.profile)
              AND mth.module <> '__unresolved__'
            WITH mth.name AS name, count(DISTINCT mth.module) AS modcount
            WHERE modcount >= 2
            RETURN collect(name) AS overrides
            """,
            m=model, v=odoo_version, profile_name=profile_name,
        ).single()
        override_names = sorted(override_rec["overrides"]) if override_rec else []

    # Mint refs for the structured channel — same api_key_id as text channel.
    method_items = [{"method_name": r["name"], "model": model} for r in rows]
    ref_ids = mint_refs(method_items, api_key_id, kind="method")

    methods = [
        MethodRef(
            model=model,
            name=r["name"],
            module=r["module_name"],
            odoo_version=odoo_version,
            ref=ref_id,
        )
        for r, ref_id in zip(rows, ref_ids)
    ]

    first_method = rows[0]["name"] if rows else None
    next_hints: list[str] = []
    if first_method:
        next_hints.append(
            f"resolve_method(model_name='{model}', method_name='{first_method}'"
            f", odoo_version='{odoo_version}') for override chain",
        )
        next_hints.append(
            f"find_override_point(model='{model}', method='{first_method}'"
            f", odoo_version='{odoo_version}') for hook spot",
        )
    return ListMethodsOutput(
        model=model,
        odoo_version=odoo_version,
        total=total,
        shown=len(methods),
        methods=methods,
        override_names=override_names,
        next_step_hint=format_next_step(next_hints),
    )


# Wave 1 — @mcp.tool(**READONLY_TOOL_KWARGS) wrappers for the 7 new tools (ADR-0023 §5).
# TRIGGER docstrings keep EN + VI for router accuracy (ADR-0012 §2 exception).
# ---------------------------------------------------------------------------


@mcp.tool(output_schema=DescribeModuleOutput.model_json_schema(), **READONLY_TOOL_KWARGS)
def describe_module(
    name: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
) -> ToolResult:
    """Return a full architecture overview of an Odoo module (manifest +
    model/view/JS counts).

    TRIGGER when: "what does module viin_sale do", "describe sale_management",
    "overview of website_sale", "module X làm gì", "tóm tắt module Y"
    PREFER over: check_module_exists when caller needs module contents
    (models, views, JS), not just YES/NO. Also prefer over resolve_model
    when the question is about a module, not a model.
    SKIP when: caller only needs YES/NO — use check_module_exists (faster).

    Args:
        name: Module technical name (e.g. 'sale', 'viin_sale').
        odoo_version: '17.0' / '18.0' / 'auto'.
        profile_name: Optional profile filter.

    Returns:
        Tree: Manifest (Depends, Edition, Version), Defines models,
        Extends models, Views (by type), JS patches.

    Example:
        describe_module("viin_sale", "17.0")
        → Manifest:
            ├─ Depends: sale, account, viin_base
            ├─ Edition: viindoo
            ├─ Defines models: 2
            ├─ Extends models: 5
            ├─ Views: 12 (8 form, 3 tree, 1 search)
            └─ JS patches: 3

    See also: odoo://{version}/module/{name}
    """
    text = _describe_module(name, odoo_version, profile_name)
    structured = _describe_module_structured(name, odoo_version, profile_name)
    return ToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=structured.model_dump() if structured is not None else None,
    )


@mcp.tool(output_schema=ListFieldsOutput.model_json_schema(), **READONLY_TOOL_KWARGS)
def list_fields(
    model: str,
    odoo_version: str = "auto",
    module: str | None = None,
    kind: str | None = None,
    profile_name: str | None = None,
    limit: int = 200,
    start_index: int = 0,
) -> ToolResult:
    """Enumerate fields declared on an Odoo model, grouped by module.

    TRIGGER when: "list all fields of sale.order", "show fields on account.move",
    "what fields does res.partner have", "liệt kê field của model X",
    "tất cả field trên sale.order"
    PREFER over: resolve_model — returns full enumerated list with type per row
    SKIP when: caller wants one field's detail → resolve_field. When asking
    "how many fields", resolve_model is cheaper.

    Args:
        model: Odoo model dotted name (e.g. 'sale.order').
        odoo_version: '17.0' / '18.0' / 'auto'.
        module: Optional module filter.
        kind: Optional ttype filter (e.g. 'monetary').
        profile_name: Optional profile filter.
        limit: Cypher LIMIT (default 200). Render cap is 50.
        start_index: Pagination cursor (default 0).

    Returns:
        Tree: header + per-module subtree of `[ref=fN] name : ttype`.

    Example:
        list_fields("sale.order", "17.0", module="sale")
        → [odoo] sale
            ├─ [ref=f1] name : char
            └─ [ref=f2] amount_total : monetary

    See also: odoo://{version}/field/{model}/{field}
    """
    text = _list_fields(
        model, odoo_version, module, kind, profile_name, limit, start_index,
        api_key_id=_get_api_key_id(),
    )
    structured = _list_fields_structured(
        model, odoo_version, module, kind, profile_name, limit, start_index,
        api_key_id=_get_api_key_id(),
    )
    text = _deprecation_banner("model_inspect", "model='...', method='fields'") + text
    return ToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=structured.model_dump() if structured is not None else None,
    )


@mcp.tool(output_schema=ListMethodsOutput.model_json_schema(), **READONLY_TOOL_KWARGS)
def list_methods(
    model: str,
    odoo_version: str = "auto",
    module: str | None = None,
    profile_name: str | None = None,
    limit: int = 200,
    start_index: int = 0,
) -> ToolResult:
    """Enumerate methods on an Odoo model, grouped by module.

    Methods overridden across ≥2 modules are marked with `(*)`.

    TRIGGER when: "list methods of sale.order", "all methods on res.partner",
    "what behavior does account.move have", "method nào trên model X",
    "tất cả method của sale.order"
    PREFER over: resolve_method — enumerates every method on the model
    SKIP when: caller wants one method's override chain → resolve_method.
    For best override point → use find_override_point.

    Args:
        model: Odoo model dotted name.
        odoo_version: '17.0' / '18.0' / 'auto'.
        module: Optional module filter.
        profile_name: Optional profile filter.
        limit: Cypher LIMIT (default 200). Render cap is 20.
        start_index: Pagination cursor (default 0).

    Returns:
        Tree: header + per-module subtree of `[ref=mN] name[(*)] : kind`.

    Example:
        list_methods("sale.order", "17.0")
        → [odoo] sale
            ├─ [ref=m1] action_confirm(*) : action
            └─ [ref=m2] _compute_amount : compute

    See also: odoo://{version}/method/{model}/{method}
    """
    text = _list_methods(
        model, odoo_version, module, profile_name, limit, start_index,
        api_key_id=_get_api_key_id(),
    )
    structured = _list_methods_structured(
        model, odoo_version, module, profile_name, limit, start_index,
        api_key_id=_get_api_key_id(),
    )
    text = _deprecation_banner("model_inspect", "model='...', method='methods'") + text
    return ToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=structured.model_dump() if structured is not None else None,
    )


@mcp.tool(**READONLY_TOOL_KWARGS)
def list_views(
    model: str,
    odoo_version: str = "auto",
    view_type: str | None = None,
    profile_name: str | None = None,
    limit: int = 200,
    start_index: int = 0,
) -> str:
    """Enumerate XML views targeting an Odoo model, grouped by module.

    TRIGGER when: "list views of sale.order", "what views are defined for
    res.partner", "all form views on account.move", "view nào của model X",
    "tất cả form/tree view trên sale.order", "kanban views on hr.employee"
    PREFER over: resolve_view — that tool drills into one xmlid;
    list_views enumerates every view targeting the model.
    SKIP when: caller wants one view's xpath chain → use resolve_view. Use
    list_qweb_templates when the caller wants QWeb (not ir.ui.view) records.
    SKIP when (NEW): you want view enumeration with model/module context in one tool →
        prefer model_inspect(model='...', method='views') or
        module_inspect(name='...', method='views') (M11 superset, v0.5+)

    Args:
        model: Odoo model dotted name (e.g. 'sale.order').
        odoo_version: '17.0' / '18.0' / 'auto'.
        view_type: Optional filter (form/tree/kanban/search/...).
        profile_name: Optional profile filter.
        limit: Cypher LIMIT (default 200). Render cap is 20.
        start_index: Zero-based pagination cursor (default 0 = first page).

    Returns:
        Tree text: header + per-module subtree of `[ref=vN] xmlid : type` rows.

    Example:
        list_views("sale.order", "17.0", view_type="form")
        → Views of sale.order (Odoo 17.0)
          ├─ [odoo] sale
          │   └─ [ref=v1] sale.view_order_form : form
    """
    text = _list_views(
        model, odoo_version, view_type, profile_name, limit, start_index,
        api_key_id=_get_api_key_id(),
    )
    text = _deprecation_banner("model_inspect", "model='...', method='views'") + text
    return text


@mcp.tool(**READONLY_TOOL_KWARGS)
def list_owl_components(
    module: str,
    odoo_version: str = "auto",
    bound_model: str | None = None,
    profile_name: str | None = None,
    limit: int = 200,
    start_index: int = 0,
) -> str:
    """Enumerate OWL components declared in a module (Odoo v14+).

    Era-aware: empty + warning for v8-v13 (Widget era, no OWL).
    bound_model filter is heuristic — may miss dynamic resModel bindings.

    TRIGGER when: "list OWL components in sale_management", "what OWL
    components does website_sale define", "OWL components for sale.order",
    "OWL component nào trong module X", "tất cả OWL component bound to model Y"
    PREFER over: find_examples — that tool returns code snippets;
    list_owl_components gives the structured component inventory.
    SKIP when: caller wants legacy Widget (v8-v13) → use list_js_patches
    with era='era1'. Use list_qweb_templates for QWeb templates.
    SKIP when (NEW): prefer module_inspect(name='...', method='owl')
        for OWL enumeration with module context in one call (M11+)

    Args:
        module: Module name to search within.
        odoo_version: '17.0' / '18.0' / 'auto'.
        bound_model: Optional filter — components matching this model name.
        profile_name: Optional profile filter.
        limit: Cypher LIMIT (default 200). Render cap is 20.
        start_index: Zero-based pagination cursor (default 0).

    Returns:
        Tree text: header + `[ref=fN] component_name : bound_model` rows.

    Example:
        list_owl_components("sale_management", "17.0")
        → OWL components of sale_management (Odoo 17.0)
          ├─ [ref=f1] SaleOrderKanban : sale.order
          └─ [ref=f2] SaleSidebar : (unbound)
    """
    text = _list_owl_components(
        module, odoo_version, bound_model, profile_name, limit, start_index,
        api_key_id=_get_api_key_id(),
    )
    text = _deprecation_banner("module_inspect", "name='...', method='owl'") + text
    return text


@mcp.tool(**READONLY_TOOL_KWARGS)
def list_qweb_templates(
    module: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
    limit: int = 200,
    start_index: int = 0,
) -> str:
    """Enumerate QWeb templates declared in a module.

    TRIGGER when: "list QWeb templates in website_sale", "what QWeb
    templates does module X define", "QWeb template nào trong module Y",
    "all t-name templates in module Z", "show me QWeb inheritance for
    module W"
    PREFER over: find_examples — that tool returns rendered snippets;
    list_qweb_templates gives the inheritance-aware inventory.
    SKIP when: caller wants OWL components (v15+ JS classes) → use
    list_owl_components. Use resolve_view when the template IS an
    ir.ui.view (not a pure QWeb-portal template).
    SKIP when (NEW): you want QWeb template enumeration with module context in one tool →
        prefer module_inspect(name='...', method='qweb') (M11 superset, v0.5+)

    Args:
        module: Module name to search within.
        odoo_version: '17.0' / '18.0' / 'auto'.
        profile_name: Optional profile filter.
        limit: Cypher LIMIT (default 200). Render cap is 20.
        start_index: Zero-based pagination cursor (default 0 = first page).

    Returns:
        Tree text: header + `[ref=vN] xmlid : t-inherit=<parent or (root)>` rows.

    Example:
        list_qweb_templates("website_sale", "17.0")
        → QWeb templates of website_sale (Odoo 17.0)
          ├─ [ref=v1] website_sale.product : t-inherit=(root)
          └─ [ref=v2] website_sale.cart_lines : t-inherit=website_sale.cart
    """
    text = _list_qweb_templates(
        module, odoo_version, profile_name, limit, start_index,
        api_key_id=_get_api_key_id(),
    )
    text = _deprecation_banner("module_inspect", "name='...', method='qweb'") + text
    return text


@mcp.tool(**READONLY_TOOL_KWARGS)
def list_js_patches(
    odoo_version: str = "auto",
    target: str | None = None,
    module: str | None = None,
    era: str | None = None,
    profile_name: str | None = None,
    limit: int = 200,
    start_index: int = 0,
) -> str:
    """Enumerate JS patches across all eras (Widget extend, mixin, OWL patch).

    `era` accepts era1 / era2 / era3 or the stored values extend /
    include / patch (ADR-0023 §5.3).

    TRIGGER when: "list JS patches on hr.employee", "all OWL patches in
    Odoo 17", "Widget extends in v12", "JS patch nào trên model X",
    "tất cả patch() trong module Y"
    PREFER over: find_examples — that tool returns code snippets;
    list_js_patches gives the structured per-target inventory.
    SKIP when: caller wants OWL component declarations (not patches) →
    use list_owl_components. Use find_examples for code-level patterns.
    SKIP when (NEW): prefer module_inspect(name='...', method='js')
        for JS patch enumeration with module context in one call (M11+)

    Args:
        odoo_version: '17.0' / '18.0' / 'auto'.
        target: Optional filter on patched widget/component name.
        module: Optional filter on patching module.
        era: Optional — 'era1' (Widget extend), 'era2' (mixin),
            'era3' (OWL patch); or extend/include/patch.
        profile_name: Optional profile filter.
        limit: Cypher LIMIT (default 200). Render cap is 10.
        start_index: Zero-based pagination cursor (default 0).

    Returns:
        Tree text: header + per-module subtree of
        `[ref=xN] target.patch_name : era=<era>` rows.

    Example:
        list_js_patches(odoo_version="17.0", target="ListController")
        → JS patches on ListController (Odoo 17.0)
          ├─ [odoo] sale_management
          │   └─ [ref=x1] ListController.applyFilters : era=patch
    """
    text = _list_js_patches(
        odoo_version, target, module, era, profile_name, limit, start_index,
        api_key_id=_get_api_key_id(),
    )
    text = _deprecation_banner("module_inspect", "name='...', method='js'") + text
    return text


@mcp.tool(**READONLY_TOOL_KWARGS)
def model_inspect(
    model: str,
    method: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
    *,
    field: str | None = None,
    method_name: str | None = None,
) -> ToolResult:
    """Method-discriminator superset for model-scoped reads. See ADR-0028.

    TRIGGER when: you need to inspect one model from multiple angles in
    succession — summary then fields then methods — to reduce round trips
    vs calling resolve_model / list_fields / list_methods separately.
    Also: "kiểm tra một model nhiều mặt", "xem mọi thông tin của model X"
    PREFER over: chaining resolve_model + list_fields + list_methods when
    you already know which sub-view you want; one call with method= is
    friendlier for LLM context windows.
    SKIP when: you need a single specific drill-down — call the specific
    tool directly (resolve_field, list_methods) for a cleaner trace.

    Args:
        model: Dotted model name, e.g. 'sale.order', 'res.partner'.
        method: One of summary | fields | methods | views | field | method.
            'field' requires field=. 'method' requires method_name=.
        odoo_version: e.g. '17.0', '18.0'. 'auto' = latest indexed.
        profile_name: Optional profile filter.
        field: Required when method='field'. Field technical name.
        method_name: Required when method='method'. Method name.

    Returns:
        Tree text identical to the underlying tool's output.

    Example:
        model_inspect("sale.order", "fields", "17.0")
        → same as list_fields(model="sale.order", odoo_version="17.0")
    """
    text = _model_inspect(
        model=model,
        method=method,
        odoo_version=odoo_version,
        profile_name=profile_name,
        field=field,
        method_name=method_name,
        api_key_id=_get_api_key_id(),
    )
    return ToolResult(content=[TextContent(type="text", text=text)])


@mcp.tool(**READONLY_TOOL_KWARGS)
def module_inspect(
    name: str,
    method: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
) -> ToolResult:
    """Method-discriminator superset for module-scoped reads. See ADR-0028.

    TRIGGER when: you need to inspect one module from multiple angles —
    summary then views then OWL components — reducing round trips vs
    calling describe_module / list_views / list_owl_components separately.
    Also: "khám phá nội dung module X", "module X chứa những gì"
    PREFER over: chaining describe_module + list_views + list_owl_components
    when the discriminator method= captures the exact sub-view needed.
    SKIP when: you need a single entity type — call describe_module,
    list_qweb_templates, or list_js_patches directly for a cleaner trace.

    Args:
        name: Technical module name, e.g. 'sale', 'website_sale'.
        method: One of summary | views | owl | qweb | js.
            'fields' and 'methods' return a guidance stub (model required).
        odoo_version: e.g. '17.0', '18.0'. 'auto' = latest indexed.
        profile_name: Optional profile filter.

    Returns:
        Tree text identical to the underlying tool's output.

    Example:
        module_inspect("sale_management", "owl", "17.0")
        → same as list_owl_components(module="sale_management", odoo_version="17.0")
    """
    text = _module_inspect(
        name=name,
        method=method,
        odoo_version=odoo_version,
        profile_name=profile_name,
        api_key_id=_get_api_key_id(),
    )
    return ToolResult(content=[TextContent(type="text", text=text)])


@mcp.tool(**READONLY_TOOL_KWARGS)
def entity_lookup(
    kind: str,
    *,
    odoo_version: str = "auto",
    profile_name: str | None = None,
    model: str | None = None,
    field: str | None = None,
    method_name: str | None = None,
    xmlid: str | None = None,
    name: str | None = None,
) -> ToolResult:
    """Unified single-entity lookup by kind discriminator. See ADR-0028.

    TRIGGER when: kind of entity is known but the specific tool name is
    unclear — use kind= to dispatch to the right resolver without knowing
    whether to call resolve_model, resolve_field, resolve_view, etc.
    Also: "tra cứu một entity cụ thể khi biết kind", "tìm field/method/view"
    PREFER over: guessing the right tool name; entity_lookup normalises the
    dispatch and returns the same tree text as the underlying tool.
    SKIP when: the specific tool name is already known — call resolve_model,
    resolve_field, resolve_method, resolve_view, describe_module, or
    suggest_pattern directly for a cleaner trace and less cognitive overhead.

    Args:
        kind: One of model | field | method | view | module | pattern.
        odoo_version: e.g. '17.0'. 'auto' = latest indexed.
        profile_name: Optional profile filter.
        model: Required for kind in {model, field, method}.
        field: Required for kind='field'.
        method_name: Required for kind='method'.
        xmlid: Required for kind='view'.
        name: Required for kind in {module, pattern}.

    Returns:
        Tree text identical to the underlying tool's output.

    Example:
        entity_lookup("field", model="sale.order", field="amount_total")
        → same as resolve_field("sale.order", "amount_total")
    """
    text = _entity_lookup(
        kind=kind,
        odoo_version=odoo_version,
        profile_name=profile_name,
        model=model,
        field=field,
        method_name=method_name,
        xmlid=xmlid,
        name=name,
        api_key_id=_get_api_key_id(),
    )
    return ToolResult(content=[TextContent(type="text", text=text)])


# ---------------------------------------------------------------------------
# Wave E — Session-context tools (ADR-0029, M11 WI-E3)
# ---------------------------------------------------------------------------


@mcp.tool(**MUTATING_TOOL_KWARGS)
def set_active_version(odoo_version: str) -> ToolResult:
    """Pin the active Odoo version for this API key (ADR-0029 implicit context).

    TRIGGER when: starting a work session focused on a specific Odoo version
    and you want to drop odoo_version= from every subsequent tool call.
    PREFER over: passing odoo_version='17.0' to every individual tool call;
    this scopes the version once per session with a 24h sliding TTL.
    SKIP when: hopping between multiple versions mid-session — pass
    odoo_version= explicitly to each call instead to avoid confusion.

    Args:
        odoo_version: Concrete version string, e.g. '17.0', '16.0', '18.0'.
            Sentinel values ('auto', 'default', 'latest', 'any', '') are
            rejected — pass a real version number.

    Returns:
        Confirmation receipt with the pinned version and TTL duration.
    """
    normalized = _session.normalize_version_arg(odoo_version)
    if normalized is None:
        return ToolResult(content=[TextContent(type="text",
            text=(
                f"Error: '{odoo_version}' is a sentinel placeholder, not a real version.\n"
                "Pass a concrete version like '17.0' or '16.0'.\n"
                "Use list_available_versions() to see what is indexed."
            )
        )])
    try:
        _session.set_active_version_db(_get_api_key_id(), normalized)
    except Exception as exc:
        return ToolResult(content=[TextContent(type="text",
            text=f"Error persisting session version: {exc}")])
    return ToolResult(content=[TextContent(type="text",
        text=(
            f"Active version set to '{normalized}' for this API key (TTL 24h).\n"
            "Subsequent tool calls that omit odoo_version= will resolve to this version."
        )
    )])


@mcp.tool(**MUTATING_TOOL_KWARGS)
def set_active_profile(profile_name: str | None) -> ToolResult:
    """Pin the active profile for this API key (ADR-0029 implicit context).

    TRIGGER when: working exclusively within one customer profile and you
    want profile filtering applied automatically to all subsequent queries.
    PREFER over: passing profile_name='my-erp-prod' to every tool call;
    this scopes the profile once per session with a 24h sliding TTL.
    SKIP when: comparing across multiple profiles mid-session — pass
    profile_name= explicitly to each call instead.

    Args:
        profile_name: Profile name such as 'viindoo_internal_17' or
            'my-erp-prod'. Pass null / None to clear the active profile
            (subsequent calls revert to cross-profile queries).

    Returns:
        Confirmation receipt with the pinned profile name and TTL duration.
    """
    try:
        _session.set_active_profile_db(_get_api_key_id(), profile_name)
    except Exception as exc:
        return ToolResult(content=[TextContent(type="text",
            text=f"Error persisting session profile: {exc}")])
    if profile_name is None:
        msg = (
            "Active profile cleared for this API key.\n"
            "Subsequent tool calls will query across all profiles."
        )
    else:
        msg = (
            f"Active profile set to '{profile_name}' for this API key (TTL 24h).\n"
            "Subsequent tool calls that omit profile_name= will filter to this profile."
        )
    return ToolResult(content=[TextContent(type="text", text=msg)])


@mcp.tool(**READONLY_TOOL_KWARGS)
def list_available_versions() -> ToolResult:
    """List all Odoo versions indexed in this knowledge base.

    TRIGGER when: unsure which Odoo versions are available, before calling
    set_active_version(), or to validate a version string before querying.
    PREFER over: guessing a version and getting an empty result; use this
    first to confirm what is indexed before running model/field queries.
    SKIP when: the version is already known (e.g. from a prior set_active_version
    confirmation or from a resolve_model result header).

    Returns:
        Sorted list of indexed Odoo versions (newest first), e.g.:
        Indexed Odoo versions (3 total):
        ├─ 17.0
        ├─ 16.0
        └─ 15.0
    """
    with _get_driver().session() as neo4j_session:
        rows = neo4j_session.run("""
            MATCH (m:Module)
            WITH DISTINCT m.odoo_version AS v
            WHERE v <> 'unknown' AND v =~ '\\d+\\.\\d+'
            RETURN v
            ORDER BY toInteger(split(v, '.')[0]) DESC,
                     toInteger(split(v, '.')[1]) DESC
        """).data()

    if not rows:
        return ToolResult(content=[TextContent(type="text",
            text=(
                "No Odoo versions indexed yet.\n"
                "Run `python -m src.indexer index-repo --profile <name>` to index a repo."
            )
        )])

    versions = [r["v"] for r in rows]
    lines = [f"Indexed Odoo versions ({len(versions)} total):"]
    for i, v in enumerate(versions):
        prefix = "└─" if i == len(versions) - 1 else "├─"
        lines.append(f"{prefix} {v}")
    return ToolResult(content=[TextContent(type="text", text="\n".join(lines))])


@mcp.tool(**READONLY_TOOL_KWARGS)
def list_available_profiles() -> ToolResult:
    """List all profiles registered in this knowledge base.

    TRIGGER when: unsure which profiles exist, before calling set_active_profile(),
    or to enumerate tenants/customers before running profile-scoped queries.
    PREFER over: guessing a profile name and getting an empty result; use this
    first to confirm available profiles before filtering queries.
    SKIP when: the profile name is already known (e.g. from a prior
    set_active_profile confirmation or from admin documentation).

    Returns:
        Tree listing of registered profiles with their Odoo version, e.g.:
        Registered profiles (2 total):
        ├─ viindoo_internal_17  (17.0)
        └─ customer_erp_16      (16.0)
    """
    sql = "SELECT name, odoo_version FROM profiles ORDER BY name"
    try:
        with _checkout_pg() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
    except Exception as exc:
        return ToolResult(content=[TextContent(type="text",
            text=f"Error querying profiles: {exc}")])

    if not rows:
        return ToolResult(content=[TextContent(type="text",
            text=(
                "No profiles registered yet.\n"
                "Use the admin UI or `manager` CLI to register a profile."
            )
        )])

    lines = [f"Registered profiles ({len(rows)} total):"]
    for i, (name, odoo_version) in enumerate(rows):
        prefix = "└─" if i == len(rows) - 1 else "├─"
        ver_str = f"  ({odoo_version})" if odoo_version else ""
        lines.append(f"{prefix} {name}{ver_str}")
    return ToolResult(content=[TextContent(type="text", text="\n".join(lines))])


def _mcp_host() -> str:
    from src import config
    return config.get("server", "host", fallback="127.0.0.1")


def _mcp_port() -> int:
    from src import config
    return int(config.get("server", "port", fallback="8002"))


# Health endpoint — registered as custom route on MCP app
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request):
    from src.mcp.health import health_handler
    return await health_handler(request)


if __name__ == "__main__":
    import logging as _logging

    from src import config as _config
    _config.init_dotenv()

    from src.logging_config import configure_logging as _configure_logging
    _configure_logging(level=_logging.INFO)

    from pathlib import Path as _Path

    import uvicorn as _uvicorn
    from starlette.middleware import Middleware as _Middleware
    from starlette.staticfiles import StaticFiles as _StaticFiles

    from src.mcp.middleware import AuthMiddleware

    # Replace mcp.run(...) with explicit app+uvicorn so we can mount /install StaticFiles.
    _app = mcp.http_app(
        transport="streamable-http",
        path="/mcp",
        middleware=[_Middleware(AuthMiddleware)],
    )

    # --- Resilient PG startup: degraded mode + background retry (incident 2026-05-19) ---
    # AuthMiddleware.dispatch calls auth_store() → get_pool() on every authenticated
    # request. If init_pool() never ran, get_pool() raises RuntimeError. Previously
    # we blocked startup on _ensure_pg() — but that turned any DB blip into uvicorn
    # exit(3), and systemd Restart=on-failure happily looped the process forever
    # (~11k restarts in 26h during the May 2026 incident).
    #
    # New behaviour: try once with a short timeout. On failure, log a warning,
    # schedule a background retry every 30s, and let startup complete. The
    # AuthMiddleware returns 503 {"status":"degraded","pg":"unavailable"} for
    # any non-public request until the pool comes up; /health (public) keeps
    # reporting accurate status the whole time.
    _existing_lifespan = _app.router.lifespan_context

    @asynccontextmanager
    async def _lifespan_with_pg(app):
        import asyncio as _asyncio

        from src.constants import PG_BG_RETRY_INTERVAL_SECONDS
        from src.db.pg import is_pool_initialized as _is_pool_initialized

        _log = _logging.getLogger(__name__)
        retry_task: _asyncio.Task | None = None

        try:
            await _asyncio.to_thread(_ensure_pg)
            _log.info("PG pool initialized at startup")
        except Exception as e:  # noqa: BLE001 — any failure → degraded mode
            _log.warning(
                "PG pool init failed at startup — entering DEGRADED mode."
                " Service stays UP; /health returns degraded; non-public requests"
                " return 503 until the pool recovers. Cause: %s",
                str(e)[:300],
            )

            async def _bg_retry_init_pool():
                # Retry on a fixed cadence until the pool comes up OR we get
                # canceled by the lifespan-exit finally block below.
                while not _is_pool_initialized():
                    await _asyncio.sleep(PG_BG_RETRY_INTERVAL_SECONDS)
                    try:
                        await _asyncio.to_thread(_ensure_pg)
                        _log.info(
                            "PG pool initialized after background retry"
                            " — degraded mode cleared",
                        )
                        return
                    except Exception as bg_e:  # noqa: BLE001
                        _log.warning(
                            "PG background retry still failing: %s", str(bg_e)[:300],
                        )

            # Hold a strong reference so the task is not GC'd before completion,
            # AND so the finally block below can cancel + await it on shutdown.
            # Without this, the task is fire-and-forget: ASGI lifespan exit
            # (rapid restart, hot reload) silently cancels it mid-flight.
            retry_task = _asyncio.create_task(_bg_retry_init_pool())

        # Best-effort: warn ops team about legacy Neo4j nodes lacking `profile`
        # property so they know a full reindex is required (per ADR-0016).
        try:
            _drv = _get_driver()
            with _drv.session() as _s:
                _row = _s.run(
                    """
                    MATCH (n)
                    WHERE n:Module OR n:Model OR n:Field OR n:Method
                       OR n:View OR n:QWebTmpl OR n:OWLComp OR n:JSPatch
                    WITH count(CASE WHEN n.profile IS NULL THEN 1 END) AS legacy_count
                    RETURN legacy_count
                    """
                ).single()
                if _row and _row["legacy_count"] > 0:
                    _logging.getLogger(__name__).warning(
                        "%d Neo4j nodes have no `profile` property — these are invisible"
                        " to profile-scoped MCP queries. Run a full reindex per ADR-0016"
                        " to backfill.",
                        _row["legacy_count"],
                    )
        except Exception:
            pass  # startup warning is best-effort — never block startup

        try:
            async with _existing_lifespan(app):
                yield
        finally:
            # Cancel + await the background retry task on shutdown. Skip if
            # the task already completed naturally (PG came back up). Catch
            # CancelledError so the cancel itself doesn't propagate; any
            # other exception is genuine and re-raised by the framework.
            if retry_task is not None and not retry_task.done():
                retry_task.cancel()
                try:
                    await retry_task
                except _asyncio.CancelledError:
                    pass

    _app.router.lifespan_context = _lifespan_with_pg
    # --------------------------------------------------------------------------

    _install_dir = _Path(__file__).parent / "static" / "install"
    if _install_dir.is_dir():
        _app.mount(
            "/install",
            _StaticFiles(directory=str(_install_dir), html=True),
            name="install",
        )

    # Mount feedback API on MCP port so remote users can submit thumbs-up/down.
    # feedback.router exposes POST /api/feedback and GET /api/feedback/{pattern_id}.
    # Auth is already enforced by AuthMiddleware above — no loopback guard needed.
    # We wrap the router in a mini FastAPI sub-app (include_router) and mount it
    # at the root prefix '' so its /api/feedback paths remain unchanged.
    from fastapi import FastAPI as _FastAPI

    from src.web_ui.routes import feedback as _feedback

    _feedback_app = _FastAPI()
    _feedback_app.include_router(_feedback.router)
    _app.mount("", _feedback_app)

    _uvicorn.run(
        _app,
        host=_mcp_host(),
        port=_mcp_port(),
        timeout_graceful_shutdown=0,
        lifespan="on",
        access_log=True,
    )
