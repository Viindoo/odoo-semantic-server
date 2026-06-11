"""Inspect MCP tool wrappers (split out of src/mcp/server.py, Phase 3).

Five discriminator / overview tools:
  - ``describe_module`` (sync, ``@offload_neo4j``) — full module architecture
    overview.  Its impl ``_describe_module`` stays in the server hub (inspect.py
    uses it too), so this wrapper reaches it through ``_srv``.
  - ``model_inspect`` / ``module_inspect`` / ``profile_inspect`` (sync,
    ``@offload_neo4j``) — method-discriminator supersets (ADR-0028).
  - ``entity_lookup`` (``async def``, no offload — pre-embeds the pattern intent
    on the loop before the to_thread hop, ADR-0046).

The discriminator impls (``_model_inspect`` / ``_module_inspect`` /
``_entity_lookup`` / ``_profile_inspect``) live in ``src/mcp/inspect.py`` and are
imported directly here (the same source server.py uses), so this module is a thin
wrapper layer over them.

Registration happens via the ``@mcp.tool`` import-time side effect; server.py
imports this module at the end of the file so the decorators run.

The wrappers still reach a handful of shared resolver/state-hub helpers
(``_describe_module`` / ``_get_api_key_id`` / ``_get_embedder`` / ``_embed_query``
/ ``_metric_nonorm_query_timeout`` and the ``EmbedOverloaded`` / ``OrmQueryTimeout``
exceptions) that remain in the hub.  They are read through the module-level
``_srv`` server reference bound at the END of this file (see the note there) via
``_srv.<name>`` attribute lookups performed at call time.  This both (a) tracks
the SAME server generation that imported this module and registered these tools
(so a ``sys.modules.pop('src.mcp.server')`` + re-import keeps a stale-generation
tool wired to its own generation, exactly as the pre-refactor bare-name globals
behaved) and (b) lets any ``monkeypatch.setattr(srv, ...)`` on those hub names be
observed.

server.py re-exports ``describe_module`` / ``model_inspect`` / ``module_inspect``
/ ``entity_lookup`` / ``profile_inspect`` (public tools) so that
``src.mcp.server.<tool>`` keeps working for tests + external callers.
"""

import asyncio
import sys

from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from src.mcp.inspect import (
    _entity_lookup,
    _model_inspect,
    _module_inspect,
    _profile_inspect,
)
from src.mcp.server import (
    READONLY_TOOL_KWARGS,
    RequiredOdooVersion,
    mcp,
    offload_neo4j,
)

