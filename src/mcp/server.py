# SPDX-License-Identifier: AGPL-3.0-or-later
# src/mcp/server.py
import math
import os
import threading
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
    MAGIC_FIELDS,
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
from src.mcp.orm import (
    _resolve_orm_chain,
    _validate_depends,
    _validate_domain,
    _validate_relation,
)
from src.mcp.refs import mint_refs
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
    "model_inspect(model='sale.order', method='fields', odoo_version='17.0') for full list".
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

# All 20 OSM tools are read-only queries against a statically-indexed graph.
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


def _get_api_key_id() -> str:
    """Return the API key ID for the current sync context.

    Sync MCP tool wrappers run outside Starlette request context, so we
    cannot extract the real API key from the request.  Return a stable
    sentinel so all sync callers share one ref namespace — per-call refs
    minted by model_inspect/module_inspect tools are stored under the real
    api_key_id, and entity_lookup / on_read_resource resolve under the same key.
    In production, the middleware writes the api_key_id into a thread-local;
    fall back to 'default' when not set (unit tests, CLI invocations).
    """
    return getattr(_api_key_id_local, "value", "default")


# Thread-local storage for API key ID — populated by middleware when available.
_api_key_id_local = threading.local()


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
    from_module: str | None = None,
) -> str:
    """Return a tree overview of a model, optionally scoped to a single declaring module.

    Parameters
    ----------
    model_name:
        Dotted model name, e.g. ``sale.order``.
    odoo_version:
        Odoo version string, e.g. ``17.0``. ``"auto"`` resolves to the latest
        indexed version.
    profile_name:
        Optional profile filter.
    from_module:
        When set, restrict the inheritance-chain layers to rows where the
        declaring module equals this value. Layers from other modules are
        silently filtered out.  ``"<builtin>"`` is never returned regardless
        of this parameter (magic fields live in synthetic space only).
        Default ``None`` preserves the existing behaviour (all modules).
    """
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
              AND ($from_module IS NULL OR m.module = $from_module)
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
            from_module=from_module,
        ).data()

        if not layers:
            if from_module:
                return (
                    f"Model '{model_name}' not found in module '{from_module}'"
                    f" (Odoo {odoo_version})."
                )
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

    # ADR-0023 §1.1: header = "{entity} (Odoo {version})", no decoration.
    # from_module filter info goes as a branch line, not appended to header.
    lines = [f"{model_name} (Odoo {odoo_version})"]
    if from_module:
        lines.append(f"├─ filter: from_module={from_module}")
    lines.append(f"├─ Defined in:     [{base['repo']}] {base['module_name']}")

    if parents:
        parents_str = ", ".join(p["pname"] for p in parents)
        lines.append(f"├─ Inherits from:  {parents_str}")

    if extensions:
        lines.append("├─ Extended by:")
        more_hint = (
            f"model_inspect(model='{model_name}', method='fields', odoo_version='{odoo_version}')"
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
        f"model_inspect(model='{model_name}', method='fields', odoo_version='{odoo_version}')"
        " for full field list",
        f"model_inspect(model='{model_name}', method='methods', odoo_version='{odoo_version}')"
        " for behavior",
    ]))
    return "\n".join(lines)


