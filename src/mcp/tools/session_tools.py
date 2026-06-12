"""Session-context MCP tools (split out of src/mcp/server.py, Phase 3).

Wave E session tools (ADR-0029, M11 WI-E3):
  - ``set_active_version`` / ``set_active_profile`` (sync, ``@offload``) — pin
    the per-session version / profile.
  - ``list_available_versions`` (sync, ``@offload_neo4j`` — per-query bounded +
    clean-string-on-timeout, #287) / ``list_available_profiles`` (sync,
    ``@offload``, Postgres-only) — enumerate what is indexed / registered.

The session persistence helpers (``normalize_version_arg`` /
``set_active_version_db`` / ``set_active_profile_db``) live in
``src/mcp/session.py`` and are imported directly here as ``_session`` (the same
source server.py uses), so this module is a thin wrapper layer over them.

Registration happens via the ``@mcp.tool`` import-time side effect; server.py
imports this module at the end of the file so the decorators run.

The wrappers reach the shared resolver/state-hub helpers (``_get_driver`` /
``_scope`` / ``_scope_pred`` / ``_checkout_pg`` / ``_get_api_key_id`` /
``_get_mcp_session_id`` / ``_http_request_has_api_key`` / ``_get_allowed_profiles``
/ ``_effective_allowed`` / ``logger``) through the module-level ``_srv`` server
reference bound at the END of this file (see the note there) via ``_srv.<name>``
attribute lookups performed at call time.  This both (a) tracks the SAME server
generation that imported this module and registered these tools (so a
``sys.modules.pop('src.mcp.server')`` + re-import keeps a stale-generation tool
wired to its own generation, exactly as the pre-refactor bare-name globals
behaved) and (b) lets any ``monkeypatch.setattr(srv, ...)`` on those hub names be
observed.

server.py re-exports ``set_active_version`` / ``set_active_profile`` /
``list_available_versions`` / ``list_available_profiles`` (public tools) so that
``src.mcp.server.<tool>`` keeps working for tests + external callers.
"""

import sys

from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from src.mcp import session as _session
from src.mcp.orm import OrmQueryTimeout
from src.mcp.server import (
    MUTATING_TOOL_KWARGS,
    READONLY_TOOL_KWARGS,
    mcp,
    offload,
    offload_neo4j,
)

# ---------------------------------------------------------------------------
# Wave E — Session-context tools (ADR-0029, M11 WI-E3)
# ---------------------------------------------------------------------------