# Wave 1 — @mcp.tool(**READONLY_TOOL_KWARGS) wrappers for the 7 new tools (ADR-0023 §5).
# TRIGGER docstrings keep EN + VI for router accuracy (ADR-0012 §2 exception).
# ---------------------------------------------------------------------------


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload_neo4j
def describe_module(
    name: str,
    odoo_version: RequiredOdooVersion,
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
    # WI-5 (#261/#265-Obs4): uniform raw-text output. describe_module was the last
    # tool wiring the M10.5 Wave-B dual channel (output_schema + structured_content);
    # that lone structured channel is the #261 not-found throw and the #265-Obs4
    # serialization inconsistency. Emit text only, like every sibling tool
    # (ADR-0023 §1: the plain-text tree IS the contract). The unwired
    # _describe_module_structured companion + DescribeModuleOutput DTO have been
    # removed (L9) now that no consumer references them.
    text = _srv._describe_module(name, odoo_version, profile_name)
    return ToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=None,
    )


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload_neo4j
def model_inspect(
    model: str,
    method: str,
    odoo_version: RequiredOdooVersion,
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
        method: summary | fields | methods | views | field | method | extenders.
            'field' needs field=. 'method' needs method_name=.
            'extenders' paginates the full extending-module list (use after
            summary shows "and N more" in Extended by).
        profile_name: Profile filter (inheritance-resolved). Default: all.
        field: Required for method='field'. readonly reflects Python def only.
        method_name: Required for method='method'.
        start_index: Pagination cursor for fields/methods/views/extenders.
        limit: Rows per page (cap: 50 fields, 20 methods/views/extenders,
            10 JS patches). Page via start_index, not limit.
        from_module: Filter to rows in this module (summary/fields/field).
        kind: Filter fields by ttype, e.g. 'many2one' — fields only.
        view_type: Filter views, e.g. 'form'/'tree'/'list' — views only.
            'list' is the v18+ alias for 'tree'.
    """
    text = _model_inspect(
        model=model,
        method=method,
        odoo_version=odoo_version,
        profile_name=profile_name,
        field=field,
        method_name=method_name,
        api_key_id=_srv._get_api_key_id(),
        start_index=start_index,
        limit=limit,
        from_module=from_module,
        kind=kind,
        view_type=view_type,
    )
    return ToolResult(content=[TextContent(type="text", text=text)])


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload_neo4j
def module_inspect(
    name: str,
    method: str,
    odoo_version: RequiredOdooVersion,
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
        method: One of summary | views | owl | qweb | js | dependencies.
            'fields' and 'methods' return a guidance stub (model required).
            'dependencies' returns the transitive DEPENDS_ON closure with repo
            info and topological load order (B2, ADR-0028 consolidation).
        profile_name: Optional profile filter (inheritance-resolved via
            ancestor chain). Default None = all profiles.
        start_index: Pagination cursor for views/owl/qweb/js (zero-based).
        limit: Max rows per page for views/owl/qweb/js (default 200).
        view_type: Filter views by type, e.g. 'form'/'tree'/'list' — method='views' only.
            'list' is the v18+ alias for 'tree'.
        bound_model: Filter OWL components bound to a model — method='owl' only.
        era: era1|era2|era3 — filter JS patches by era — method='js' only.
        target: filter JS patches by patched target — method='js' only.
    """
    text = _module_inspect(
        name=name,
        method=method,
        odoo_version=odoo_version,
        profile_name=profile_name,
        api_key_id=_srv._get_api_key_id(),
        start_index=start_index,
        limit=limit,
        view_type=view_type,
        bound_model=bound_model,
        era=era,
        target=target,
    )
    return ToolResult(content=[TextContent(type="text", text=text)])


@mcp.tool(**READONLY_TOOL_KWARGS)
async def entity_lookup(
    kind: str,
    *,
    odoo_version: RequiredOdooVersion,
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
        entity_lookup("field", model="sale.order", field="amount_total", odoo_version="17.0")
        → same as model_inspect(model="sale.order", method="field",
            field="amount_total", odoo_version="17.0")
    """
    # #227: entity_lookup(kind='pattern') routes to _suggest_pattern, whose
    # embedder.embed() blocks. Capture the api_key_id ContextVar on the event
    # loop (it does not propagate into a raw worker thread), then run the whole
    # dispatch off-loop so a slow embed never freezes the server.
    api_key_id = _srv._get_api_key_id()
    # Pre-embed the pattern intent on the loop through the SAME bounded path as
    # suggest_pattern (semaphore + 30s query timeout) so this discriminator
    # route can't pin an unbounded worker on the 1200s batch client. Any failure
    # leaves _query_vec=None → _suggest_pattern's sync path reports the error.
    _embedder = None
    _query_vec = None
    if kind == "pattern" and name and name.strip():
        from src.embedding.instructions import INSTRUCT_NL_TO_CODE
        try:
            _embedder = _srv._get_embedder()
            instruct = getattr(_embedder, "query_instruction", INSTRUCT_NL_TO_CODE)
            _query_vec = await _srv._embed_query(_embedder, instruct, name)
        except _srv.EmbedOverloaded as e:
            return ToolResult(content=[TextContent(type="text", text=f"entity_lookup: {e}")])
        except Exception:
            _embedder = None
            _query_vec = None
    # entity_lookup is async (it pre-embeds the pattern intent on the loop before
    # the to_thread hop), so it CANNOT be wrapped by the sync-bodied @offload_neo4j
    # (design §6 embed-aware exception). Instead we add a SCOPED `except
    # OrmQueryTimeout` around just the Neo4j dispatch: the converted resolvers
    # (_resolve_field / _resolve_method / _resolve_view / _resolve_model /
    # _describe_module) now RAISE OrmQueryTimeout on a tx-timeout, which would
    # otherwise escape this async handler as a protocol-level isError. Record the
    # metric once here + return the clean message. (Exception: kind='model' →
    # _resolve_model self-catches and returns the clean string already counted as
    # "model_inspect", so this catch never fires for model — no double-count.)
    # kind='pattern' routes to _suggest_pattern (PR-2 scope) which does not raise
    # OrmQueryTimeout, so this catch fires only for field/method/view/module.
    try:
        text = await asyncio.to_thread(
            _entity_lookup,
            kind=kind,
            odoo_version=odoo_version,
            profile_name=profile_name,
            model=model,
            field=field,
            method_name=method_name,
            xmlid=xmlid,
            name=name,
            api_key_id=api_key_id,
            from_module=from_module,
            _embedder=_embedder,
            _query_vec=_query_vec,
        )
    except _srv.OrmQueryTimeout as exc:
        _srv._metric_nonorm_query_timeout("entity_lookup")
        return ToolResult(
            content=[TextContent(type="text", text=exc.user_message)]
        )
    return ToolResult(content=[TextContent(type="text", text=text)])


# ---------------------------------------------------------------------------
# WI-4 (#260, #259 chain) — profile_inspect discriminator tool (24 -> 25)
# ADR-0028: one discriminator superset, naming matches model_inspect/module_inspect.
# ---------------------------------------------------------------------------


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload_neo4j
def profile_inspect(
    method: str,
    odoo_version: RequiredOdooVersion,
    name: str | None = None,
    repo: str | None = None,
    start_index: int = 0,
    limit: int = 50,
) -> ToolResult:
    """Method-discriminator for profile-level introspection. See ADR-0028.

    TRIGGER when: "which repos make up profile X", "list modules in Viindoo 17",
    "show profile hierarchy", "describe profile X", "danh sách module của
    profile", "profile X có bao nhiêu module", "repos nào trong profile này".
    PREFER over: chaining list_available_profiles + describe_module when the
    question is about a profile's composition (repos, modules, parent chain).
    SKIP when: you only need YES/NO on a module — use check_module_exists.
    SKIP when: you need a single model's fields/methods — use model_inspect.

    Args:
        method: 'summary' | 'repos' | 'modules'.
            summary - ancestor chain, children, repos, module count (needs name=).
            repos   - distinct repos in the ancestor chain, deduped by (url, branch).
            modules - paginated modules scoped to the profile; repo= URL-substring
              filter; start_index/limit pagination (default 50/page, max 50).
        name: Profile name (e.g. 'viindoo_internal_17'). Required for 'summary';
            optional for 'repos'/'modules' (None = all caller-visible profiles).
        repo: Filter by repo URL substring ('repos'/'modules' only).
        start_index: Zero-based pagination cursor for 'modules'.
        limit: Rows per page for 'modules' (default 50, max 50).

    Example:
        profile_inspect(method='summary', name='viindoo_internal_17',
                        odoo_version='17.0')
        -> tree: Ancestor chain, Children, Repos (deduped), Module count.
    """
    text = _profile_inspect(
        name=name,
        method=method,
        odoo_version=odoo_version,
        repo=repo,
        api_key_id=_srv._get_api_key_id(),
        start_index=start_index,
        limit=limit,
    )
    return ToolResult(content=[TextContent(type="text", text=text)])


# Bind the owning server module generation AFTER the tool functions are defined.
# sys.modules['src.mcp.server'] at THIS point is the generation that is importing
# this module (server.py imports this module from the very end of its own body,
# and that generation registered these tools onto its `mcp`). Binding at
# end-of-module — rather than via a top-level `from src.mcp import server`, which
# reads the stale `src.mcp` package attribute after a pop+reimport — makes `_srv`
# track the SAME generation as the tool objects defined above. The bodies above
# read the hub through `_srv.<name>` at call time so that
# monkeypatch.setattr(srv, ...) on hub helpers (e.g. _get_api_key_id /
# _get_embedder / _describe_module) is observed, and so a test holding a stale
# top-level `srv` binding (after a pop+reimport) calls the stale-gen tool whose
# `_srv` points back at that same stale generation — exactly as it was
# pre-refactor when these bodies used bare-name globals in server.py.
_srv = sys.modules["src.mcp.server"]