def _resolve_field(
    model_name: str,
    field_name: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
    from_module: str | None = None,
) -> str:
    """Return detail about a field, optionally scoped to a single declaring module.

    Parameters
    ----------
    model_name:
        Dotted model name, e.g. ``sale.order``.
    field_name:
        Field name to look up.
    odoo_version:
        Odoo version string. ``"auto"`` resolves to the latest indexed version.
    profile_name:
        Optional profile filter.
    from_module:
        When set, only ``Declared in`` rows whose module equals this value are
        returned.  When the field is a magic field (``id``, ``display_name``,
        etc.) it is declared in ``"<builtin>"``; setting ``from_module`` will
        suppress magic-field synthetic rows since ``"<builtin>"`` will not match
        any real module name.  Default ``None`` preserves existing behaviour.
    """
    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        # 5-tier ranking via m_node proxy — see docs/adr/0013
        records = session.run(f"""
            MATCH (f:Field {{name: $fn, model: $mn, odoo_version: $v}})
            WHERE ($profile_name IS NULL OR $profile_name IN f.profile)
              AND ($from_module IS NULL OR f.module = $from_module)
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
        """, fn=field_name, mn=model_name, v=odoo_version, profile_name=profile_name,
            from_module=from_module).data()

    # D2: If not found in graph, check whether it's a magic field.
    # Magic fields are synthetic — not in Neo4j — so we build a synthetic record.
    if not records:
        if from_module is None and field_name in MAGIC_FIELDS:
            ttype, _comodel = MAGIC_FIELDS[field_name]
            lines = [
                f"{model_name}.{field_name} (Odoo {odoo_version})",
                f"├─ Type:     {ttype}",
                "├─ Computed: No",
                "├─ Stored:   Yes",
                "├─ Required: No",
                "├─ Related:  —",
                "├─ Declared in:",
                "│   └─ <builtin>  [ORM magic field — injected at runtime, not in source]",
            ]
            lines.append(format_next_step([
                f"find_examples(query='{model_name}.{field_name} usage'"
                f", odoo_version='{odoo_version}') for real-world patterns",
                f"impact_analysis(entity_type='field'"
                f", entity_name='{model_name}.{field_name}'"
                f", odoo_version='{odoo_version}') for blast radius",
            ]))
            return "\n".join(lines)
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
    # Wave 5: append Next-step footer per ADR-0023 §4. Suggest model_inspect views
    # scoped to the same model when known, plus find_examples for xpath patterns.
    view_model = v_props.get("model")
    next_hints: list[str] = []
    if view_model:
        next_hints.append(
            f"model_inspect(model='{view_model}', method='views', odoo_version='{odoo_version}')"
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
                f"entity_lookup(kind='view', xmlid='{xmlid}', odoo_version='{odoo_version}')"
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
            f"model_inspect(model='{model_name}', method='field', field='{member_name}'"
            f", odoo_version='{odoo_version}') for field detail",
            f"find_deprecated_usage(odoo_version='{odoo_version}')"
            " to widen for deprecated calls",
        ]
    else:  # model
        next_hints = [
            f"model_inspect(model='{model_name}', method='methods', odoo_version='{odoo_version}')"
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
    SKIP when: user wants to see who extends a model → use model_inspect(method='summary');
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


def _build_noqa_suppress(code: str) -> dict[int, set[str]]:
    """Parse noqa comments and return a suppress-set keyed by 1-based line number.

    Each value is a set of rule IDs suppressed on that line, or ``{"*"}`` for a
    bare ``noqa`` (suppresses all rules on that line).

    Examples (comment marker elided to avoid ruff false-positive)::

        noqa: E8001          → {1: {"E8001"}}
        noqa: E8001, W9002   → {1: {"E8001", "W9002"}}
        noqa                 → {1: {"*"}}
    """
    import re as _re

    suppress: dict[int, set[str]] = {}
    for lineno, line in enumerate(code.splitlines(), start=1):
        # Match bare noqa comment with optional rule list.
        m = _re.search(r"#\s*noqa(?::\s*([A-Za-z0-9,\s]+))?", line)
        if not m:
            continue
        ids_str = m.group(1)
        if ids_str:
            ids = {s.strip() for s in ids_str.split(",") if s.strip()}
        else:
            ids = {"*"}
        suppress[lineno] = ids
    return suppress


def _match_lint_rule_lines(code: str, rule: dict) -> list[int]:
    """Return 1-based line numbers in *code* where *rule* matches.

    Uses the same token-overlap logic as :func:`_match_lint_rule` but applies
    it per-line so we can honour ``# noqa`` suppression.  A line matches when
    it contains ≥2 significant tokens from the rule message.

    Returns an empty list when the rule message has fewer than 2 significant
    tokens (same contract as ``_match_lint_rule`` — the rule never fires).
    """
    import re as _re

    msg = (rule.get("message") or "").lower()
    if not msg:
        return []
    rule_tokens = {
        t for t in _re.split(r"[^a-z_]+", msg)
        if len(t) > 3 and t not in _LINT_STOPWORDS
    }
    if len(rule_tokens) < 2:
        return []

    # First check the whole snippet (existing behaviour) — if not triggered at
    # all, skip per-line work.
    code_tokens_all = set(_re.split(r"[^a-z_]+", (code or "").lower()))
    if len(rule_tokens & code_tokens_all) < 2:
        return []

    # Per-line pass to get line numbers.
    hit_lines: list[int] = []
    for lineno, line in enumerate(code.splitlines(), start=1):
        line_lc = line.lower()
        line_tokens = set(_re.split(r"[^a-z_]+", line_lc))
        if len(rule_tokens & line_tokens) >= 2:
            hit_lines.append(lineno)
    # If the whole-code match fired but no individual line triggered ≥2 tokens
    # (tokens spread across lines), attribute the violation to line 1 as a
    # conservative fallback so the caller still receives it.
    if not hit_lines:
        hit_lines = [1]
    return hit_lines


def _lint_check(
    code: str, odoo_version: str = "auto", language: str = "python",
) -> str:
    """Pattern-match user code against indexed LintRule.message (V0).

    Supports ``# noqa`` suppression per line:

    * ``# noqa: E8001`` — suppress rule ``E8001`` on that line only.
    * ``# noqa: E8001, W9002`` — suppress multiple rules on that line.
    * ``# noqa`` (bare) — suppress all rules matched on that line.
    """
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

    # Build noqa suppress set from the input code.
    suppress = _build_noqa_suppress(code)

    violations: list[dict] = []
    for rule in rules:
        hit_lines = _match_lint_rule_lines(code, rule)
        if not hit_lines:
            continue
        rule_id = rule.get("rule_id") or "?"
        # A violation is suppressed only when ALL matched lines suppress this rule.
        suppressed_lines = sum(
            1 for ln in hit_lines
            if ln in suppress and ("*" in suppress[ln] or rule_id in suppress[ln])
        )
        if suppressed_lines < len(hit_lines):
            violations.append(rule)

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
        profile_name: Optional profile filter (e.g. 'my_profile').
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
                f" (use model_inspect(model='{first_def}', method='fields',"
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
                f" (use model_inspect(model='{first_ext}', method='fields',"
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
            f"model_inspect(model='{first_target}', method='fields', odoo_version='{odoo_version}')"
            " for declared fields",
            f"model_inspect(model='{first_target}', method='views', odoo_version='{odoo_version}')"
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
    `module` restricts to one declaring module.  When ``module`` is set,
    magic-field synthetic rows are suppressed (module=``"<builtin>"`` would
    not match any real module filter value).
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

    # D2: Build magic-field prelude for page 0 only when no module filter suppresses them.
    # Magic fields are rendered as a FIXED <builtin> prelude block that is OUTSIDE the
    # pagination/truncation logic for real fields.  The "Showing rows X–Y of N" line and
    # all start_index arithmetic operate ONLY on real (Neo4j) fields.
    # Dedup: skip a magic field if the model already declares it in Neo4j anywhere (model-
    # scoped, not page-scoped — fields on page 2+ would not be in `rows` and would cause
    # duplicates for e.g. display_name, write_date that appear late in the field list).
    magic_prelude_rows: list[dict] = []
    if start_index == 0 and module is None:
        magic_names_list = list(MAGIC_FIELDS.keys())
        with _get_driver().session() as _dedup_session:
            _dedup_rec = _dedup_session.run(
                """
                MATCH (f:Field {model: $m, odoo_version: $v})
                WHERE f.name IN $magic_names
                  AND ($profile_name IS NULL OR $profile_name IN f.profile)
                  AND f.module <> '__unresolved__'
                RETURN collect(DISTINCT f.name) AS names
                """,
                m=model, v=odoo_version, magic_names=magic_names_list,
                profile_name=profile_name,
            ).single()
        existing_names: set[str] = set(_dedup_rec["names"]) if _dedup_rec else set()
        magic_prelude_rows = [
            {
                "name": fname,
                "ttype": ttype,
            }
            for fname, (ttype, _comodel) in MAGIC_FIELDS.items()
            if fname not in existing_names
            and (kind is None or kind == ttype)
        ]

    header = f"Fields of {model} (Odoo {odoo_version})"

    # Render the <builtin> prelude block (always shown in full, no refs, not paginated).
    # Group header matches the old "repo=None → '?', module='<builtin>'" format so that
    # existing tests checking ``"<builtin>" in out`` continue to pass.
    lines = [header]
    if magic_prelude_rows:
        lines.append("├─ [?] <builtin>")
        builtin_tagged = [f"{r['name']} : {r['ttype']}" for r in magic_prelude_rows]
        lines.extend(render_list_block(builtin_tagged))

    if total == 0:
        # No real declared fields.
        if magic_prelude_rows:
            # Model has no declared fields but magic fields are present — the builtin block
            # IS the content. ADR-0023 §1.6: "(none)" means "empty IS the answer"; when
            # magic rows exist, the answer is not empty. Do NOT emit "(none)".
            # The builtin block was already appended above. Just add the Next footer.
            next_line = format_next_step([
                f"model_inspect(model='{model}', method='methods', odoo_version='{odoo_version}')"
                " for behavior",
            ])
            lines.append(next_line)
        else:
            # Truly no fields at all (all filtered out by kind/module/profile, or model unknown).
            # Emit "(none)" sentinel so callers can detect completely empty result.
            lines.append("├─ (none)")
            next_line = format_next_step([
                f"model_inspect(model='{model}', method='methods', odoo_version='{odoo_version}')"
                " for behavior",
            ])
            lines.append(next_line)
        return "\n".join(lines)

    # Mint opaque refs for real (Neo4j) rows only.
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

    for key in order:
        repo, mod_name = key
        lines.append(f"├─ [{repo}] {mod_name}")
        sub_items = groups[key]
        more_hint = (
            f"model_inspect(model='{model}', method='fields', odoo_version='{odoo_version}')"
            f" with limit={max(limit * 2, total)} for full list"
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

    # Pagination hint — counts ONLY real fields (total from Neo4j, not +magic).
    shown = len(rows)
    end_index = start_index + shown
    has_more = total > end_index

    if has_more:
        # Pagination continuation hint (plain text, NOT <error> tag — ADR-0023
        # §Appendix B item #2: pagination is routine, not failure).
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total}."
            f" Call model_inspect(model='{model}', method='fields', odoo_version='{odoo_version}',"
            f" start_index={end_index}) for next {min(cap, total - end_index)}."
        )
    elif start_index > 0:
        # Final page of a paginated sequence — disclose position.
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total} (last page)."
        )

    # Wave 5: Next-step footer per ADR-0023 §4. Prefer a real field name for the
    # drill-down hint; fall back to first magic field if no real field on this page.
    first_real_field = rows[0]["name"] if rows else None
    first_hint_field = first_real_field or (
        magic_prelude_rows[0]["name"] if magic_prelude_rows else None
    )
    next_hints: list[str] = []
    if first_hint_field:
        next_hints.append(
            f"model_inspect(model='{model}', method='field', field='{first_hint_field}'"
            f", odoo_version='{odoo_version}') for full chain",
        )
    next_hints.append(
        f"model_inspect(model='{model}', method='methods', odoo_version='{odoo_version}')"
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
            f"model_inspect(model='{model}', method='fields', odoo_version='{odoo_version}')"
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
            f"model_inspect(model='{model}', method='methods', odoo_version='{odoo_version}')"
            f" with limit={max(limit * 2, total)} for full list"
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
            f" Call model_inspect(model='{model}', method='methods', odoo_version='{odoo_version}',"
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
            f"model_inspect(model='{model}', method='method', method_name='{first_method}'"
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
            f"model_inspect(model='{model}', method='methods', odoo_version='{odoo_version}')"
            " for behavior"
        )
        pager_tool = f"model_inspect(model='{model}', method='views', odoo_version='{odoo_version}'"
    else:
        header = f"Views in module '{module}' (Odoo {odoo_version})"
        empty_hint = (
            f"describe_module(name='{module}', odoo_version='{odoo_version}')"
            " for model fields"
        )
        pager_tool = (
            f"module_inspect(name='{module}', method='views',"
            f" odoo_version='{odoo_version}'"
        )

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
            f"entity_lookup(kind='view', xmlid='{first_xmlid}', odoo_version='{odoo_version}')"
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
            # Wave 5: still emit Next: footer suggesting module_inspect(method='js') for
            # era1 widget extensions (the natural era-aware drill-down).
            next_line = format_next_step([
                f"module_inspect(name='{module}', method='js'"
                f", odoo_version='{odoo_version}') for legacy widget extends",
            ])
            return (
                f"OWL components of {module} (Odoo {odoo_version})\n"
                "├─ (none) — Warning: No OWL components in v8-v13"
                " (Widget era). Use module_inspect(method='js') for legacy"
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
        # Wave 5: suggest module_inspect qweb / js as siblings.
        lines.append(format_next_step([
            f"module_inspect(name='{module}', method='qweb'"
            f", odoo_version='{odoo_version}') for QWeb templates",
            f"module_inspect(name='{module}', method='js', odoo_version='{odoo_version}')"
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
        f"module_inspect(name='{module}', method='owl'"
        f", odoo_version='{odoo_version}') with limit={max(limit * 2, total)}"
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
            f" Call module_inspect(name='{module}', method='owl',"
            f" odoo_version='{odoo_version}',"
            f" start_index={end_index}) for next {min(cap, total - end_index)}."
        )
    elif start_index > 0:
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total} (last page)."
        )

    # Wave 5: Next-step footer per ADR-0023 §4.
    lines.append(format_next_step([
        f"module_inspect(name='{module}', method='qweb', odoo_version='{odoo_version}')"
        " for QWeb templates",
        f"module_inspect(name='{module}', method='js', odoo_version='{odoo_version}')"
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
            f"module_inspect(name='{module}', method='owl', odoo_version='{odoo_version}')"
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
        f"module_inspect(name='{module}', method='qweb'"
        f", odoo_version='{odoo_version}') with limit={max(limit * 2, total)}"
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
            f" Call module_inspect(name='{module}', method='qweb',"
            f" odoo_version='{odoo_version}',"
            f" start_index={end_index}) for next {min(cap, total - end_index)}."
        )
    elif start_index > 0:
        lines.append(
            f"├─ Showing rows {start_index + 1}–{end_index} of {total} (last page)."
        )

    # Wave 5: Next-step footer per ADR-0023 §4.
    lines.append(format_next_step([
        f"module_inspect(name='{module}', method='owl', odoo_version='{odoo_version}')"
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
                f"module_inspect(name='{module}', method='owl'"
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
            f"module_inspect(name='{mod_name}', method='js', odoo_version='{odoo_version}')"
            f" with limit={max(limit * 2, total)} for full list"
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
            f" Call module_inspect(name='{module or '...'}', method='js',"
            f" odoo_version='{odoo_version}',"
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
            f"module_inspect(name='{module}', method='owl'"
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
            f"model_inspect(model='{model}', method='methods', odoo_version='{to_version}')"
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
        f"model_inspect(model='{model}', method='method', method_name='{method}'"
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
            f"model_inspect(model='{model}', method='methods', odoo_version='{v}')"
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
        f"model_inspect(model='{model}', method='method', method_name='{method}'"
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
    one round-trip. user wants module field/method details → use model_inspect;
    user wants code examples from a module → use find_examples

    Args:
        name: Module technical name (e.g. 'sale', 'helpdesk', 'viin_helpdesk').
        odoo_version: '17.0' / '18.0' / 'auto'.
        profile_name: Optional profile filter (e.g. 'my_profile').
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
    PREFER over: model_inspect(method='method') — adds super() safety guidance
    and anti-patterns, not just the chain listing
    SKIP when: full override chain only → model_inspect(method='method');
    design pattern guidance → suggest_pattern

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
            f"model_inspect(model='{model_name}', method='fields', odoo_version='{odoo_version}')"
            " for full field list",
            f"model_inspect(model='{model_name}', method='methods', odoo_version='{odoo_version}')"
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
                f"model_inspect(model='{v_props.get('model')}', method='views',"
                f" odoo_version='{odoo_version}')"
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
                f"model_inspect(model='{defines[0]['name']}', method='fields'"
                f", odoo_version='{odoo_version}') for declared fields",
                f"model_inspect(model='{defines[0]['name']}', method='views'"
                f", odoo_version='{odoo_version}') for module views",
            ]
            if defines
            else (
                [
                    f"model_inspect(model='{extends[0]['name']}', method='fields'"
                    f", odoo_version='{odoo_version}') for declared fields",
                    f"model_inspect(model='{extends[0]['name']}', method='views'"
                    f", odoo_version='{odoo_version}') for module views",
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
            f"model_inspect(model='{model}', method='field', field='{first_field}'"
            f", odoo_version='{odoo_version}') for full chain",
        )
    next_hints.append(
        f"model_inspect(model='{model}', method='methods', odoo_version='{odoo_version}')"
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
            f"model_inspect(model='{model}', method='method', method_name='{first_method}'"
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
    (models, views, JS), not just YES/NO. Also prefer over model_inspect
    when the question is about a module overview, not a single model.
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


@mcp.tool(**READONLY_TOOL_KWARGS)
def model_inspect(
    model: str,
    method: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
    *,
    field: str | None = None,
    method_name: str | None = None,
    start_index: int = 0,
    limit: int = 200,
    from_module: str | None = None,
    kind: str | None = None,
    view_type: str | None = None,
) -> ToolResult:
    """Method-discriminator superset for model-scoped reads. See ADR-0028.

    TRIGGER when: inspecting one model from multiple angles (summary, fields,
    methods, views) — fewer round trips than separate calls.
    Also: "kiểm tra một model nhiều mặt", "xem mọi thông tin của model X"
    PREFER over: separate per-view calls when you know the sub-view; one call
    with method= is friendlier for LLM context windows.
    SKIP when: cross-model entity dispatch by kind — use entity_lookup.

    Args:
        model: Dotted model name, e.g. 'sale.order'.
        method: One of summary | fields | methods | views | field | method.
            'field' requires field=. 'method' requires method_name=.
        odoo_version: e.g. '17.0'. 'auto' = latest indexed.
        profile_name: Optional profile filter.
        field: Required when method='field'.
        method_name: Required when method='method'.
        start_index: Pagination cursor for fields/methods/views (zero-based).
        limit: Max rows per page (default 200).
        from_module: Restrict to rows declared in this module (summary/fields/field).
        kind: Filter fields by ttype, e.g. 'many2one' — method='fields' only.
        view_type: Filter views by type, e.g. 'form' — method='views' only.
    """
    text = _model_inspect(
        model=model,
        method=method,
        odoo_version=odoo_version,
        profile_name=profile_name,
        field=field,
        method_name=method_name,
        api_key_id=_get_api_key_id(),
        start_index=start_index,
        limit=limit,
        from_module=from_module,
        kind=kind,
        view_type=view_type,
    )
    return ToolResult(content=[TextContent(type="text", text=text)])


@mcp.tool(**READONLY_TOOL_KWARGS)
def module_inspect(
    name: str,
    method: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
    start_index: int = 0,
    limit: int = 200,
    view_type: str | None = None,
    bound_model: str | None = None,
    era: str | None = None,
    target: str | None = None,
) -> ToolResult:
    """Method-discriminator superset for module-scoped reads. See ADR-0028.

    TRIGGER when: you need to inspect one module from multiple angles —
    summary then views then OWL components — reducing round trips vs
    multiple separate module_inspect or describe_module calls.
    Also: "khám phá nội dung module X", "module X chứa những gì"
    PREFER over: chaining describe_module + multiple module_inspect calls
    when the discriminator method= captures the exact sub-view needed.
    SKIP when: you need only a summary — use describe_module directly.

    Args:
        name: Technical module name, e.g. 'sale', 'website_sale'.
        method: One of summary | views | owl | qweb | js.
            'fields' and 'methods' return a guidance stub (model required).
        odoo_version: e.g. '17.0', '18.0'. 'auto' = latest indexed.
        profile_name: Optional profile filter.
        start_index: Pagination cursor for views/owl/qweb/js (zero-based).
        limit: Max rows per page for views/owl/qweb/js (default 200).
        view_type: Filter views by type, e.g. 'form'/'tree' — method='views' only.
        bound_model: Filter OWL components bound to a model — method='owl' only.
        era: era1|era2|era3 — filter JS patches by era — method='js' only.
        target: filter JS patches by patched target — method='js' only.
    """
    text = _module_inspect(
        name=name,
        method=method,
        odoo_version=odoo_version,
        profile_name=profile_name,
        api_key_id=_get_api_key_id(),
        start_index=start_index,
        limit=limit,
        view_type=view_type,
        bound_model=bound_model,
        era=era,
        target=target,
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
    from_module: str | None = None,
) -> ToolResult:
    """Unified single-entity lookup by kind discriminator. See ADR-0028.

    TRIGGER when: kind of entity is known but you're unsure which method=
    to use on model_inspect — use kind= to dispatch without knowing whether
    to call model_inspect, module_inspect, or describe_module.
    Also: "tra cứu một entity cụ thể khi biết kind", "tìm field/method/view"
    PREFER over: guessing the right superset tool + method combination;
    entity_lookup normalises the dispatch and returns the same tree text.
    SKIP when: the entity kind and tool are already known — call model_inspect,
    module_inspect, or describe_module directly for a cleaner trace.

    Args:
        kind: One of model | field | method | view | module | pattern.
        odoo_version: e.g. '17.0'. 'auto' = latest indexed.
        profile_name: Optional profile filter.
        model: Required for kind in {model, field, method}.
        field: Required for kind='field'.
        method_name: Required for kind='method'.
        xmlid: Required for kind='view'.
        name: Required for kind in {module, pattern}.
        from_module: Optional module filter — restrict results to rows declared
            in this module only (kind='model' and kind='field').

    Returns:
        Tree text identical to the underlying tool's output.

    Example:
        entity_lookup("field", model="sale.order", field="amount_total")
        → same as model_inspect(model="sale.order", method="field", field="amount_total")
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
        from_module=from_module,
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
    # Sanity-check: confirm the version is actually indexed in Neo4j before pinning it.
    try:
        with _get_driver().session() as neo4j_session:
            hit = neo4j_session.run(
                "MATCH (m:Module {odoo_version: $v}) RETURN m LIMIT 1",
                v=normalized,
            ).data()
        if not hit:
            # Version not indexed — fetch available list for the error message.
            with _get_driver().session() as neo4j_session:
                rows = neo4j_session.run("""
                    MATCH (m:Module)
                    WITH DISTINCT m.odoo_version AS v
                    WHERE v <> 'unknown' AND v =~ '\\d+\\.\\d+'
                    RETURN v
                    ORDER BY toInteger(split(v, '.')[0]) DESC,
                             toInteger(split(v, '.')[1]) DESC
                """).data()
            available = [r["v"] for r in rows]
            if available:
                avail_str = ", ".join(available)
                hint = f"Indexed versions: {avail_str}"
            else:
                hint = "No versions indexed yet — run the indexer first."
            return ToolResult(content=[TextContent(type="text",
                text=(
                    f"Error: version '{normalized}' is not indexed in this knowledge base.\n"
                    f"├─ {hint}\n"
                    "└─ Use list_available_versions() to see what is available."
                )
            )])
    except Exception as exc:
        return ToolResult(content=[TextContent(type="text",
            text=f"Error checking indexed versions: {exc}")])
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
        profile_name: Profile name such as 'internal_17' or
            'my-erp-prod'. Pass null / None to clear the active profile
            (subsequent calls revert to cross-profile queries).

    Returns:
        Confirmation receipt with the pinned profile name and TTL duration.
    """
    # Validate the profile exists before pinning it (None = clear, always valid).
    if profile_name is not None:
        try:
            with _checkout_pg() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM profiles WHERE name=%s", (profile_name,))
                    found = cur.fetchone()
        except Exception as exc:
            return ToolResult(content=[TextContent(type="text",
                text=f"Error checking profiles: {exc}")])
        if not found:
            # Profile not registered — list available ones for the error message.
            try:
                with _checkout_pg() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT name FROM profiles ORDER BY name")
                        rows = cur.fetchall()
                available = [r[0] for r in rows]
            except Exception:
                available = []
            if available:
                avail_str = ", ".join(available)
                hint = f"Registered profiles: {avail_str}"
            else:
                hint = "No profiles registered yet — use the admin UI or manager CLI."
            return ToolResult(content=[TextContent(type="text",
                text=(
                    f"Error: profile '{profile_name}' is not registered.\n"
                    f"├─ {hint}\n"
                    "└─ Use list_available_profiles() to see what is available."
                )
            )])
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
    confirmation or from a model_inspect result header).

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
        ├─ my_profile_17  (17.0)
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


# ---------------------------------------------------------------------------
# M10A Stylesheet tools — D5 resolve_stylesheet + D6 find_style_override
# ---------------------------------------------------------------------------


def _resolve_stylesheet(
    module: str,
    odoo_version: str = "auto",
) -> str:
    """Impl for resolve_stylesheet tool — no FastMCP wrapper overhead.

    Returns a tree listing all :Stylesheet nodes for *module* at *odoo_version*
    with their language, stat counters, and BFS :IMPORTS chain.
    """
    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        rows = session.run(
            """
            MATCH (ss:Stylesheet {module: $mod, odoo_version: $v})
            RETURN ss.file_path AS file_path,
                   ss.language AS language,
                   ss.selector_count AS selector_count,
                   ss.variable_count AS variable_count,
                   ss.import_count AS import_count,
                   ss.mixin_count AS mixin_count
            ORDER BY ss.file_path ASC
            """,
            mod=module, v=odoo_version,
        ).data()

    if not rows:
        footer = hints_for("resolve_stylesheet", name=module, ver=odoo_version)
        recovery = (
            "Recovery: describe_module(name=..., odoo_version=...) to verify module exists."
        )
        if footer:
            # footer ends with └─ Next: — use ├─ for Recovery so └─ stays last.
            lines = [
                f"resolve_stylesheet({module!r}, {odoo_version!r})",
                f"├─ not found — no Stylesheet nodes indexed for module '{module}'.",
                f"├─ {recovery}",
                footer,
            ]
        else:
            lines = [
                f"resolve_stylesheet({module!r}, {odoo_version!r})",
                f"├─ not found — no Stylesheet nodes indexed for module '{module}'.",
                f"└─ {recovery}",
            ]
        return "\n".join(lines)

    # I5: Batch-query all :IMPORTS edges in ONE session before the render loop
    # (avoids N+1 sessions — one session per row that had imp > 0).
    # Pattern: UNWIND file paths → collect imports per source, return as map.
    fps_with_imports = [r["file_path"] for r in rows if (r["import_count"] or 0) > 0]
    imports_by_fp: dict[str, list[dict]] = {}
    if fps_with_imports:
        with _get_driver().session() as session:
            batch_rows = session.run(
                """
                UNWIND $fps AS fp
                MATCH (src:Stylesheet {file_path: fp, module: $mod, odoo_version: $v})
                      -[:IMPORTS]->(tgt:Stylesheet)
                RETURN fp, tgt.file_path AS import_path, tgt.module AS import_module
                ORDER BY fp ASC, tgt.file_path ASC
                """,
                fps=fps_with_imports, mod=module, v=odoo_version,
            ).data()
        for br in batch_rows:
            imports_by_fp.setdefault(br["fp"], []).append(
                {"import_path": br["import_path"], "import_module": br["import_module"]}
            )

    header = f"resolve_stylesheet({module!r}, {odoo_version!r})"
    lines = [header, f"├─ Stylesheets: {len(rows)} file(s)"]

    for idx, row in enumerate(rows):
        is_last_row = idx == len(rows) - 1
        row_prefix = "└─" if is_last_row else "├─"
        fp = row["file_path"]
        lang = row["language"] or "css"
        sel = row["selector_count"] or 0
        var = row["variable_count"] or 0
        imp = row["import_count"] or 0
        mix = row["mixin_count"] or 0

        # Stat summary line
        stats_parts = [f"lang={lang}", f"selectors={sel}", f"vars={var}"]
        if mix:
            stats_parts.append(f"mixins={mix}")
        if imp:
            stats_parts.append(f"imports={imp}")

        # I4: Use grammar-valid prefixes only (per ADR-0023 §1, tested by
        # test_grammar_consistency_all_tools).  The import entries are rendered
        # at the same depth as Stats (not deeper), so they always land on a
        # valid allowed_start regardless of whether this is the last row:
        #   non-last row: sub_prefix="│   " → import lines start with "│   │   ├─" (valid)
        #   last row:     sub_prefix="    " → import lines start with "│       ├─" (valid)
        # Nesting import entries one level deeper (│   {sub_prefix}    {imp_pfx}) would
        # produce "│           ├─" for last-row which is NOT in allowed_starts.
        lines.append(f"│   {row_prefix} {fp}")
        sub_prefix = "    " if is_last_row else "│   "
        import_rows = imports_by_fp.get(fp, []) if imp > 0 else []

        if import_rows:
            lines.append(f"│   {sub_prefix}├─ Stats: {', '.join(stats_parts)}")
            lines.append(f"│   {sub_prefix}├─ Imports ({len(import_rows)}):")
            for i_idx, ir in enumerate(import_rows):
                is_last_imp = i_idx == len(import_rows) - 1
                imp_prefix = "└─" if is_last_imp else "├─"
                lines.append(
                    f"│   {sub_prefix}{imp_prefix} {ir['import_path']}"
                    f" [{ir['import_module']}]"
                )
        elif imp > 0:
            # imp_count > 0 but batch returned no edges (edges not yet resolved)
            lines.append(f"│   {sub_prefix}├─ Stats: {', '.join(stats_parts)}")
            lines.append(
                f"│   {sub_prefix}└─ Imports: edges not yet resolved (re-index to backfill)."
            )
        else:
            lines.append(f"│   {sub_prefix}├─ Stats: {', '.join(stats_parts)}")
            lines.append(f"│   {sub_prefix}└─ Imports: none")

    footer = hints_for("resolve_stylesheet", name=module, ver=odoo_version)
    if footer:
        lines.append(footer)
    return "\n".join(lines)


def _find_style_override(
    selector_or_variable: str,
    odoo_version: str = "auto",
    limit: int = 5,
    *,
    _driver=None,
    _pg_conn=None,
    _embedder=None,
) -> str:
    """Impl for find_style_override tool — no FastMCP wrapper overhead.

    Performs pgvector ANN on chunk_type ∈ {css, scss} to find stylesheets
    declaring *selector_or_variable*, then traverses :IMPORTS to show which
    modules re-declare the same selector (override order — last writer wins
    in CSS cascade, first-match wins in SCSS @import chain).
    """
    if not selector_or_variable.strip():
        return (
            "find_style_override: empty selector_or_variable — provide a CSS selector,"
            " SCSS variable, or mixin name.\nFound 0 results\n"
        )

    from src.embedding.instructions import INSTRUCT_NL_TO_CODE

    driver = _driver or _get_driver()
    try:
        embedder = _embedder or _get_embedder()
    except Exception as e:
        return (
            f"find_style_override: embedder unavailable — {type(e).__name__}: {e}\n"
            "Hint: check Ollama server is running and EMBEDDER_MODEL is loaded.\n"
            "Found 0 results\n"
            f"{hints_for('find_style_override', module='', ver=odoo_version)}"
        )

    with driver.session() as session:
        odoo_version = _resolve_version(odoo_version, session)

    try:
        query_vec = embedder.embed([INSTRUCT_NL_TO_CODE + selector_or_variable])[0]
    except Exception as e:
        return (
            f"find_style_override: embedding failed — {type(e).__name__}: {e}\n"
            "Found 0 results\n"
            f"{hints_for('find_style_override', module='', ver=odoo_version)}"
        )

    _pg_ctx = nullcontext(_pg_conn) if _pg_conn is not None else _checkout_pg()
    with _pg_ctx as pg:
        with pg.cursor() as cur:
            placeholders = "%s, %s"
            cur.execute(
                f"""SELECT chunk_type, module, entity_name, file_path,
                           chunk_idx, content, 1 - (vec <=> %s::vector) AS cosine
                    FROM embeddings
                    WHERE odoo_version = %s AND chunk_type IN ({placeholders})
                    ORDER BY vec <=> %s::vector LIMIT %s""",
                [query_vec, odoo_version, "css", "scss", query_vec,
                 min(limit, FIND_EXAMPLES_ANN_LIMIT)],
            )
            raw = [
                dict(chunk_type=r[0], module=r[1], entity_name=r[2],
                     file_path=r[3], chunk_idx=r[4], content=r[5], cosine=float(r[6]))
                for r in cur.fetchall()
            ]

    header = (
        f'find_style_override: "{selector_or_variable}" ({odoo_version})\n'
        f"Found {len(raw)} result(s)\n"
    )
    if not raw:
        footer = hints_for("find_style_override", module="", ver=odoo_version)
        return header + (footer if footer else "")

    # For each hit, check :IMPORTS override chain — which modules re-declare
    # the same selector (import same file path chain).
    sep = "─" * 41
    lines = [header]
    for i, chunk in enumerate(raw, 1):
        entity = f'[{chunk["module"]}] {chunk["entity_name"]}'
        chunk_label = chunk["chunk_type"]
        if chunk["chunk_idx"] > 0:
            chunk_label += f" chunk {chunk['chunk_idx'] + 1}"
        lines.append(sep)
        lines.append(f"#{i} · score {chunk['cosine']:.2f} · {chunk_label} · {entity}")
        lines.append(f"   File: {chunk['file_path']}")

        # Find stylesheets that import this file (override chain — BFS depth 1)
        with driver.session() as session:
            importers = session.run(
                """
                MATCH (tgt:Stylesheet {file_path: $fp, odoo_version: $v})
                      <-[:IMPORTS]-(src:Stylesheet)
                RETURN src.file_path AS importer_path, src.module AS importer_module
                ORDER BY src.file_path ASC
                """,
                fp=chunk["file_path"], v=odoo_version,
            ).data()

        if importers:
            lines.append(f"   Override chain ({len(importers)} importer(s)):")
            for imp in importers:
                lines.append(
                    f"   ├─ {imp['importer_path']} [{imp['importer_module']}]"
                )
        else:
            lines.append("   Override chain: no importers found (no :IMPORTS edges).")

        lines.append("   ┌" + "─" * 42)
        for line in chunk["content"].splitlines():
            lines.append(f"   │ {line}")
        lines.append("   └" + "─" * 42)
        lines.append("")

    # Pass top-result module so hints render useful resolve_stylesheet/describe_module calls.
    top_module = raw[0]["module"] if raw else ""
    footer = hints_for("find_style_override", module=top_module, ver=odoo_version)
    if footer:
        lines.append(footer)
    return "\n".join(lines)


@mcp.tool(**READONLY_TOOL_KWARGS)
def resolve_stylesheet(
    module: str,
    odoo_version: str = "auto",
) -> str:
    """Enumerate CSS/SCSS stylesheets for an Odoo module with import chain.

    TRIGGER when: "what stylesheets does module X have", "show CSS files in
    website_sale", "list SCSS imports for web module", "module Y có file CSS/SCSS
    nào", "xem import chain stylesheet của module Z"
    PREFER over: find_style_override when you want an overview of all stylesheets
    in a module, not a specific selector search.
    SKIP when: searching for a specific CSS selector or SCSS variable across
    modules — use find_style_override (ANN search).

    Args:
        module: Odoo module technical name (e.g. 'web', 'website_sale').
        odoo_version: e.g. '17.0'. Default 'auto' = latest indexed.

    Returns:
        Tree listing each stylesheet with language, stat counters
        (selectors, vars, mixins, imports), and resolved :IMPORTS chain.

    Example:
        resolve_stylesheet("web", "17.0")
        → resolve_stylesheet('web', '17.0')
          ├─ Stylesheets: 2 file(s)
          │   ├─ /path/web/static/src/css/main.css
          │   │   ├─ Stats: lang=css, selectors=42, vars=0
          │   │   └─ Imports: none
          │   └─ /path/web/static/src/scss/variables.scss
          │       ├─ Stats: lang=scss, selectors=0, vars=15, mixins=3, imports=1
          │       ├─ Imports (1):
          │       └─ /path/web/static/src/scss/base.scss [web]
          └─ Next: find_style_override(...) | describe_module(...)

    See also: odoo://{version}/stylesheet/{module}/{file_path*}
    """
    return _resolve_stylesheet(module, odoo_version)


@mcp.tool(**READONLY_TOOL_KWARGS)
def find_style_override(
    selector_or_variable: str,
    odoo_version: str = "auto",
    limit: int = 5,
) -> str:
    """Find CSS selectors or SCSS variables/mixins across modules + override order.

    Uses pgvector ANN on indexed css/scss chunks to locate declarations, then
    traces :IMPORTS edges to show which modules re-declare the same selector
    (override order — last writer wins in CSS cascade).

    TRIGGER when: "which module overrides .o_form_view selector", "where is
    $primary variable defined", "find CSS override for .btn-primary", "module
    nào override selector X", "tìm định nghĩa biến SCSS Y trong codebase"
    PREFER over: find_examples when looking specifically for CSS/SCSS patterns
    rather than Python/XML code examples.
    SKIP when: you want a full list of all stylesheets in a module — use
    resolve_stylesheet instead.

    Args:
        selector_or_variable: CSS selector, SCSS variable (e.g. '$primary'),
            or mixin name to search for.
        odoo_version: e.g. '17.0'. Default 'auto' = latest indexed.
        limit: Max results to return (default 5).

    Returns:
        Ranked ANN hits with chunk content, cosine score, and :IMPORTS
        override chain showing which modules import the matched file.

    Example:
        find_style_override(".o_list_view", "17.0")
        → find_style_override: ".o_list_view" (17.0)
          Found 2 result(s)
          ─────...
          #1 · score 0.87 · css · [web] selector:.o_list_view
             File: /path/web/static/src/css/views.css
             Override chain (1 importer(s)):
             ├─ /path/website/static/src/scss/views.scss [website]
    """
    return _find_style_override(selector_or_variable, odoo_version, limit)


@mcp.tool(**READONLY_TOOL_KWARGS)
def resolve_orm_chain(
    model: str,
    dotted_path: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
) -> str:
    """Walk a dotted ORM field path and return the terminal field type.

    Traverses 'partner_id.country_id.code' hop by hop across the indexed Field
    graph (following many2one/one2many/many2many comodels), reporting the
    terminal type or the exact hop where the path breaks.

    TRIGGER when: "what type is sale.order.partner_id.country_id.code", "does
    this dotted path resolve", "trace a field path", "field nào ở cuối chain",
    "kiểm tra đường dẫn field a.b.c có hợp lệ không"
    PREFER over: entity_lookup(kind='field') when you have a multi-hop dotted
    path rather than a single field.
    SKIP when: validating a whole domain or @api.depends — use validate_domain
    / validate_depends (they call this primitive per term).

    Args:
        model: Root dotted model name, e.g. 'sale.order'.
        dotted_path: Dotted field path, e.g. 'partner_id.country_id.code'.
        odoo_version: e.g. '17.0'. 'auto' = latest indexed.
        profile_name: Optional profile filter.

    Returns:
        Tree: one line per resolved hop (field : type -> comodel), terminal
        tagged, or a BROKEN line naming the first unresolved hop.
    """
    return _resolve_orm_chain(model, dotted_path, odoo_version, profile_name)


@mcp.tool(**READONLY_TOOL_KWARGS)
def validate_domain(
    model: str,
    domain: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
) -> str:
    """Validate a search domain's field-paths and operators against the graph.

    Parses the domain literal and checks each (field_path, operator, value)
    term: every field-path hop must resolve in the Field graph, and the operator
    must be valid for the version ('any'/'not any' only exist from v17). Catches
    hallucinated fields before they reach a user.

    TRIGGER when: "is this domain valid", "check domain [('x','=',1)]", "validate
    search domain for sale.order", "domain này có field sai không", "kiểm tra
    domain trước khi dùng"
    PREFER over: resolve_orm_chain when you have a full domain (multiple terms).
    SKIP when: validating @api.depends — use validate_depends.

    Args:
        model: Dotted model the domain runs on, e.g. 'sale.order'.
        domain: Domain literal, e.g. "[('partner_id.country_id', '=', 'VN')]".
        odoo_version: e.g. '17.0'. 'auto' = latest indexed.
        profile_name: Optional profile filter.

    Returns:
        Tree: per-term OK / ERROR (bad field-path or invalid operator) with a
        verdict header. Logical connectors (&, |, !) are skipped.
    """
    return _validate_domain(model, domain, odoo_version, profile_name)


@mcp.tool(**READONLY_TOOL_KWARGS)
def validate_depends(
    model: str,
    method: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
) -> str:
    """Validate a compute method's @api.depends paths against the Field graph.

    Reads the indexed @api.depends('a.b', ...) arguments of the method and
    checks each dependency path resolves; flags depends on 'id' (Odoo forbids
    it) and suggests the closest field name for typos.

    TRIGGER when: "are the @api.depends on _compute_x correct", "validate depends
    of this compute method", "check compute dependencies", "depends của method
    này có field sai không", "kiểm tra @api.depends"
    PREFER over: resolve_orm_chain when checking an existing method's declared
    dependencies (not an ad-hoc path).
    SKIP when: the path is in a domain, not a depends — use validate_domain.

    Args:
        model: Dotted model name, e.g. 'sale.order'.
        method: Compute method name, e.g. '_compute_amount_total'.
        odoo_version: e.g. '17.0'. 'auto' = latest indexed.
        profile_name: Optional profile filter.

    Returns:
        Tree: per-dependency OK / ERROR (missing field, depends-on-id, typo
        suggestion). Note line when the method has no @api.depends (or era1).
    """
    return _validate_depends(model, method, odoo_version, profile_name)


@mcp.tool(**READONLY_TOOL_KWARGS)
def validate_relation(
    model: str,
    field: str,
    target_model: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
) -> str:
    """Assert a relational field points at an expected comodel.

    Checks that model.field is a many2one/one2many/many2many whose comodel is
    target_model (or a subtype of it via inheritance). Reports the actual
    comodel on mismatch and suggests the closest field name when missing.

    TRIGGER when: "does sale.order.partner_id point to res.partner", "is this
    field a many2one to res.users", "check relation target", "field X có trỏ
    đúng model Y không", "kiểm tra quan hệ field"
    PREFER over: entity_lookup(kind='field') when you specifically want to assert
    the comodel rather than read all field detail.
    SKIP when: tracing a multi-hop path — use resolve_orm_chain.

    Args:
        model: Dotted model name, e.g. 'sale.order'.
        field: Relational field name, e.g. 'partner_id'.
        target_model: Expected comodel, e.g. 'res.partner'.
        odoo_version: e.g. '17.0'. 'auto' = latest indexed.
        profile_name: Optional profile filter.

    Returns:
        Tree: OK (field -> comodel) or MISMATCH (actual vs expected) or ERROR
        (field not found / not relational), with a Next-step footer.
    """
    return _validate_relation(model, field, target_model, odoo_version, profile_name)


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