@mcp.tool(**MUTATING_TOOL_KWARGS)
@offload
def set_active_version(odoo_version: str) -> ToolResult | str:
    """Pin the active Odoo version for this MCP session (ADR-0029 implicit context).

    TRIGGER when: a single actor works one Odoo version for a while and wants the
    'auto' convenience of dropping odoo_version= on subsequent calls.
    PREFER over: passing odoo_version='17.0' to every individual tool call ONLY
    when one actor drives this session; this scopes the version once per MCP
    session with a 24h write-anchored idle TTL.
    SKIP when: hopping between multiple versions mid-session, OR when concurrent
    sub-agents / parallel sessions may share this MCP session — pass
    odoo_version= explicitly to each call instead. The pin is single-actor
    convenience and last-write-wins; concurrent actors sharing one session MUST
    pass an explicit odoo_version= so each resolves its own version safely.

    Args:
        odoo_version: Concrete version string to pin, e.g. '17.0', '16.0'.
            Sentinel values ('auto', 'default', 'latest', 'any', '') are
            rejected here — you cannot pin a sentinel as the active version.
            After a successful pin, a single-actor session may pass 'auto' as
            odoo_version to reuse this pin (ADR-0029); under concurrency pass the
            version explicitly instead.

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
    # #287 (GAP-2): the two sanity reads are now per-query bounded via
    # _data_bounded; the catch-all `except Exception` below would otherwise
    # swallow a tx-timeout silently (no metric, indistinguishable from a real
    # config error). The `except OrmQueryTimeout` is placed BEFORE it so a
    # timeout records the metric once and returns the clean ADR-0023 string.
    try:
        with _srv._get_driver().session() as neo4j_session:
            hit = _srv._data_bounded(
                neo4j_session,
                f"MATCH (m:Module {{odoo_version: $v}}) WHERE {_srv._scope_pred('m')} "
                "RETURN m LIMIT 1",
                label=f"version {normalized!r} sanity check",
                v=normalized, **_srv._scope(),
            )
        if not hit:
            # Version not indexed (for this tenant) — fetch available list for the error.
            with _srv._get_driver().session() as neo4j_session:
                rows = _srv._data_bounded(
                    neo4j_session,
                    f"""
                    MATCH (m:Module)
                    WHERE {_srv._scope_pred("m")}
                    WITH DISTINCT m.odoo_version AS v
                    WHERE v <> 'unknown' AND v =~ '\\d+\\.\\d+'
                    RETURN v
                    ORDER BY toInteger(split(v, '.')[0]) DESC,
                             toInteger(split(v, '.')[1]) DESC
                    """,
                    label="indexed-version list",
                    **_srv._scope(),
                )
            available = [r["v"] for r in rows]
            if available:
                avail_str = ", ".join(available)
                hint = f"Indexed versions: {avail_str}"
            else:
                hint = "No Odoo versions are indexed in this knowledge base yet."
            return ToolResult(content=[TextContent(type="text",
                text=(
                    f"Error: version '{normalized}' is not indexed in this knowledge base.\n"
                    f"├─ {hint}\n"
                    "└─ Use list_available_versions() to see what is available."
                )
            )])
    except OrmQueryTimeout as exc:
        return _srv._nonorm_timeout_response(exc, "set_active_version")
    except Exception:
        _srv.logger.warning("set_active_version: indexed-version check failed", exc_info=True)
        return ToolResult(content=[TextContent(type="text",
            text="Error checking indexed versions — try again shortly.")])
    try:
        persisted = _session.set_active_version_db(
            _srv._get_api_key_id(), normalized, _srv._get_mcp_session_id()
        )
    except Exception:
        _srv.logger.warning("set_active_version: persist failed", exc_info=True)
        return ToolResult(content=[TextContent(type="text",
            text="Error persisting the active version — try again shortly.")])
    if not persisted:
        # The write was skipped (non-numeric api_key_id). On authenticated HTTP
        # this means the api_key_id never reached the tool body (#248) — fail
        # loud instead of lying with a success receipt. On stdio / CLI (no
        # X-API-Key header) it is the expected no-op.
        if _srv._http_request_has_api_key():
            return ToolResult(content=[TextContent(type="text",
                text=(
                    "Error: could not persist the active version for this API key "
                    "(session context unavailable).\n"
                    "└─ Pass an explicit odoo_version= on each call until this is resolved."
                )
            )])
        return ToolResult(content=[TextContent(type="text",
            text=(
                f"Note: '{normalized}' was not persisted — no API key in this "
                "transport (stdio/CLI).\n"
                "Pass odoo_version= explicitly; session pinning needs an HTTP API key."
            )
        )])
    return ToolResult(content=[TextContent(type="text",
        text=(
            f"Active version set to '{normalized}' for this session (TTL 24h).\n"
            "Pass odoo_version='auto' on subsequent calls to reuse this version "
            "(the version-required tools no longer accept an omitted odoo_version; "
            "'auto' resolves to this pin)."
        )
    )])


@mcp.tool(**MUTATING_TOOL_KWARGS)
@offload
def set_active_profile(profile_name: str | None) -> ToolResult:
    """Pin the active profile for this MCP session (ADR-0029 implicit context).

    TRIGGER when: a single actor works exclusively within one customer profile
    and wants profile filtering applied automatically to subsequent queries.
    PREFER over: passing profile_name='my-erp-prod' to every tool call ONLY when
    one actor drives this session; this scopes the profile once per MCP session
    with a 24h write-anchored idle TTL.
    SKIP when: comparing across multiple profiles mid-session, OR when concurrent
    sub-agents / parallel sessions may share this MCP session — pass
    profile_name= explicitly to each call instead. The pin is single-actor
    convenience: concurrent same-session subagents that need DISTINCT profiles
    must pass profile_name= explicitly per call, because the pin is shared per
    (api_key_id, mcp_session_id) and is last-write-wins — one subagent's
    set_active_profile clobbers another's. (Authz-safe regardless: the pinned
    profile is re-validated at read time through the ADR-0034 tenant choke,
    narrowing-only and fail-closed, so a clobber can only narrow a view, never
    widen or leak — but it can silently return a narrower-than-intended result,
    hence pass profile_name= explicitly when subagents diverge. See #279.)

    Args:
        profile_name: Profile name such as 'internal_17' or
            'my-erp-prod'. Pass null / None to clear the active profile
            (subsequent calls revert to cross-profile queries).

    Returns:
        Confirmation receipt with the pinned profile name and TTL duration.
    """
    # Validate the profile exists before pinning it (None = clear, always valid).
    if profile_name is not None:
        # SECURITY (ADR-0034): authorization gate BEFORE the existence check.
        # _get_allowed_profiles() returns None for an admin / unrestricted key,
        # or the list of profiles this API key's tenant may see. Pinning a
        # profile outside that set would let a scoped key narrow onto — and read
        # from — a profile it is not entitled to, so reject it here.
        # _get_allowed_profiles() touches the DB (choke-point query), so wrap it:
        # a DB error here must surface as a structured ToolResult error, not a
        # raw trace. Authz semantics unchanged (None=admin→allow; else reject if
        # profile_name not in allowed).
        try:
            allowed = _srv._get_allowed_profiles()
        except Exception:
            _srv.logger.warning("set_active_profile: authorization check failed", exc_info=True)
            return ToolResult(content=[TextContent(type="text",
                text="Error checking profile authorization — try again shortly.")])
        if allowed is not None and profile_name not in allowed:
            return ToolResult(content=[TextContent(type="text",
                text=(
                    f"Error: profile '{profile_name}' is not available to this "
                    "API key.\n"
                    "└─ Use list_available_profiles() to see what you can pin."
                )
            )])
        try:
            with _srv._checkout_pg() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM profiles WHERE name=%s", (profile_name,))
                    found = cur.fetchone()
        except Exception:
            _srv.logger.warning("set_active_profile: profile existence check failed", exc_info=True)
            return ToolResult(content=[TextContent(type="text",
                text="Error checking profiles — try again shortly.")])
        if not found:
            # Profile not registered — list available ones for the error message.
            try:
                with _srv._checkout_pg() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT name FROM profiles ORDER BY name")
                        rows = cur.fetchall()
                available = [r[0] for r in rows]
            except Exception:
                available = []
            # INFO-LEAK guard (ADR-0034): a scoped (non-admin) key must not see
            # profile names outside its tenant in the error hint. `allowed` is the
            # authorized set computed above (None = admin / unrestricted → show
            # all). Filter the listing to names this key may already see.
            if allowed is not None:
                available = [name for name in available if name in allowed]
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
        persisted = _session.set_active_profile_db(
            _srv._get_api_key_id(), profile_name, _srv._get_mcp_session_id()
        )
    except Exception:
        _srv.logger.warning("set_active_profile: persist failed", exc_info=True)
        return ToolResult(content=[TextContent(type="text",
            text="Error persisting the active profile — try again shortly.")])
    if not persisted:
        # Skipped write (non-numeric api_key_id). Loud on authenticated HTTP
        # (#248 propagation gap), gentle no-op on stdio / CLI.
        if _srv._http_request_has_api_key():
            return ToolResult(content=[TextContent(type="text",
                text=(
                    "Error: could not persist the active profile for this API key "
                    "(session context unavailable).\n"
                    "└─ Pass an explicit profile_name= on each call until this is resolved."
                )
            )])
        return ToolResult(content=[TextContent(type="text",
            text=(
                "Note: active profile was not persisted — no API key in this "
                "transport (stdio/CLI).\n"
                "Pass profile_name= explicitly; session pinning needs an HTTP API key."
            )
        )])
    if profile_name is None:
        msg = (
            "Active profile cleared for this session.\n"
            "Subsequent tool calls will query across all profiles you can access."
        )
    else:
        msg = (
            f"Active profile set to '{profile_name}' for this session (TTL 24h).\n"
            "Subsequent query tools that omit profile_name= will narrow to this "
            "profile for this session (still bounded by your tenant scope)."
        )
    return ToolResult(content=[TextContent(type="text", text=msg)])


def _list_available_versions() -> ToolResult:
    """Impl for list_available_versions — no FastMCP wrapper overhead.

    Extracted from the public def (#287) so the bounded Neo4j read can be tested
    in the no-DB timeout lane exactly like the other PURE bodies, and so the
    @offload_neo4j backstop wraps a plain sync body.
    """
    with _srv._get_driver().session() as neo4j_session:
        rows = _srv._data_bounded(
            neo4j_session,
            """
            MATCH (m:Module)
            WHERE ($own IS NULL OR (size(m.profile) > 0
                   AND all(__p IN m.profile WHERE __p IN $own OR __p IN $shared)))
            WITH DISTINCT m.odoo_version AS v
            WHERE v <> 'unknown' AND v =~ '\\d+\\.\\d+'
            RETURN v
            ORDER BY toInteger(split(v, '.')[0]) DESC,
                     toInteger(split(v, '.')[1]) DESC
            """,
            label="indexed Odoo version list",
            **_srv._scope(None),
        )

    if not rows:
        return ToolResult(content=[TextContent(type="text",
            text=(
                "No Odoo versions indexed in this profile yet. "
                "Call list_available_profiles to see which profiles are configured."
            )
        )])

    versions = [r["v"] for r in rows]
    lines = [f"Indexed Odoo versions ({len(versions)} total):"]
    for i, v in enumerate(versions):
        prefix = "└─" if i == len(versions) - 1 else "├─"
        lines.append(f"{prefix} {v}")
    return ToolResult(content=[TextContent(type="text", text="\n".join(lines))])


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload_neo4j
def list_available_versions() -> ToolResult | str:
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
    return _list_available_versions()


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload
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
    # C4 (WI-4): scope the listing to the tenant's allowed profiles. admin
    # (allowed=None) sees all; a tenant sees only its own + shared-base profiles;
    # [] (profile-less tenant) → empty list (deny-all).
    allowed = _srv._effective_allowed(None)
    if allowed is None:
        sql = "SELECT name, odoo_version FROM profiles ORDER BY name"
        params: list = []
    else:
        sql = "SELECT name, odoo_version FROM profiles WHERE name = ANY(%s) ORDER BY name"
        params = [allowed]
    try:
        with _srv._checkout_pg() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
    except Exception:
        _srv.logger.warning("list_available_profiles: query failed", exc_info=True)
        return ToolResult(content=[TextContent(type="text",
            text="Error querying profiles — try again shortly.")])

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


# Bind the owning server module generation AFTER the tool functions are defined.
# sys.modules['src.mcp.server'] at THIS point is the generation that is importing
# this module (server.py imports this module from the very end of its own body,
# and that generation registered these tools onto its `mcp`). Binding at
# end-of-module — rather than via a top-level `from src.mcp import server`, which
# reads the stale `src.mcp` package attribute after a pop+reimport — makes `_srv`
# track the SAME generation as the tool objects defined above. The bodies above
# read the hub through `_srv.<name>` at call time so that
# monkeypatch.setattr(srv, ...) on hub helpers (e.g. _get_driver / _scope /
# _checkout_pg / _effective_allowed) is observed, and so a test holding a stale
# top-level `srv` binding (after a pop+reimport) calls the stale-gen tool whose
# `_srv` points back at that same stale generation — exactly as it was
# pre-refactor when these bodies used bare-name globals in server.py.
_srv = sys.modules["src.mcp.server"]
