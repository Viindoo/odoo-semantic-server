# SPDX-License-Identifier: AGPL-3.0-or-later
# src/mcp/server.py
import asyncio
import functools
import logging
import math
import os
import re
import sys
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Annotated

from fastmcp import FastMCP
from neo4j import GraphDatabase
from neo4j.exceptions import ClientError
from pydantic import Field
from starlette.requests import Request

from src.constants import (
    DEFAULT_EMBEDDER_MODEL,
    EMBEDDER_MAX_CONCURRENCY,
    EMBEDDER_SLOT_ACQUIRE_TIMEOUT,
    EMBEDDER_TOKEN_BUDGET,
    HNSW_ITERATIVE_SCAN,
    LIST_PREVIEW_MAX_ITEMS,
    MAGIC_FIELDS,
    NEO4J_QUERY_TIMEOUT_SECONDS,
    NONORM_READ_MAX_CONCURRENCY,
    NONORM_SLOT_ACQUIRE_TIMEOUT,
    ORM_QUERY_MAX_CONCURRENCY,
    ORM_SLOT_ACQUIRE_TIMEOUT,
    PG_POOL_MAX_CONN,
    PG_POOL_MIN_CONN,
    REL_DEPENDS_ON,
    REL_INHERITS,
    REL_INHERITS_VIEW,
    TIMEOUT_EMBEDDER_READ_QUERY,
)
from src.mcp import session as _session
from src.mcp.hints import (  # noqa: F401  (hints_for is re-exported for external consumers)
    format_next_step,
    hints_for,
)
from src.mcp.inspect import (
    # The inspect-tool wrappers moved to tools/inspect_tools.py (Phase 3) and
    # import the discriminator impls (_model_inspect / _module_inspect /
    # _entity_lookup / _profile_inspect) directly from src.mcp.inspect. Only
    # _module_inspect needs re-exporting here: test_cross_tenant_isolation.py
    # imports it via src.mcp.server. `X as X` marks the intentional re-export so
    # ruff keeps F401 active for genuinely-unused imports elsewhere.
    _module_inspect as _module_inspect,
)
from src.mcp.orm import (
    OrmQueryTimeout,
    _bounded,
    _edition_rank_cypher,
    _is_tx_timeout,
)
from src.mcp.orm_queries import (
    _count_fields_with_inherited,
    _count_methods_with_inherited,
    _resolve_field_inherited,
    _resolve_method_inherited,
)
from src.mcp.orm_validators import (
    # ORM-validation impls: the tool wrappers moved to tools/orm_tools.py
    # (Phase 1) but tests still import these via src.mcp.server. `X as X` marks
    # an intentional re-export so ruff keeps F401 active for the genuinely
    # internal names above (instead of a blanket per-block noqa).
    _resolve_orm_chain as _resolve_orm_chain,
)
from src.mcp.orm_validators import (
    _validate_depends as _validate_depends,
)
from src.mcp.orm_validators import (
    _validate_domain as _validate_domain,
)
from src.mcp.orm_validators import (
    _validate_relation as _validate_relation,
)
from src.mcp.resources import register_resources
from src.mcp.tool_log_middleware import UsageLogMiddleware as _UsageLogMiddleware
from src.mcp.tree_builder import render_list_block

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Facade-split patterns - DO NOT "unify for consistency" (each is FUNCTIONAL).
# ---------------------------------------------------------------------------
# The god-file split (server / orm / writer_neo4j / parser_python / pipeline /
# auth_registry) left several DIFFERENT child<->parent wiring mechanisms in the
# tree. They look inconsistent but each is load-bearing for a specific reason;
# collapsing them to one style reintroduces an import cycle or a broken
# monkeypatch. The variants and WHY they differ:
#
#  1. `_srv = sys.modules["src.mcp.server"]` (subscript) - used by the tool-mods
#     (e.g. tools/orm_tools.py, tools/discovery.py). Safe because each tool-mod
#     ALSO top-imports `src.mcp.server`, so the key is guaranteed present.
#  2. `_srv = sys.modules.get("src.mcp.server")` (.get) - used by describe.py /
#     listings.py, which do NOT top-import the server. `.get` binds None on a
#     cold import instead of raising KeyError (see test_facade_cold_import.py).
#  3. Deferred function-local `from src.mcp import server` (inspect.py /
#     orm_validators.py) and `from <parent> import X` inside a function
#     (parser_python_era1.py, writer_neo4j_{orm,ui,spec}.py, pipeline_{repo,
#     reembed}.py) - breaks a parent<->child module-load cycle AND/OR keeps the
#     test monkeypatch contract (the binding is resolved at call time on the
#     parent namespace, so `patch("src.indexer.pipeline.<name>")` is seen).
# (The orm.py `_rebind` facade re-export loop was removed in the Phase 7.5
# codemod: callers now import the query/validator helpers DIRECTLY from
# src.mcp.orm_queries / src.mcp.orm_validators, so orm.py no longer mirrors
# those names onto its own namespace. The bottom-layer it DEFINES is still
# imported by the children via `from .orm import ...`.)
#
# Net: `[...]` vs `.get()` is decided by "does the child top-import server?";
# symbol-import vs module-attr-access is decided by "is the binding a
# monkeypatch target?". Both are documented at each call site. See the three
# consolidation review reports and tests/test_facade_cold_import.py.
# ---------------------------------------------------------------------------

# Sentinel api_key_id for direct _impl calls (tests, CLI) - refs are scoped
# to this namespace and do not collide with production tenant refs.
_ANONYMOUS_API_KEY_ID = "anonymous"


# Render-only edition label - WG-5 T1.
# Maps raw license string → human-readable label for MCP output.
# License facts (Odoo S.A., https://www.odoo.com/documentation/19.0/legal/licenses.html):
#   OEEL-1 = Odoo Enterprise Edition License - Odoo S.A.'s OWN Enterprise add-ons.
#   OPL-1  = Odoo Proprietary License - Odoo S.A.'s license for THIRD-PARTY / proprietary
#            Odoo apps; Viindoo's tvtmaaddons are published under OPL-1. OPL-1 is NOT
#            Odoo Enterprise.
# OPL-1 is intentionally NOT mapped here so it falls through to the indexed `edition`
# enum (e.g. "viindoo" → "Viindoo Enterprise (EE)"); mapping it to "Odoo Enterprise (EE)"
# mislabeled third-party OPL-1 addons authored by Viindoo (#263, regression from PR #165).
_LICENSE_TO_EDITION_LABEL: dict[str, str] = {
    "lgpl-3":   "Community (CE)",
    "lgpl-3.0": "Community (CE)",
    "agpl-3":   "Community (CE)",
    "agpl-3.0": "Community (CE)",
    "gpl-3":    "Community (CE)",
    "gpl-3.0":  "Community (CE)",
    "oeel-1":   "Odoo Enterprise (EE)",
}
_EDITION_ENUM_TO_LABEL: dict[str, str] = {
    "community":  "Community (CE)",
    "enterprise": "Odoo Enterprise (EE)",
    "viindoo":    "Viindoo Enterprise (EE)",
    "oca":        "OCA / Community-compatible",
    "custom":     "Custom",
}


# Edition enums that are DEFINITIVE first-party signals: when the indexer has
# stamped one of these, it identifies the author/edition more authoritatively
# than any license string, so it must NOT be overridden by a license mapping
# (#263 / N3). OPL-1 is Odoo S.A.'s third-party proprietary license, under which
# Viindoo publishes its addons (it is NOT Odoo Enterprise, and OEEL-1 is Odoo
# S.A.'s own Enterprise license - not a Viindoo license). A first-party Viindoo
# module (`edition='viindoo'`) must read "Viindoo Enterprise (EE)" - never
# "Odoo Enterprise (EE)" - even on the defensive edge where its license string
# would otherwise map elsewhere.
_FIRST_PARTY_EDITIONS: frozenset[str] = frozenset({"viindoo"})


def _edition_label(edition: str | None, license: str | None = None) -> str:
    """Return a human-readable edition label for MCP output.

    Resolution order:
      1. A DEFINITIVE first-party ``edition`` enum (``_FIRST_PARTY_EDITIONS``)
         wins outright - license can never override a known first-party author
         (#263 / N3: even if a module's license string would otherwise map to
         Odoo Enterprise, a ``viindoo`` edition still reads "Viindoo Enterprise").
      2. Otherwise ``license`` (SPDX string) - more specific than a generic
         ``edition`` enum (e.g. disambiguates raw ``'enterprise'`` via OEEL-1).
      3. Otherwise the ``edition`` enum mapping, then the raw value, then CE.

    Used by check_module_exists, describe_module, and model_inspect summary
    to show 'Community (CE)' / 'Odoo Enterprise (EE)' / 'Viindoo Enterprise (EE)'
    instead of raw 'community'/'enterprise'/'viindoo'.
    """
    if edition and edition in _FIRST_PARTY_EDITIONS:
        return _EDITION_ENUM_TO_LABEL.get(edition, edition)
    if license:
        label = _LICENSE_TO_EDITION_LABEL.get(license.lower().strip())
        if label:
            return label
    if edition:
        return _EDITION_ENUM_TO_LABEL.get(edition, edition)
    return "Community (CE)"


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

    `total` defaults to len(items) - pass explicitly when caller has already
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


def _portable_path(
    file_path: str | None,
    *,
    repo: str | None = None,
    module: str | None = None,
) -> str:
    """Strip a server-absolute prefix so tool output is portable (ADR-0037).

    AI clients run on a different machine than the indexer; an absolute server
    path (``/home/tuan/git/odoo_17.0/addons/sale/...``) is useless to them.
    This returns the repo-relative tail (``addons/sale/...``) they can map onto
    their own checkout.

    Strategy (first match wins):
      1. Already relative (no leading "/") → returned unchanged.  Idempotent, so
         it is a safe no-op on reindexed data that already stores relative paths
         (this function is the read-side safety-net for legacy absolute rows).
      2. ``/{repo}/`` segment → cut *through* it → repo-root-relative
         (``addons/sale/models/x.py``), matching the relative form the indexer
         now stores. The ``[repo]`` label already names the repo, so the dir
         name is dropped from the path to avoid redundancy.
      3. ``/{module}/`` segment (when repo dirname unavailable, e.g. stylesheet
         tools) → cut just *before* it so the module dir is kept
         (``css_mod/static/...``) - a close approximation of repo-relative.
      4. No anchor (e.g. CoreSymbol) → first ``/odoo/`` or ``/openerp/`` package
         segment kept (Odoo core source layout → ``odoo/orm/models.py``).
      5. Last resort → strip the leading "/" so no absolute path ever leaks.
    """
    if not file_path:
        return file_path or ""
    if not file_path.startswith("/"):
        return file_path
    if repo:
        marker = f"/{repo}/"
        # rfind (LAST occurrence): the repo root is the deepest "/{repo}/"
        # segment before the in-repo tail.  A checkout whose parent dirs repeat
        # the repo name (e.g. /srv/odoo/repos/odoo/addons/sale/x.py, repo=odoo)
        # would strip at the wrong segment with find() (FIRST occurrence).
        idx = file_path.rfind(marker)
        if idx != -1:
            # Cut through the repo dir → repo-root-relative (matches write side).
            return file_path[idx + len(marker):]
    if module:
        marker = f"/{module}/"
        idx = file_path.find(marker)
        if idx != -1:
            # Keep the module dir segment (cut at its leading "/").
            return file_path[idx + 1:]
    for core_seg in ("/odoo/", "/openerp/"):
        idx = file_path.find(core_seg)
        if idx != -1:
            return file_path[idx + 1:]
    return file_path.lstrip("/")


_REPO_URL_CACHE: dict[int, str | None] = {}


def _repo_url_for_id(repo_id: int | None) -> str | None:
    """Resolve a repo's portable git URL from its id (ADR-0037, cached).

    The ``[repo]`` label must show a *semantic* identity an AI client can map
    to its own checkout - the git URL (``github.com/odoo/odoo``) - never the
    server checkout directory name (``odoo_17.0``), which is host-specific
    detail the client neither knows nor needs.

    Returns None when repo_id is None, the repo is unknown, or the repo has no
    URL (locally-registered repos) - callers then fall back to the dirname.
    Successful lookups (incl. a genuine NULL url) are memoised; transient DB
    failures are not cached so a later call can retry.  Note: the cache has no
    TTL/invalidation - a url set AFTER the first lookup serves the stale value
    (or dirname fallback) until process restart.  Acceptable: display-only, and
    a restart clears it (same restart that ADR-0037 D1 prescribes after a
    local_path re-point).
    """
    if repo_id is None:
        return None
    if repo_id in _REPO_URL_CACHE:
        return _REPO_URL_CACHE[repo_id]
    try:
        from src.db.pg import repo_store
        row = repo_store().get_repo_by_id(repo_id)
    except Exception:
        return None
    url = (row or {}).get("url")
    _REPO_URL_CACHE[repo_id] = url
    return url


# Server-level instructions surfaced to MCP clients (Claude Code, Cursor, VS
# Code, Codex, Gemini) at initialize. Gives this server a UNIQUE positive
# identity + a clear precedence so an AI agent never routes an Odoo-source
# question to the wrong place. Two failure modes this prevents, both silent
# (every alternative returns a plausible-but-wrong answer, so there is no error
# to self-correct on):
#   1. Confusing this STATIC index with a LIVE-instance Odoo MCP.
#   2. Skipping this index and reading the (huge) Odoo codebase directly, which
#      burns context and degrades answers - OSM exists precisely to avoid that.
# Everything is described by CAPABILITY, never by a specific product name, so it
# stays correct as the tool ecosystem changes. Per-tool reinforcement is NOT
# duplicated into each docstring (FastMCP enforces a ~1500-char description
# budget - see tests/test_mcp_tool_descriptions.py); the look-live tool list
# below is the single carrier.
INSTRUCTIONS = """\
Odoo Semantic ("odoo-semantic") is the INDEXED Odoo source-code knowledge
graph: a pre-built graph + vector index of Odoo source spanning every indexed
Odoo version (legacy through latest) and many repos/editions, with inheritance,
override, and cross-module impact ALREADY RESOLVED. It gives AUTHORITATIVE
STRUCTURAL facts about how Odoo source IS DEFINED - module composition,
inheritance chains, field/method definitions by spec, view XPath, version
diffs, deprecations, lint rules, override points, ORM-path validity, patterns.
Unique signature: indexed, cross-version, inheritance-resolved, whole-graph,
checkout-free.

This is your PRIMARY, context-efficient source for Odoo source/structure
questions. The Odoo codebase is huge; reading it directly burns context and
degrades answers - prefer THIS server first. Order of precedence:
  1. odoo-semantic available -> use it.
  2. available but it lacks the specific detail -> THEN read the source
     (Read/Grep the addons checkout) to fill that gap.
  3. unavailable -> read the source.
Reading source code is the FALLBACK, never the first move when this server can
answer.

This is a STATIC index - it has NO runtime/live data and never connects to a
running instance. For LIVE DATA (actual record values, search/read/write real
records, executing a method, this instance's installed modules) use a live Odoo
MCP server (one exposing read_record / search_records / execute_method), NOT
this server.

Tools whose names look live but are STATIC here (return indexed source, never
runtime data): model_inspect, module_inspect, entity_lookup, describe_module,
check_module_exists, validate_domain, validate_depends, validate_relation,
resolve_orm_chain. If you need live records, this is the wrong server.
"""

# on_duplicate="replace": the tool-wrapper modules at the end of this file are
# deliberately re-imported (sys.modules.pop + re-import) so their @mcp.tool
# decorators re-run against the CURRENT `mcp` after any reload - that is how the
# full tool surface survives importlib.reload / sys.modules.pop in ~15 tests
# (see the "Tool wrapper modules" block + ADR/#H3). When the entry point is a
# tool module itself, that module's body also executes a second time, so each
# tool registers twice on the same `mcp`. fastmcp v2 replaced silently;
# v3's default (on_duplicate=None, resolves to "warn") logs "Component already exists" on every
# re-register. "replace" restores the v2 silent-override semantics this
# re-import architecture relies on - same final tool count, no log noise. It is
# NOT a log suppression: it declares the duplicate policy the design requires.
mcp = FastMCP("odoo-semantic", instructions=INSTRUCTIONS, on_duplicate="replace")
# Register 7 MCP resources (odoo:// URIs) - Pattern 8, Wave F.
register_resources(mcp)
# Register FastMCP-layer usage logging middleware so that on_call_tool has
# access to context.message.name (the real tool name) - see F5 fix in
# src/mcp/tool_log_middleware.py.
mcp.add_middleware(_UsageLogMiddleware())

# All read-only OSM tools query a statically-indexed graph.
# Annotations advertise this to MCP clients (Claude Code, Cursor, VS Code,
# ChatGPT) so they can auto-approve and skip confirmation gates.
# (cross-server pattern: read-only annotations for auto-approval)
READONLY_TOOL_KWARGS = {
    # Suppress FastMCP auto-wrap: without this, -> str tools get wrapped as
    # {"result": "<tree>"} in structuredContent, giving two output shapes for
    # one grammar (ADR-0023 §1 + WI-5 fix for #261/#265-Obs4).
    # -> ToolResult tools are unaffected: FastMCP returns ToolResult as-is
    # before reaching the output_schema branch (fastmcp/tools/tool.py:385-386).
    "output_schema": None,
    "annotations": {
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
        "destructiveHint": False,
    }
}

# Session-mutating tools (set_active_version, set_active_profile) - write the
# per-(api_key, mcp_session) in-memory pin store but are idempotent and
# non-destructive.  readOnlyHint=False because they mutate session state
# (the in-memory pin - no DB write since #251).
MUTATING_TOOL_KWARGS = {
    "annotations": {
        "readOnlyHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
        "destructiveHint": False,
    }
}

# WI-4 (ADR-0029 amend): ``odoo_version`` is HARD-REQUIRED on every
# version-bearing MCP tool.  In a long session an LLM tends to drop the version
# argument; with a sentinel default ("auto") that silently fell through to the
# latest-indexed version (resolver Tier-3) and could return data for the WRONG
# Odoo version without any signal.  Marking the parameter required means FastMCP
# rejects the call with a "Missing required argument" ValidationError *before*
# the handler runs, forcing the model to retry with an explicit version.
#
# Implemented as ``Annotated[str, Field(...)]`` with NO default:
#   * FastMCP renders ``odoo_version`` into the JSON-Schema ``required`` array.
#   * It is syntactically a non-default parameter, so it must precede any
#     defaulted positional params (or be keyword-only, e.g. cli_help/entity_lookup).
# The 4 session/bootstrap tools (set_active_version, list_available_versions,
# set_active_profile, list_available_profiles) intentionally do NOT use this -
# they are how a client discovers/sets the version in the first place.
# MCP Resources (odoo://{version}/...) keep sentinel support: the version is
# always present in the URI path, so the silent-omission failure mode cannot
# occur there.
RequiredOdooVersion = Annotated[
    str,
    Field(
        description=(
            "REQUIRED - pass the concrete Odoo version explicitly on every call "
            "(e.g. '17.0'). Passing it per call is the correct, race-free choice "
            "under concurrency: parallel sessions or concurrent sub-agents that "
            "share one MCP session each get the version they pass, regardless of "
            "any session pin. 'auto' is a single-actor convenience that reuses "
            "the pin set by set_active_version; it is NOT safe when multiple "
            "actors share a session (the pin is last-write-wins) - pass the "
            "version explicitly instead. Use list_available_versions if unsure "
            "which versions are indexed."
        ),
    ),
]

_driver = None
_embedder_instance = None
_version_checked = False
_init_lock = threading.Lock()  # guards _driver + _embedder_instance lazy init


def _resolve_orm_int(name: str, default: int) -> int:
    """Post-dotenv int knob: env wins over the (possibly stale) import constant."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _resolve_orm_float(name: str, default: float) -> float:
    """Post-dotenv float knob: env wins over the (possibly stale) import constant."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class _LazyBoundedSemaphore:
    """Lazily-built ``threading.BoundedSemaphore`` for one concurrency pool (#279).

    One instance per pool (embed / orm / nonorm). The single source of the
    double-check-locked lazy-build logic the three pools used to duplicate as 12
    module globals + 3 near-identical factory functions (#273/#275/#276). A
    *threading* (NOT asyncio) BoundedSemaphore so acquire()/release() can run
    INSIDE the worker thread - the slot's lifetime is tied to the THREAD, never
    the (possibly cancelled) caller coroutine. That is the #276 / CRITICAL-2
    cancel-safety invariant: a client disconnect cancels the wrapper coroutine
    but the worker thread keeps the slot until its own ``finally`` releases it.

    Built once on first ``.get()`` - lazily and post-dotenv, so a ``.env``-only
    cap is honoured (``config.init_dotenv()`` runs in __main__ AFTER this module
    imports; the import-time constant can be stale - ADR-0031). ``cap_in_use`` /
    ``timeout_in_use`` are cached so the hot path logs exactly the value the
    semaphore was sized for (#276 G6/G7). ``importlib.reload(server)`` re-runs the
    module body → a fresh instance with ``_sem=None`` → the next ``.get()``
    rebuilds from the freshly-set env, keeping test reconfiguration honest (the
    15 safety-net tests reload the module then call the thin wrappers directly).

    BoundedSemaphore (not plain) turns any over-release into an immediate
    ValueError instead of silent permit inflation (HIGH #4).
    """

    def __init__(
        self,
        cap_env: str,
        default_cap: int,
        timeout_env: str,
        default_timeout: float,
    ):
        self._cap_env = cap_env
        self._default_cap = default_cap
        self._timeout_env = timeout_env
        self._default_timeout = default_timeout
        self._lock = threading.Lock()
        self._sem: threading.BoundedSemaphore | None = None
        # Cached so the hot path reads exactly the cap/timeout the live semaphore
        # was sized with (post-dotenv). None until the first .get() builds it.
        self.cap_in_use: int | None = None
        self.timeout_in_use: float | None = None

    def get(self) -> "threading.BoundedSemaphore":
        """Return the BoundedSemaphore, building it once (double-check-locked)."""
        if self._sem is None:
            with self._lock:
                if self._sem is None:
                    self.cap_in_use = _resolve_orm_int(
                        self._cap_env, self._default_cap
                    )
                    self.timeout_in_use = _resolve_orm_float(
                        self._timeout_env, self._default_timeout
                    )
                    self._sem = threading.BoundedSemaphore(self.cap_in_use)
        return self._sem

    def reset(self) -> None:
        """Drop the built semaphore so the next ``.get()`` rebuilds from env.

        Used by tests that reconfigure the pool in-process (set a new env knob
        then expect the next call to honour it) WITHOUT a full module reload.
        ``importlib.reload(server)`` resets implicitly (fresh instance), so the
        reload-based tests do not need this; the in-process tests do.
        """
        with self._lock:
            self._sem = None
            self.cap_in_use = None
            self.timeout_in_use = None


# --- Anti-freeze hot-path embed guards (#227 + #276 G7) ---------------------
# FastMCP runs sync `def` tool handlers DIRECTLY on the asyncio event-loop
# thread (no to_thread). A single blocking embedder.embed() call therefore
# freezes the whole server (including /health). The three query-embed tools
# (find_examples, suggest_pattern, find_style_override) embed off the loop via
# a worker thread, bounded by a module-level semaphore so a burst of concurrent
# embeds cannot exhaust the upstream Ollama connection pool / queue unboundedly.
#
# #276 G7 - CANCEL-SAFE SLOT: the original guard used an asyncio.Semaphore whose
# slot was released in a coroutine `finally`. When a client disconnected mid-embed
# FastMCP cancelled the coroutine; the `finally: sem.release()` ran on cancel -
# but the underlying embed thread (inside embed_async's own to_thread) kept
# running and STILL held the Ollama connection. The slot freed early → the exact
# #276 pool-drain. Fixed by porting the offload_bounded thread-held
# BoundedSemaphore pattern: acquire()/release() now happen INSIDE the worker
# thread, so the slot's lifetime is tied to the THREAD, not the coroutine. A
# cancel can no longer free a slot while a thread is still embedding. In the
# worker thread we call the SYNC embed path DIRECTLY (NOT embed_async - that
# would double-to_thread / spin a child event loop, R-A6), routing through the
# short-timeout query client so a hung query never inherits the 1200s batch
# read timeout.
#
# EMBEDDER_MAX_CONCURRENCY caps in-flight query embeds. Callers that cannot
# acquire a slot within EMBEDDER_SLOT_ACQUIRE_TIMEOUT (constants SSOT, validated
# < EMBEDDER_TIMEOUT_READ_QUERY at startup) fail fast (EmbedOverloaded) instead
# of queueing forever.
class EmbedOverloaded(RuntimeError):
    """Raised when the bounded embed semaphore cannot be acquired in time.

    Surfaced to the MCP client as a fast, actionable overload message rather
    than letting the request hang on an unbounded queue (#227).
    """


# #276 G7: a *threading* BoundedSemaphore (NOT asyncio.Semaphore) so the slot is
# acquired/released inside the worker thread - no event-loop affinity, no
# released-on-cancel hazard. Built once on first .get() (lazy + post-dotenv so
# the cap honours a .env-only EMBEDDER_MAX_CONCURRENCY); a module reload (tests)
# yields a fresh instance so the next .get() rebuilds from the freshly-set env.
_embed_pool = _LazyBoundedSemaphore(
    "EMBEDDER_MAX_CONCURRENCY", EMBEDDER_MAX_CONCURRENCY,
    "EMBEDDER_SLOT_ACQUIRE_TIMEOUT", EMBEDDER_SLOT_ACQUIRE_TIMEOUT,
)


def _get_embed_semaphore() -> threading.BoundedSemaphore:
    """Return the embed threading BoundedSemaphore (built once on first use)."""
    return _embed_pool.get()


def _cap_query_text(embedder, text: str) -> str:
    """Truncate `text` to EMBEDDER_TOKEN_BUDGET tokens BEFORE prepending INSTRUCT.

    A user can paste kilobytes of text into a query/intent/selector argument;
    embedding the whole thing wastes the upstream context window and slows the
    hot path. We keep only the first budgeted chunk (split_by_token_budget
    returns the whole string unchanged when it already fits - a cheap no-op for
    normal short queries).
    """
    from src.indexer.embedder import split_by_token_budget

    chars_per_token = getattr(embedder, "chars_per_token", None) or 4.0
    return split_by_token_budget(text, EMBEDDER_TOKEN_BUDGET, chars_per_token)[0]


def _embed_sync_query(embedder, payload: list[str]) -> list[list[float]]:
    """Run a SINGLE query embed SYNCHRONOUSLY, routed through the short-timeout client.

    Called only from inside the embed worker thread (#276 G7). Prefers the HTTP
    base's ``_embed_with_timeout`` so the query goes through the dedicated
    short-timeout (TIMEOUT_EMBEDDER_READ_QUERY) client - never the 1200s batch
    client. Falls back to the Protocol-guaranteed sync ``embed`` for embedders
    without that method (e.g. FakeEmbedder in tests). We deliberately do NOT call
    ``embed_async`` here - that would nest a second to_thread / child loop inside
    this already-offloaded thread (R-A6).
    """
    fn = getattr(embedder, "_embed_with_timeout", None)
    if callable(fn):
        return fn(payload, TIMEOUT_EMBEDDER_READ_QUERY)
    return embedder.embed(payload)


def _embed_query_in_thread(embedder, payload: list[str]) -> list[float]:
    """Worker-thread body for a single query embed - thread-held slot (#276 G7).

    Acquires the embed BoundedSemaphore HERE (on the worker thread) so the slot
    lives with the thread, not the caller coroutine. On acquire-timeout: fast
    fail with EmbedOverloaded. The sync embed runs while the slot is held and the
    slot is released only in this thread's ``finally`` - a cancelled coroutine
    can never free it early while the embed is still in flight.
    """
    sem = _get_embed_semaphore()  # builds the pool → populates cap_in_use below
    # _get_embed_semaphore() (just called, same thread) always populates
    # cap_in_use / timeout_in_use under the pool lock before returning, so these
    # are never None here - read them directly (no default-constant fallback).
    cap = _embed_pool.cap_in_use
    slot_timeout = _embed_pool.timeout_in_use
    if not sem.acquire(timeout=slot_timeout):
        raise EmbedOverloaded(
            "server busy - too many concurrent embedding requests"
            f" (max {cap}); retry shortly"
        )
    try:
        vecs = _embed_sync_query(embedder, payload)
    finally:
        sem.release()
    return vecs[0]


async def _embed_query(embedder, instruct: str, text: str) -> list[float]:
    """Embed a single query string off the event-loop's blocking path (#227 + #276 G7).

    - Caps `text` to the token budget before prepending `instruct`.
    - Offloads the whole acquire→embed→release sequence to ONE worker thread, so
      the bounded-slot lifetime is tied to that thread (cancel-safe, #276 G7) and
      the event loop never blocks on the embed.
    - The embed runs through the SHORT query read timeout (30s) so a single hung
      query never inherits the 1200s batch timeout.

    Returns the embedding vector. Raises EmbedOverloaded on slot saturation, or
    re-raises any embed failure (caller maps to the tool's existing "embedding
    query failed" message to preserve behaviour).
    """
    capped = _cap_query_text(embedder, text)
    return await asyncio.to_thread(
        _embed_query_in_thread, embedder, [instruct + capped]
    )


def offload(fn):
    """Move a blocking sync tool body off the asyncio event loop (#227 root).

    FastMCP 2.14.x runs a sync ``def`` tool handler DIRECTLY on the event-loop
    thread (no implicit to_thread). Any Neo4j/Postgres I/O inside such a handler
    therefore blocks the whole server - a single slow/locked query freezes
    /health and every concurrent request (the #227 504). This decorator wraps a
    sync handler in an ``async def`` that runs the original body in a worker
    thread via ``asyncio.to_thread``, so the loop stays free.

    Mechanism notes:
      * ``functools.wraps`` copies ``__wrapped__`` so ``inspect.signature``
        (which FastMCP uses to build the input schema) resolves to the ORIGINAL
        handler signature - the generic ``*a, **k`` wrapper is invisible to
        introspection, so the tool schema and FastMCP's "no **kwargs" rule are
        preserved.
      * ``asyncio.to_thread`` copies the current ``contextvars.Context``, so the
        per-request ContextVars (``_api_key_id_var``, tenant, profile) propagate
        into the worker thread unchanged.
      * Place BETWEEN ``@mcp.tool(...)`` and the sync ``def`` so FastMCP
        registers the resulting async callable. Do NOT apply to handlers that
        are already ``async def`` (they offload their own blocking body).
    """
    @functools.wraps(fn)
    async def wrapper(*a, **k):
        return await asyncio.to_thread(functools.partial(fn, *a, **k))

    return wrapper


# --- Bounded offload for the ORM-validation tools (#273) --------------------
# The 4 ORM tools (resolve_orm_chain / validate_domain / validate_depends /
# validate_relation) run a Neo4j traversal that, on a dense inheritance graph,
# could enumerate millions of paths and hang for hours (the #273 zombie
# transactions). WI-1 caps each query with a Neo4j-side timeout; this layer
# adds the SECOND half of the defence (mirroring ADR-0046 for the embed path):
# an asyncio.Semaphore so a fan-out burst of dense ORM calls cannot drain the
# default ThreadPoolExecutor that asyncio.to_thread shares with every other
# @offload tool. Without the cap, N hung ORM calls starve model_inspect /
# entity_lookup / etc. for the duration of the Neo4j timeout window.
#
# ORM_QUERY_MAX_CONCURRENCY caps in-flight ORM queries; ORM_SLOT_ACQUIRE_TIMEOUT
# is the fast-reject window. Both are the SSOT in constants.py (imported above)
# and validated at server startup by _validate_orm_env() - a value of 0 or an
# acquire-timeout >= the Neo4j query timeout is rejected fail-fast there.
#
# A *threading* BoundedSemaphore (NOT asyncio.Semaphore) is used here, built
# lazily on first ORM call (after init_dotenv() settles). Rationale (the
# #276 / CRITICAL-2 fix):
#   - A threading semaphore does not bind to an event loop, so there is no lazy
#     init dance and no per-loop ownership to reason about.
#   - acquire()/release() happen INSIDE the worker thread (see offload_bounded),
#     so the slot's lifetime is tied to the THREAD, not the coroutine. When a
#     client disconnects mid-call FastMCP cancels the wrapper coroutine and
#     `await asyncio.to_thread(...)` raises CancelledError immediately - but the
#     worker thread keeps running the blocking Neo4j call. Because the slot is
#     held by the thread, cancellation can no longer free it early; the slot
#     stays held until the thread itself exits (its own `finally`), i.e. until
#     the query finishes or the Neo4j-side timeout (NEO4J_QUERY_TIMEOUT_SECONDS)
#     frees it. This is the pool-drain protection #273/#276 require.
#   - BoundedSemaphore (not plain) turns any over-release into an immediate
#     ValueError instead of silent value inflation (HIGH #4).
# Built LAZILY on first ORM call, NOT at import. ``config.init_dotenv()`` runs in
# the __main__ block AFTER this module imports, so the import-time constant can be
# stale for a value set ONLY in .env (a supported config path per ADR-0031). A
# threading semaphore has no event-loop affinity, so first-use construction is
# safe behind a plain lock (none of the lazy-per-loop dance the embed Semaphore
# needs). Resolving the cap + acquire-timeout here too means the live semaphore,
# the fast-reject window, and _validate_orm_env() all read the SAME post-dotenv
# value - there is no import-time/.env mismatch to warn about (#275 LOW).
class OrmOverloaded(RuntimeError):
    """Raised when the bounded ORM semaphore cannot be acquired in time.

    Caught in ``offload_bounded`` and surfaced to the MCP client as a fast,
    actionable overload *string* (NOT a protocol-level error) - uniform with the
    embed path's EmbedOverloaded (ADR-0046 D3) and the ADR-0023 raw-text posture
    (so "server busy" never shows up as ``isError=true``, MED isError fix).
    """


# Built LAZILY on first ORM call (after init_dotenv() settles), NOT at import, so
# the cap honours a .env-only ORM_QUERY_MAX_CONCURRENCY (ADR-0031). cap_in_use /
# timeout_in_use are cached on the pool so the hot path (_run_in_thread), the
# fast-reject window, and the logged max all read the SAME post-dotenv value.
_orm_pool = _LazyBoundedSemaphore(
    "ORM_QUERY_MAX_CONCURRENCY", ORM_QUERY_MAX_CONCURRENCY,
    "ORM_SLOT_ACQUIRE_TIMEOUT", ORM_SLOT_ACQUIRE_TIMEOUT,
)


def _get_orm_semaphore() -> threading.BoundedSemaphore:
    """Return the ORM threading BoundedSemaphore (built once on first use)."""
    return _orm_pool.get()


# --- Bounded offload for NON-ORM heavy reads (#276 G6) ----------------------
# A SEPARATE threading BoundedSemaphore pool for non-ORM heavy reads (currently
# impact_analysis - a 6-query fan-out over TARGETS_MODEL / DEPENDS_ON / BOUND_TO
# / PATCHES that can run long on a dense graph). Kept distinct from the ORM pool
# so a fan-out burst of one class cannot starve the other (#276 G6). Built lazily
# on first use under its own lock, post-dotenv, exactly like the ORM semaphore.
# A distinct pool object from _orm_pool (#276 G6) so a fan-out burst of one read
# class cannot starve the other. Same lazy / post-dotenv build as the ORM pool.
_nonorm_pool = _LazyBoundedSemaphore(
    "NONORM_READ_MAX_CONCURRENCY", NONORM_READ_MAX_CONCURRENCY,
    "NONORM_SLOT_ACQUIRE_TIMEOUT", NONORM_SLOT_ACQUIRE_TIMEOUT,
)


def _get_nonorm_semaphore() -> threading.BoundedSemaphore:
    """Return the non-ORM threading BoundedSemaphore (built once on first use).

    Peer of :func:`_get_orm_semaphore` for the non-ORM read pool (#276 G6); a
    distinct pool object so one read class cannot starve the other.
    """
    return _nonorm_pool.get()


def _make_bounded_offload(
    pool: "_LazyBoundedSemaphore",
    metric_overloaded: "Callable[[str], None]",
    metric_timeout: "Callable[[str], None]",
    log_label: str,
    overload_phrase: str,
    timeout_log_prefix: str,
    tool_name_default: str,
    timeout_msg_default: str,
    call_context_fn=None,
):
    """Build a bounded-offload decorator for ONE concurrency pool (#279).

    Single source of the thread-held-semaphore offload machinery the ORM and
    non-ORM decorators used to duplicate (~70 LOC each). Consolidating them means
    a future cancel-safety fix lands in ONE place and cannot silently regress one
    pool while patching the other - the exact missed-fix class that bit the embed
    path in #275 (fixed late in #278).

    THREAD-LIFETIME RELEASE (the #276 / CRITICAL-2 invariant - preserved verbatim):
      ``acquire()``/``release()`` run INSIDE the worker thread, so a slot's
      lifetime is bound to the THREAD, never the (possibly cancelled) caller
      coroutine. When a client disconnects mid-call FastMCP cancels the wrapper
      coroutine and the awaited ``to_thread`` future raises ``CancelledError``
      immediately - but the worker thread keeps running and STILL HOLDS the slot
      until its own ``finally`` releases it. Overload + timeout metric/log
      bookkeeping also runs in-thread, so it is recorded even after a cancel.
      ``functools.wraps`` preserves ``__wrapped__`` so FastMCP introspects the
      ORIGINAL handler signature; ``asyncio.to_thread`` copies the current
      ``contextvars.Context`` so per-request ContextVars propagate into the thread.

    The four observable strings that differ between the ORM and non-ORM pools are
    parameters so the log lines (ops grep them) and the client-facing busy string
    stay byte-identical to the pre-consolidation behaviour:
      * ``log_label``         - overload-log prefix ("ORM tool" | "non-ORM read").
      * ``overload_phrase``   - exception text ("ORM-validation requests" |
                                "heavy read requests").
      * ``timeout_log_prefix``- timeout-log prefix ("ORM query" | "non-ORM read
                                query").
      * ``timeout_msg_default``- wrapper fallback when the timeout exc has no
                                ``user_message``.
    ``call_context_fn`` (the ORM model/version/profile context, ``None`` for the
    non-ORM pool whose tools have a different signature) decides whether the log
    lines append a trailing context string - when ``None`` the format strings
    omit the trailing ``%s`` entirely, so a non-ORM log line carries NO trailing
    space (observable byte-identical to the old non-ORM decorator).
    """
    def decorator(fn):
        tool_name = getattr(fn, "__name__", tool_name_default)

        def _run_in_thread(a, k):
            # Runs entirely on the worker thread. The slot is acquired and
            # released here so its lifetime is bound to THIS thread, never to the
            # (possibly cancelled) caller coroutine. pool.get() also resolves the
            # cap + acquire-timeout the semaphore was sized with (post-dotenv), so
            # the reject window and the logged max match the live semaphore.
            sem = pool.get()
            # pool.get() (just called, same thread) always populates cap_in_use /
            # timeout_in_use under its lock before returning, so these are never
            # None here - read them directly (no _default_* fallback needed).
            cap = pool.cap_in_use
            slot_timeout = pool.timeout_in_use
            if not sem.acquire(timeout=slot_timeout):
                # Saturated: fast-reject. Record metric + log here so cancel-storms
                # are never invisible (the coroutine may already be gone).
                metric_overloaded(tool_name)
                if call_context_fn is not None:
                    logger.warning(
                        "%s overloaded - semaphore full (max %d): tool=%s %s",
                        log_label,
                        cap,
                        tool_name,
                        call_context_fn(a, k),
                    )
                else:
                    logger.warning(
                        "%s overloaded - semaphore full (max %d): tool=%s",
                        log_label,
                        cap,
                        tool_name,
                    )
                raise OrmOverloaded(
                    f"server busy - too many concurrent {overload_phrase}"
                    f" (max {cap}); retry shortly"
                )
            try:
                return fn(*a, **k)
            except OrmQueryTimeout:
                # Record the timeout metric + log IN-THREAD so it is observed even
                # if the awaiting coroutine was already cancelled (MED cancel-path
                # blind spot). Re-raise so the wrapper can return user_message to a
                # still-connected client.
                metric_timeout(tool_name)
                if call_context_fn is not None:
                    logger.warning(
                        "%s timed out: tool=%s %s",
                        timeout_log_prefix,
                        tool_name,
                        call_context_fn(a, k),
                    )
                else:
                    logger.warning(
                        "%s timed out: tool=%s",
                        timeout_log_prefix,
                        tool_name,
                    )
                raise
            finally:
                sem.release()

        @functools.wraps(fn)
        async def wrapper(*a, **k):
            try:
                return await asyncio.to_thread(_run_in_thread, a, k)
            except OrmOverloaded as exc:
                # Uniform with the embed path + ADR-0023: return the overload
                # message as a normal str, never a protocol-level isError.
                return str(exc)
            except OrmQueryTimeout as exc:
                # Metric/log already recorded in-thread; return the normal str
                # result. We never leak the Cypher / user code here.
                return getattr(exc, "user_message", timeout_msg_default)

        return wrapper

    return decorator


# The two decorators (`offload_bounded` / `offload_bounded_nonorm`) are bound
# from this factory just below the metric + call-context helpers they reference
# (those are defined a little further down - the bindings must come after them).


def _validate_orm_env() -> None:
    """Fail-fast guard for the ORM concurrency / timeout env knobs (HIGH #3).

    Called once at server startup (NOT at import - see the call site in the
    __main__ block), so pytest collection and tool imports never trip these
    assertions. Values are re-read from ``os.getenv`` here rather than trusting
    the import-time constants, because ``config.init_dotenv()`` runs in the
    __main__ block AFTER this module imports - so a ``.env``-only value would
    not yet be reflected in the module-level constant when this runs. Each
    foot-gun below silently reverts a load-bearing #273/#276 protection:

      * NEO4J_QUERY_TIMEOUT_SECONDS <= 0 - the neo4j driver treats 0 as
        "no timeout", reverting the core #273 per-query-timeout fix.
      * ORM_QUERY_MAX_CONCURRENCY <= 0 - every ORM call fast-rejects forever
        (0 slots can never be acquired).
      * ORM_SLOT_ACQUIRE_TIMEOUT >= NEO4J_QUERY_TIMEOUT_SECONDS - the reject is
        no longer "fast", so an overloaded server pins a worker-thread slot for
        as long as the query itself would run. The .env.example states this
        constraint; this enforces it.
      * NONORM_READ_MAX_CONCURRENCY <= 0 / NONORM_SLOT_ACQUIRE_TIMEOUT >=
        NEO4J_QUERY_TIMEOUT_SECONDS - same two foot-guns for the separate
        non-ORM heavy-read pool (#276 G6).
      * EMBEDDER_MAX_CONCURRENCY <= 0 - BoundedSemaphore(0) can never be
        acquired, so every query-embed fast-rejects forever (#276 G7); same
        foot-gun the ORM/non-ORM pools already guard.
      * EMBEDDER_SLOT_ACQUIRE_TIMEOUT >= EMBEDDER_TIMEOUT_READ_QUERY - the
        query-embed fast-reject is no longer "fast", so an overloaded embedder
        pins a worker-thread slot for as long as the embed itself would run
        (#276 G7). Must stay strictly below the query read timeout.
    """
    neo4j_timeout = int(
        os.getenv("NEO4J_QUERY_TIMEOUT_SECONDS", str(NEO4J_QUERY_TIMEOUT_SECONDS))
    )
    orm_max = int(
        os.getenv("ORM_QUERY_MAX_CONCURRENCY", str(ORM_QUERY_MAX_CONCURRENCY))
    )
    orm_acquire = float(
        os.getenv("ORM_SLOT_ACQUIRE_TIMEOUT", str(ORM_SLOT_ACQUIRE_TIMEOUT))
    )
    nonorm_max = int(
        os.getenv("NONORM_READ_MAX_CONCURRENCY", str(NONORM_READ_MAX_CONCURRENCY))
    )
    nonorm_acquire = float(
        os.getenv("NONORM_SLOT_ACQUIRE_TIMEOUT", str(NONORM_SLOT_ACQUIRE_TIMEOUT))
    )
    embed_acquire = float(
        os.getenv("EMBEDDER_SLOT_ACQUIRE_TIMEOUT", str(EMBEDDER_SLOT_ACQUIRE_TIMEOUT))
    )
    embed_read_query = int(
        os.getenv("EMBEDDER_TIMEOUT_READ_QUERY", str(TIMEOUT_EMBEDDER_READ_QUERY))
    )
    embed_max = int(
        os.getenv("EMBEDDER_MAX_CONCURRENCY", str(EMBEDDER_MAX_CONCURRENCY))
    )
    if neo4j_timeout <= 0:
        raise SystemExit(
            "FATAL: NEO4J_QUERY_TIMEOUT_SECONDS must be > 0 "
            f"(got {neo4j_timeout}); 0 disables the per-query timeout and "
            "reverts the #273 zombie-transaction fix."
        )
    if orm_max <= 0:
        raise SystemExit(
            "FATAL: ORM_QUERY_MAX_CONCURRENCY must be > 0 "
            f"(got {orm_max}); 0 makes every ORM-validation tool fast-reject "
            "forever."
        )
    if orm_acquire >= neo4j_timeout:
        raise SystemExit(
            "FATAL: ORM_SLOT_ACQUIRE_TIMEOUT "
            f"({orm_acquire}) must be < NEO4J_QUERY_TIMEOUT_SECONDS "
            f"({neo4j_timeout}) so an overloaded server rejects fast instead "
            "of pinning a worker-thread slot for the whole traversal window."
        )
    if nonorm_max <= 0:
        raise SystemExit(
            "FATAL: NONORM_READ_MAX_CONCURRENCY must be > 0 "
            f"(got {nonorm_max}); 0 makes every non-ORM heavy read fast-reject "
            "forever (#276 G6)."
        )
    if nonorm_acquire >= neo4j_timeout:
        raise SystemExit(
            "FATAL: NONORM_SLOT_ACQUIRE_TIMEOUT "
            f"({nonorm_acquire}) must be < NEO4J_QUERY_TIMEOUT_SECONDS "
            f"({neo4j_timeout}) so an overloaded server rejects fast instead "
            "of pinning a worker-thread slot for the whole query window (#276 G6)."
        )
    if embed_max <= 0:
        raise SystemExit(
            "FATAL: EMBEDDER_MAX_CONCURRENCY must be > 0 "
            f"(got {embed_max}); 0 makes every query-embed fast-reject forever "
            "(BoundedSemaphore(0) can never be acquired) - #276 G7."
        )
    if embed_acquire >= embed_read_query:
        raise SystemExit(
            "FATAL: EMBEDDER_SLOT_ACQUIRE_TIMEOUT "
            f"({embed_acquire}) must be < EMBEDDER_TIMEOUT_READ_QUERY "
            f"({embed_read_query}) so an overloaded embedder rejects fast "
            "instead of pinning a worker-thread slot for the whole query-embed "
            "window (#276 G7)."
        )


def _orm_call_context(args: tuple, kwargs: dict) -> str:
    """Build a WARNING-safe context string from an ORM tool's call args.

    Logs only structural identifiers (model / version / profile) - never the
    domain / dotted_path / code text the user submitted. All 4 ORM tools take
    ``model`` first positionally, but the positional INDEX of ``odoo_version``
    differs: it is positional[2] for resolve_orm_chain / validate_domain /
    validate_depends, but positional[3] for validate_relation
    (model, field, target_model, odoo_version). FastMCP dispatches by keyword,
    so ``kwargs['odoo_version']`` is the reliable source; the positional
    fallbacks below are best-effort only and read defensively so a signature
    drift cannot raise here. We do NOT guess validate_relation's positional[3]
    to avoid mislabelling target_model as the version.
    """
    model = kwargs.get("model")
    if model is None and len(args) >= 1:
        model = args[0]
    # odoo_version: prefer kwargs (FastMCP always passes it by keyword). The
    # positional[2] fallback is correct for 3 of the 4 tools; for
    # validate_relation positional[2] is target_model. Model names also contain
    # dots ('res.partner'), so only trust the positional fallback when the
    # value is purely numeric like '17.0' - otherwise leave it None rather
    # than log target_model under the version label.
    version = kwargs.get("odoo_version")
    if (
        version is None
        and len(args) >= 3
        and isinstance(args[2], str)
        and re.fullmatch(r"\d+(\.\d+)?", args[2])
    ):
        version = args[2]
    profile = kwargs.get("profile_name")
    return f"model={model!r} odoo_version={version!r} profile={profile!r}"


def _metric_orm_query_timeout(tool: str) -> None:
    """Increment the ORM query-timeout counter; never raise on metrics failure."""
    try:
        from src.metrics import orm_query_timeout_total

        orm_query_timeout_total.labels(tool=tool).inc()
    except Exception:  # pragma: no cover - observability must not break the tool
        pass


def _metric_orm_overloaded(tool: str) -> None:
    """Increment the ORM overload counter; never raise on metrics failure."""
    try:
        from src.metrics import orm_overloaded_total

        orm_overloaded_total.labels(tool=tool).inc()
    except Exception:  # pragma: no cover - observability must not break the tool
        pass


def _metric_nonorm_overloaded(tool: str) -> None:
    """Increment the non-ORM overload counter; never raise on metrics failure (#276 G6)."""
    try:
        from src.metrics import nonorm_overloaded_total

        nonorm_overloaded_total.labels(tool=tool).inc()
    except Exception:  # pragma: no cover - observability must not break the tool
        pass


def _metric_nonorm_query_timeout(tool: str) -> None:
    """Increment the non-ORM query-timeout counter; never raise (#276 G5).

    Kept separate from ``orm_query_timeout_total`` so ops can distinguish which
    pool's queries are hitting the per-query Neo4j timeout (the non-ORM pool is
    dominated by impact_analysis fan-outs, not the ORM-validation tools).
    """
    try:
        from src.metrics import nonorm_query_timeout_total

        nonorm_query_timeout_total.labels(tool=tool).inc()
    except Exception:  # pragma: no cover - observability must not break the tool
        pass


def _nonorm_timeout_response(exc: OrmQueryTimeout, tool: str) -> str:
    """Standard inline-catch body for an async/mutating Neo4j-read tool (ADR-0050).

    The EMBED tools (``async def``, embed on the event loop before ``to_thread``)
    and the mutating ``set_active_version`` carry no ``@offload_neo4j`` backstop,
    so each catches ``OrmQueryTimeout`` itself and converts it to the clean
    ADR-0023 string + the non-ORM timeout metric. That two-line body was repeated
    at every inline catch; this collapses it to one call. The ``except
    OrmQueryTimeout as exc:`` handler at each site is preserved verbatim (only the
    body changes) so the per-read structural guard in
    ``tests/test_resolve_timeout_guard.py`` still sees a timeout-catching ``try``.
    """
    _metric_nonorm_query_timeout(tool)
    return exc.user_message


# Bounded offload for the 4 ORM-validation tools (#273): a thread-held
# BoundedSemaphore so a fan-out burst of dense ORM traversals cannot drain the
# shared ThreadPoolExecutor / Neo4j pool. The slot is thread-bound (#276
# CRITICAL-2 cancel-safety - see _make_bounded_offload). Accepted trade-off: a
# fast-reject occupies one executor slot for up to ORM_SLOT_ACQUIRE_TIMEOUT while
# it blocks on acquire(); the Neo4j pool (~24) comfortably exceeds the ORM cap (8)
# plus that headroom, and rejecting in-thread is what makes the accounting
# cancellation-safe. Bound here (not at the factory def) so the metric +
# call-context helpers above are already defined.
offload_bounded = _make_bounded_offload(
    pool=_orm_pool,
    metric_overloaded=_metric_orm_overloaded,
    metric_timeout=_metric_orm_query_timeout,
    log_label="ORM tool",
    overload_phrase="ORM-validation requests",
    timeout_log_prefix="ORM query",
    tool_name_default="orm_tool",
    timeout_msg_default=(
        "ORM query timed out - the request was too expensive to"
        " complete; narrow the model/version and retry."
    ),
    call_context_fn=_orm_call_context,
)


# Bounded offload for NON-ORM heavy reads (#276 G6, currently impact_analysis - a
# 6-query fan-out). Identical machinery, but a SEPARATE pool so a burst of one
# read class cannot starve the other, distinct metrics, and no call-context (the
# non-ORM tools have a different signature than the ORM tools).
offload_bounded_nonorm = _make_bounded_offload(
    pool=_nonorm_pool,
    metric_overloaded=_metric_nonorm_overloaded,
    metric_timeout=_metric_nonorm_query_timeout,
    log_label="non-ORM read",
    overload_phrase="heavy read requests",
    timeout_log_prefix="non-ORM read query",
    tool_name_default="nonorm_tool",
    timeout_msg_default=(
        "Query timed out - the request was too expensive to"
        " complete; narrow the model/version and retry."
    ),
    call_context_fn=None,
)


# --- Pool-less Neo4j offload for the read-surface discriminator tools --------
# `@offload_neo4j` = `@offload` + an OrmQueryTimeout catch that records the
# non-ORM timeout metric + returns the clean English `user_message` string
# (ADR-0023 raw-text contract - never a protocol-level isError). It is the
# backstop that turns a *raised* OrmQueryTimeout into a clean string; the
# per-query work (routing bare `session.run(...)` through `_data_bounded` /
# `_single_bounded`, which wrap the Cypher in neo4j.Query(timeout=...) and
# convert a tx-timeout ClientError → OrmQueryTimeout) still happens at every
# query site. Both halves are required - see the timeout-hardening design §1.1.
#
# Kept SEPARATE from `@offload` (not a retrofit): `@offload` also wraps handlers
# that do non-Neo4j work (Postgres reads, on-disk file reads) and embed-then-
# offload handlers (find_style_override embeds on the event loop BEFORE the
# to_thread hop) - catching OrmQueryTimeout there would be pointless (no Neo4j)
# or wrong (would swallow / mislabel a timeout from a different subsystem).
#
# POOL-LESS by design (NO semaphore). The tools this wraps are single bounded
# queries or small fixed multi-query helpers, each individually 30s-bounded by
# the per-query Neo4j timeout - not the heavy fan-out drain the bounded pools
# (offload_bounded / offload_bounded_nonorm) were built to contain. Putting them
# behind the 8-slot non-ORM pool would create a NEW starvation surface and make
# them newly emit the "server busy" string (a client-visible wire change these
# tools never had). Concurrency containment is already provided by the 30s
# per-query bound + uvicorn limit_concurrency + the shared to_thread executor.
# A future profiling pass can PROMOTE any single tool to @offload_bounded_nonorm
# with a one-line decorator swap if it turns out to be a genuine fan-out drain.
def offload_neo4j(fn):
    """Offload a sync Neo4j-read handler off the event loop, clean on timeout.

    Mechanism notes (mirrors :func:`offload` + the in-thread metric invariant of
    :func:`_make_bounded_offload`):
      * ``functools.wraps`` preserves ``__wrapped__`` so FastMCP introspects the
        ORIGINAL handler signature (the generic ``*a, **k`` wrapper is invisible
        to ``inspect.signature``).
      * ``asyncio.to_thread`` copies the current ``contextvars.Context`` so the
        per-request ContextVars (api_key_id, tenant, profile) propagate into the
        worker thread unchanged.
      * The OrmQueryTimeout metric is recorded IN-THREAD (in ``_run``) so it is
        counted even if the awaiting coroutine was already cancelled by a client
        disconnect (the #276 / CRITICAL-2 cancel-path invariant). The exception
        re-raises to the async wrapper which only *returns* ``user_message`` -
        it does NOT re-record, so the metric fires exactly once.
      * A non-OrmQueryTimeout exception propagates unchanged - we never swallow
        an unrelated error.
    """
    tool_name = getattr(fn, "__name__", "neo4j_read")

    def _run(a, k):
        try:
            return fn(*a, **k)
        except OrmQueryTimeout:
            # Record in-thread so a cancelled coroutine still counts it (#276).
            _metric_nonorm_query_timeout(tool_name)
            raise

    @functools.wraps(fn)
    async def wrapper(*a, **k):
        try:
            return await asyncio.to_thread(_run, a, k)
        except OrmQueryTimeout as exc:
            # Metric already recorded in-thread; return the clean str (no Cypher).
            return getattr(
                exc, "user_message",
                "Query timed out - narrow the entity/version and retry.",
            )

    return wrapper


def _nonorm_timeout(label: str) -> "OrmQueryTimeout":
    """Build an OrmQueryTimeout for a bounded non-ORM read (#276 G5, ADR-0023 tone)."""
    return OrmQueryTimeout(
        f"Query timed out after {NEO4J_QUERY_TIMEOUT_SECONDS}s while computing "
        f"{label}. The dependency graph may be unusually dense - try a more "
        f"specific entity or retry later."
    )


def _data_bounded(session, text: str, label: str, **params) -> list[dict]:
    """Run a NON-ORM read under the per-query Neo4j timeout, return ``.data()`` (#276 G5).

    The Cypher is wrapped in ``neo4j.Query(timeout=NEO4J_QUERY_TIMEOUT_SECONDS)``
    via the shared ``_bounded`` helper (reused from src.mcp.orm - a peer module
    server already imports - so there is NO duplicate timeout helper). Neo4j
    Result consumption is LAZY, so the transaction-timeout ``ClientError`` fires
    during ``.data()``, not during ``session.run`` - both are therefore inside
    the try here. A tx-timeout ``ClientError`` becomes ``OrmQueryTimeout`` so the
    ``offload_bounded_nonorm`` wrapper records the metric in-thread and surfaces a
    clean English message; any other ``ClientError`` propagates unchanged.

    ``label`` is a short English noun phrase naming what was being resolved (e.g.
    "impact analysis for 'sale.order'"), used only in the timeout message - never
    leaks Cypher.
    """
    from neo4j.exceptions import ClientError

    try:
        return session.run(_bounded(text), **params).data()
    except ClientError as exc:
        if _is_tx_timeout(exc):
            raise _nonorm_timeout(label) from exc
        raise


def _single_bounded(session, text: str, label: str, **params):
    """Run a NON-ORM read under the per-query Neo4j timeout, return ``.single()`` (#276 G5).

    Peer of :func:`_data_bounded` for single-row queries (the impact_analysis
    existence checks). Same lazy-consumption + tx-timeout conversion contract.
    """
    from neo4j.exceptions import ClientError

    try:
        return session.run(_bounded(text), **params).single()
    except ClientError as exc:
        if _is_tx_timeout(exc):
            raise _nonorm_timeout(label) from exc
        raise


def _get_api_key_id() -> str:
    """Return the API key ID for the current async/sync context.

    Uses a ContextVar so the value is isolated per coroutine - concurrent
    async requests running in the same event-loop thread cannot clobber each
    other's api_key_id (the old threading.local() approach suffered this race
    because asyncio is single-threaded but multiplexes coroutines).

    In production the middleware writes the api_key_id via
    _api_key_id_var.set() before each tool call and resets it in finally.
    Falls back to 'default' when not set (unit tests, CLI invocations).

    #248 context-boundary fallback
    ------------------------------
    On the stateful streamable-HTTP transport FastMCP runs the tool body in a
    ``contextvars.Context`` that is captured per-connection BEFORE the per-call
    ``UsageLogMiddleware.on_call_tool`` runs. The ``_api_key_id_var.set()`` the
    middleware performs therefore mutates a context that is NOT an ancestor of
    the tool-body execution context - so the tool body still reads the
    ``'default'`` sentinel even though the middleware (reading in its OWN
    context) logged the correct numeric PK. That asymmetry is exactly the #248
    bug: ``set_active_version`` / ``set_active_profile`` skipped the persist and
    returned "session context unavailable".

    When the ContextVar value is still the ``'default'`` sentinel we therefore
    make a second, additive attempt: recover the numeric PK directly from the
    CURRENT HTTP request's own ``X-API-Key`` header via the warm auth cache
    (the same machinery the middleware uses). This derives the id ONLY from the
    request's own header - never from a shared/global - so it cannot bleed an
    id from one request into another. On any miss / no HTTP request we keep
    returning ``'default'`` (graceful, no regression); this function never
    raises.

    Note on the runtime type: the annotation is ``str``, but on the #248
    header-recovery path the recovered value is the numeric PK from the auth
    cache, so the value can be an ``int`` at runtime. Downstream consumers that
    must coerce it (e.g. ``session.set_active_*_db`` doing ``int(...)``) already
    accept both forms; do not rely on it always being a string.
    """
    value = _api_key_id_var.get()
    if value != "default":
        # ContextVar propagated correctly (stdio, in-process tests, or a
        # transport where on_call_tool shares the tool-body context).
        return value
    # ContextVar is the sentinel - try the per-request header-recovery fallback.
    recovered = _recover_api_key_id_from_request()
    return recovered if recovered is not None else value


def _recover_api_key_id_from_request() -> int | None:
    """Recover the numeric api_key_id from the current request's X-API-Key header.

    Used as the #248 fallback when ``_api_key_id_var`` is still ``'default'``
    inside a tool body (FastMCP context-boundary loss on stateful
    streamable-HTTP). Reuses the exact warm-cache lookup the FastMCP middleware
    uses, so the id is the same numeric PK ``AuthMiddleware`` already resolved
    for THIS request.

    SECURITY: the key material comes solely from ``get_http_request()`` - the
    request bound to the current ASGI task - so the recovered id can never be
    one belonging to a different concurrent request / tenant. Returns ``None``
    on any of: no active HTTP request, no ``X-API-Key`` header, or a cache miss
    (TTL edge) - the caller then keeps the graceful ``'default'`` sentinel.
    Never raises.
    """
    try:
        from fastmcp.server.dependencies import get_http_request
        raw_key = get_http_request().headers.get("X-API-Key")
    except Exception:
        return None
    if not raw_key:
        return None
    try:
        from src.mcp.middleware import _cache_get
        hit, kid = _cache_get(raw_key)
        if hit and kid is not None:
            return kid
    except Exception:
        return None
    return None


def _http_request_has_api_key() -> bool:
    """True when the current call carries an ``X-API-Key`` header.

    Distinguishes an authenticated HTTP request (where a skipped session persist
    is a real error worth surfacing loudly - #248) from the benign stdio / CLI
    no-op (gentle note). Header presence is a far more reliable HTTP-auth signal
    than ``_get_api_key_id()`` (which can read ``'default'`` on the #248
    propagation path). Never raises - absence of an HTTP request returns False.
    """
    try:
        from fastmcp.server.dependencies import get_http_request
        return bool(get_http_request().headers.get("X-API-Key"))
    except Exception:
        return False


def _get_mcp_session_id() -> str:
    """Return the MCP transport session id for the current context (#251).

    Peer of :func:`_get_api_key_id`. The per-(api_key, mcp_session) pin store in
    ``src.mcp.session`` is keyed by this id so two concurrent Claude Code
    sessions sharing one API key keep independent version/profile pins.

    Resolution order:
      1. The ``_mcp_session_id_var`` ContextVar value (set by
         UsageLogMiddleware at call time), when it is not the sentinel.
      2. A DIRECT read of the ``mcp-session-id`` header from the current HTTP
         request. Unlike the #248 api-key path, this header survives intact on
         ``scope["headers"]`` across the BaseHTTPMiddleware↔request_ctx
         boundary, so no warm-cache recovery dance is needed - a plain header
         read suffices when the ContextVar did not propagate.

    Returns the ``_session._NO_SESSION_SENTINEL`` for stdio / no active HTTP
    request / header-less callers (which reproduces the pre-#251 single-pin
    semantics). Never raises.
    """
    value = _mcp_session_id_var.get()
    if value != _session._NO_SESSION_SENTINEL:
        return value
    # ContextVar is the sentinel - try a direct per-request header read.
    try:
        from fastmcp.server.dependencies import get_http_request
        from mcp.server.streamable_http import MCP_SESSION_ID_HEADER
        sid = get_http_request().headers.get(MCP_SESSION_ID_HEADER)
    except Exception:
        return _session._NO_SESSION_SENTINEL
    return sid if sid else _session._NO_SESSION_SENTINEL


class TenantResolutionDenied(RuntimeError):
    """Raised when the tenant of an AUTHENTICATED HTTP key cannot be resolved.

    Fail-closed sentinel for the #248 context-boundary edge (security): a
    streamable-HTTP request that carries an ``X-API-Key`` header but whose
    tenant is neither in the ContextVar nor recoverable from the warm cache, AND
    whose authoritative DB lookup is unavailable / fails. In that state we must
    NOT fall through to ``None`` (which ``_effective_allowed`` / ``_allowed_to_guc``
    treat as the unrestricted ``'*'`` admin sentinel) - doing so would let a
    tenant-scoped key read ACROSS tenants. Raising instead surfaces a clean deny
    at the read entry points (which already wrap tenant resolution in
    ``try/except`` → structured ToolResult error, or let the FastMCP layer turn
    the raise into a JSON-RPC error). No data is served on this edge.
    """


def _get_tenant_id() -> int | None:
    """Return the tenant_id for the current async/sync context (ADR-0034 D4.1 plumbing).

    Populated by UsageLogMiddleware (tool_log_middleware.py) from
    request.state.tenant_id before each tool call, reset in the finally block.
    Returns None when not set - this covers:
      - Unit tests and CLI invocations (no request context)
      - Global/admin keys (tenant_id IS NULL in DB)
      - Any code path that has not yet been wired to carry tenant context

    None means admin/global access (legacy NULL-tenant key, unit tests, CLI) -
    consumed by ``_effective_allowed`` (WI-4) as "unrestricted" while a real
    tenant id scopes every user-data query to that tenant's allowed profiles.

    ContextVar semantics: each coroutine has its own isolated copy so
    concurrent requests cannot interfere with each other's tenant scope.

    #248 context-boundary fallback (SECURITY - tenant-isolation bypass)
    ------------------------------------------------------------------
    On the stateful streamable-HTTP transport FastMCP runs the tool body in a
    ``contextvars.Context`` captured per-connection BEFORE the per-call
    ``UsageLogMiddleware.on_call_tool`` runs. ``_set_server_tenant_id(tenant_id)``
    therefore mutates a context that is NOT an ancestor of the tool-body context,
    so a bare ``_tenant_id_var.get()`` reads ``None`` for EVERY HTTP call - even a
    tenant-scoped key. ``None`` then flows ``_get_allowed_profiles`` →
    ``_effective_allowed`` → ``_allowed_to_guc(None) = '*'`` → the RLS
    ``app.allowed_profiles`` GUC becomes ``'*'`` and the policy reads ALL profiles
    across ALL tenants. That is the exact parallel of the api_key_id bug
    (commit ddada46), here with a tenant-isolation (ADR-0034) consequence.

    So when the ContextVar is ``None`` we make a second, additive attempt to
    recover the tenant from the CURRENT request's own ``X-API-Key`` header. The
    overloaded ``None`` is disambiguated as follows:

      - ContextVar is a real int                    → return it (primary path).
      - ContextVar None, no HTTP request / no header → ``None`` (stdio/CLI/local
        admin - unchanged, legitimate unrestricted access).
      - ContextVar None, header present, warm-cache hit with int   → that int
        (tenant-scoped key - CLOSES the bypass).
      - ContextVar None, header present, warm-cache hit with None   → ``None``
        (genuine admin / global key - tenant_id IS NULL in DB; correct).
      - ContextVar None, header present, warm-cache MISS (TTL race) → FAIL-CLOSED:
        resolve AUTHORITATIVELY via ``verify_api_key_full`` (the same DB path the
        middleware uses on cache miss). Only a confirmed NULL tenant returns
        ``None``; a real tenant returns its int; if even the authoritative lookup
        is unavailable/fails we RAISE ``TenantResolutionDenied`` rather than widen
        to unrestricted. The warm cache is populated by AuthMiddleware BEFORE this
        hook fires, so this DB edge is rare - correctness over micro-perf.

    The recovery derives the key material SOLELY from ``get_http_request()`` (the
    request bound to the current ASGI task), so it can never bleed a tenant from
    one concurrent request into another. ContextVar stays PRIMARY (ADR-0029);
    the fallback only fires when it is ``None``.
    """
    value = _tenant_id_var.get()
    if value is not None:
        # ContextVar propagated correctly (stdio, in-process tests, or a
        # transport where on_call_tool shares the tool-body context).
        return value
    # ContextVar is None - could be a genuine admin/global key OR the #248
    # context-boundary loss. Disambiguate from the request's own header.
    return _recover_tenant_id_from_request()


def _recover_tenant_id_from_request() -> int | None:
    """Recover the tenant_id for the current request when the ContextVar is None.

    The #248 fallback for ``_get_tenant_id`` (security: tenant-isolation bypass).
    Mirrors ``_recover_identity_from_header`` in tool_log_middleware.py but with
    an authoritative fail-closed step on cache miss.

    Returns ``None`` ONLY when the absence of a tenant is legitimate:
      - no active HTTP request / no ``X-API-Key`` header (stdio/CLI/local admin), or
      - the key is authoritatively a global/admin key (tenant_id IS NULL in DB,
        confirmed via warm cache OR ``verify_api_key_full``).

    Returns an ``int`` when the request's key is tenant-scoped - this is what
    closes the GUC='*' bypass.

    Raises ``TenantResolutionDenied`` on the dangerous edge: an AUTHENTICATED key
    (``X-API-Key`` present) whose tenant is neither cached nor resolvable via the
    authoritative DB lookup. Failing closed here is mandatory - returning ``None``
    would widen a scoped key to the unrestricted ``'*'`` GUC.

    SECURITY: the key material comes solely from ``get_http_request()`` - the
    request bound to the current ASGI task - so the recovered tenant can never be
    one belonging to a different concurrent request.
    """
    # 1) No HTTP request at all (stdio / CLI / in-process test) → legitimate None.
    try:
        from fastmcp.server.dependencies import get_http_request
        raw_key = get_http_request().headers.get("X-API-Key")
    except Exception:
        return None
    # 2) No X-API-Key header on the request → unauthenticated / local → None.
    if not raw_key:
        return None

    # 3) Authenticated request - consult the warm tenant cache first. The
    #    middleware's AuthMiddleware.dispatch already populated it for THIS key
    #    before the tool hook fired, so a hit is the overwhelmingly common case.
    try:
        from src.mcp.middleware import _cache_get_tenant
        hit, tenant_id = _cache_get_tenant(raw_key)
    except Exception:
        hit, tenant_id = False, None
    if hit:
        # hit + int  → tenant-scoped key (CLOSES the bypass).
        # hit + None → genuine admin / global key (tenant_id IS NULL) → unrestricted.
        return tenant_id

    # 4) FAIL-CLOSED edge: authenticated key but cold tenant cache (TTL race).
    #    Resolve AUTHORITATIVELY via the same DB path the middleware uses on a
    #    cache miss. We must NOT silently widen to None-as-unrestricted here.
    return _authoritative_tenant_id_or_deny(raw_key)


def _authoritative_tenant_id_or_deny(raw_key: str) -> int | None:
    """Resolve tenant_id from the DB for *raw_key*; deny if unresolvable.

    Reuses ``verify_api_key_full`` - the same authoritative lookup
    ``AuthMiddleware`` uses on a cache miss - returning the (key_id, tenant_id,
    user_id, owner_is_admin) tuple. We also warm the in-memory caches on success
    so the next call in this TTL window takes the fast path.

    Returns the tenant_id int (tenant-scoped key) or ``None`` ONLY when the DB
    authoritatively confirms tenant_id IS NULL (genuine admin/global key).

    Raises ``TenantResolutionDenied`` when the key cannot be authoritatively
    resolved - verify returns ``None`` (key vanished / deactivated mid-window),
    an unexpected shape, or the DB is unavailable. Failing closed is mandatory:
    an authenticated key with an unknown tenant must never widen to the
    unrestricted ``'*'`` GUC.
    """
    try:
        from src.db.pg import auth_store
        result = auth_store().verify_api_key_full(raw_key)
    except Exception as exc:
        # DB unavailable / verify raised - cannot confirm admin status, so deny.
        raise TenantResolutionDenied(
            "tenant could not be resolved for an authenticated key "
            "(authoritative lookup unavailable) - denying to preserve tenant isolation"
        ) from exc
    if result is None or not (isinstance(result, tuple) and len(result) == 4):
        # Key not active/valid, or an unexpected return shape - deny.
        raise TenantResolutionDenied(
            "tenant could not be resolved for an authenticated key "
            "(key inactive or lookup returned no row) - denying to preserve tenant isolation"
        )
    key_id, tenant_id, user_id, owner_is_admin = result
    # Read-side escalation guard (ADR-0034, mirrors AuthMiddleware): a user-owned,
    # non-admin key with tenant_id IS NULL is the invalid "unrestricted" state a
    # scoped key must NEVER be in. AuthMiddleware 401s it upstream, but this
    # authoritative path must not diverge - re-applying the guard here means that
    # even on a path that bypassed the middleware we deny instead of widening to
    # the '*' GUC. Raise BEFORE warming caches so the bad state is never cached.
    from src.mcp.middleware import _is_null_tenant_escalation
    if _is_null_tenant_escalation(tenant_id, user_id, owner_is_admin):
        raise TenantResolutionDenied(
            "authenticated key resolved to a non-admin owner with NULL tenant "
            "(escalation state) - denying to preserve tenant isolation"
        )
    # Warm the caches so the rest of this request / TTL window is fast and
    # consistent with the middleware's own population.
    try:
        from src.mcp.middleware import _cache_set, _cache_set_owner, _cache_set_tenant
        _cache_set(raw_key, key_id)
        _cache_set_tenant(raw_key, tenant_id)
        _cache_set_owner(raw_key, user_id, owner_is_admin)
    except Exception:
        pass  # cache warming is best-effort - never block on it
    # tenant_id int → scoped key; None → DB-confirmed admin/global (unrestricted).
    return tenant_id


# ContextVar storage for API key ID - populated by UsageLogMiddleware.
# ContextVar is used instead of threading.local() because asyncio multiplexes
# coroutines in a single thread: a threading.local write in coroutine A is
# shared with coroutine B (same thread), so one request's finally-reset would
# wipe another's value mid-execution → 'default' sentinel crash on
# set_active_version / set_active_profile.  ContextVar gives each coroutine
# its own isolated copy, propagated to worker threads by anyio (if needed).
_api_key_id_var: ContextVar[str] = ContextVar("_api_key_id", default="default")

# ContextVar storage for tenant_id - populated alongside _api_key_id_var
# by UsageLogMiddleware from request.state.tenant_id (ADR-0034 D4.1).
_tenant_id_var: ContextVar[int | None] = ContextVar("_tenant_id", default=None)

# ContextVar storage for the MCP transport session id (#251). Populated by
# UsageLogMiddleware from the ``mcp-session-id`` header before each tool /
# resource call so the per-session version/profile pin (src.mcp.session) is
# keyed by (api_key_id, mcp_session_id) - concurrent Claude Code sessions on
# one API key no longer clobber each other's pins. Defaults to the
# single-session sentinel for stdio / no-request / header-less callers.
_mcp_session_id_var: ContextVar[str] = ContextVar(
    "_mcp_session_id", default=_session._NO_SESSION_SENTINEL
)


# --- WI-4 fail-closed profile filter (ADR-0034 enforcement choke point) ------
def _get_allowed_profiles() -> list[str] | None:
    """Allowed profile names for the current request's tenant (cached 60s).

    ``None`` = admin / legacy global key (tenant_id NULL) → UNRESTRICTED.
    ``[]`` = tenant owns no profiles → deny-all. ``[...]`` = scoped.
    """
    return _session.resolve_allowed_profiles(_get_tenant_id())


def _effective_allowed(profile_name: str | None) -> list[str] | None:
    """SINGLE-VALUE filter param (pgvector ``profile_name = ANY(%s)``, profile
    listing) - the flat union ``own ∪ shared`` with optional explicit narrowing.

    - admin (None), no profile_name → ``None``  (no filter applied)
    - admin (None), explicit profile → ``[profile_name]``
    - tenant, no profile_name        → the usable union list
    - tenant, profile in union       → ``[profile_name]``
    - tenant, profile NOT in union   → ``[]``  (deny)
    """
    # #251: inject the per-session pinned profile when the caller omits one,
    # BEFORE the ADR-0034 narrowing below. Narrowing-only + read-time
    # re-validation: an out-of-scope pin (scoped tenant) returns ``[]`` so the
    # downstream ``ANY('{}')`` matches nothing (fail-closed); admin (None) with
    # the pin narrows to ``[pin]`` as a convenience.
    if profile_name is None:
        profile_name = _resolve_profile(None)
    allowed = _get_allowed_profiles()
    if allowed is None:
        return [profile_name] if profile_name else None
    if profile_name:
        return [profile_name] if profile_name in allowed else []
    return allowed


def _allowed_to_guc(allowed: list[str] | None) -> str:
    """Convert ``_effective_allowed`` output to a GUC string for ``app.allowed_profiles``.

    Pure function (no I/O) - easy to unit-test.

    - ``None``  → ``'*'``    admin sentinel: policy USING clause returns TRUE.
    - ``[]``    → ``''``     tenant with no profiles: deny-all
      (``string_to_array('', ',') = {''}``; ``ANY({''})`` is FALSE for any real profile_name).
    - ``[...]`` → ``'a,b'``  comma-separated; policy matches via string_to_array.
    """
    if allowed is None:
        return "*"
    return ",".join(allowed)


@contextmanager
def _rls_read_tx(conn, allowed: list[str] | None):
    """Set ``app.allowed_profiles`` GUC for the duration of one read transaction.

    Uses ``SET LOCAL`` so the GUC is scoped to the current transaction and
    automatically cleared on COMMIT/ROLLBACK - zero pool-leak risk.

    Two operating modes:
    - Pool mode (``conn.autocommit=True``): temporarily disables autocommit,
      begins a transaction (psycopg2 opens one implicitly on the first execute
      after ``autocommit=False``; there is no explicit ``BEGIN`` statement),
      sets the GUC, yields, then commits.  The ``finally`` block restores
      ``autocommit=True`` even on error.
    - Caller-managed mode (``conn.autocommit=False``): the caller already owns
      the transaction lifecycle; only ``SET LOCAL`` is executed here and the
      caller retains full control over COMMIT/ROLLBACK.  Note: in the current
      test suite the injected connections use ``autocommit=True``, so they
      follow the pool-mode branch above.  This branch is wired for callers that
      manage their own transaction (not currently used in the test suite).

    Armed-but-dormant: while the table owner (``odoo_semantic``) connects, RLS
    is ENABLED but NOT FORCED - PostgreSQL skips all policy evaluation for the
    owner, so this context manager is a no-op in production until the operator
    runs ``ALTER TABLE embeddings FORCE ROW LEVEL SECURITY`` and switches to a
    non-owner read role (ADR-0034 WI-7 ops runbook).
    """
    guc_val = _allowed_to_guc(allowed)
    caller_autocommit = conn.autocommit
    if caller_autocommit:
        # Pool mode: wrap in an explicit transaction so SET LOCAL is scoped.
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL app.allowed_profiles = %s", (guc_val,))
            yield
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True  # restore for pool reuse
    else:
        # Test/injected-conn mode: caller owns the transaction; just set GUC.
        with conn.cursor() as cur:
            cur.execute("SET LOCAL app.allowed_profiles = %s", (guc_val,))
        yield


def _set_iterative_scan(cur) -> None:
    """SET LOCAL hnsw.iterative_scan for the current read transaction (issue #255, ADR-0047).

    Improves HNSW post-filter recall for filtered semantic queries by letting
    HNSW keep scanning until LIMIT is satisfied *after* the post-filter, instead
    of stopping at ef_search candidates.  Only applies when HNSW_ITERATIVE_SCAN is
    non-empty (default 'relaxed_order').

    Must be called inside an open transaction (the _rls_read_tx pool-mode branch
    provides one via SET LOCAL).  Silently ignored on pgvector <0.8 via a guarded
    execute so older local stacks never error.  The constant is the feature-flag:
    set HNSW_ITERATIVE_SCAN='' (empty) to disable without a code change.
    """
    if not HNSW_ITERATIVE_SCAN:
        return
    try:
        cur.execute("SET LOCAL hnsw.iterative_scan = %s", (HNSW_ITERATIVE_SCAN,))
    except Exception:
        pass  # pgvector <0.8 - silently ignored; supported deploys enforce >=0.8


def _scope_pred(alias: str) -> str:
    """Canonical fail-closed tenant choke-point predicate for Neo4j node *alias*.

    Single source of truth for the Cypher fragment (WG-3t - avoids per-site drift)::

        ($own IS NULL OR (size(<alias>.profile) > 0
                          AND all(__p IN <alias>.profile WHERE __p IN $own OR __p IN $shared)))

    The ``size(...) > 0`` guard closes the F-6 vacuous-truth hole: an empty
    ``profile=[]`` array makes ``all(__p IN [] ...)`` evaluate to TRUE in Cypher
    (universal quantification over the empty set), which would fail-OPEN - letting
    legacy / un-reindexed nodes leak to every tenant. With the guard, a node with
    no profiles is denied to all *scoped* tenants (admin, ``$own IS NULL``, still
    sees everything by design).
    """
    return (
        f"($own IS NULL OR (size({alias}.profile) > 0 AND "
        f"all(__p IN {alias}.profile WHERE __p IN $own OR __p IN $shared)))"
    )


def _scope(profile_name: str | None = None) -> dict:
    """Neo4j ARRAY-filter params: ``{'own': [...] | None, 'shared': [...]}`` (ADR-0034).

    The uniform fail-closed Cypher fragment at every user-data site is built by
    :func:`_scope_pred`::

        ($own IS NULL OR (size(<alias>.profile) > 0
                          AND all(__p IN <alias>.profile WHERE __p IN $own OR __p IN $shared)))

    ``own=None`` (admin / no tenant) disables the filter. A node is granted iff it
    has at least one profile AND EVERY profile on it is one of the tenant's OWN
    profiles or a shared/global profile - so another tenant's private node (which
    also carries the shared base in its ``profile[]``) is denied (its foreign private
    profile fails the ``all(...)``), and a same-name cross-tenant collision
    fail-closes (denied to both).

    ``profile_name`` is a NON-ESCALATING narrowing filter (WG-3t T3 - fixes the
    Neo4j/pgvector split-brain). It can only shrink the visible set *within*
    ``own ∪ shared``; it can never widen it:

    - admin (own=None), no profile_name      → ``own=None`` (unrestricted).
    - admin (own=None), explicit profile      → narrow to ``own=[profile]``, keep ``shared``
      (admin convenience; shared/CE base nodes with [own, base] still visible).
    - tenant, no profile_name                 → full ``(own, shared)`` boundary.
    - tenant, profile_name ∈ own∪shared       → narrow own to ``[profile]``, keep ``shared``
      (nodes that carry [own, base] both remain visible - shared is never stripped).
    - tenant, profile_name ∉ own∪shared       → deny-all (``own=[], shared=[]``);
      a tenant cannot borrow another tenant's profile name to escalate.
    """
    # #251: when the caller omits an explicit profile, inject the per-session
    # pinned default (set via set_active_profile) BEFORE the ADR-0034 tenant
    # narrowing below. The injection is NARROWING-ONLY - the existing tenant
    # logic re-validates the pinned profile at read time: an out-of-scope pin
    # (not in own ∪ shared, with a scoped tenant) fail-closes to deny-all, and
    # an admin (own=None) stays unrestricted. The pin can never widen beyond
    # own ∪ shared, nor cross tenants.
    if profile_name is None:
        profile_name = _resolve_profile(None)
    own, shared = _session.resolve_tenant_scope(_get_tenant_id())
    if not profile_name:
        return {"own": own, "shared": shared}
    # Non-escalating narrowing. Admin (own=None) is unrestricted, so any profile is
    # in-scope and we narrow purely as a convenience. A scoped tenant may only narrow
    # within its own∪shared boundary; an out-of-scope profile_name fail-closes.
    # In both admin and tenant cases we KEEP shared so that nodes carrying
    # [profile_name, base_profile] (the normal [own, CE-base] pattern) are not
    # accidentally denied when the caller narrows to a specific own-profile.
    if own is None:
        return {"own": [profile_name], "shared": shared}
    if profile_name in own or profile_name in shared:
        return {"own": [profile_name], "shared": shared}
    return {"own": [], "shared": []}


# find_examples rerank coefficients - extracted so calibration harness can
# monkey-patch them. See _find_examples + tests/test_calibration_eval.py.
_RERANK_LOG_COEFF = 0.02
_RERANK_CHAIN_BOOST = 0.20

# Literal-first floor score for _find_examples rerank (issue #255, M1 fix).
# Literal rows have cosine=None and must sort ABOVE all semantic (cosine-based) hits.
# Real cosine * (1 + 0.02 * log(dependents+1)) stays below ~1.5 in practice;
# 2.0 + epsilon spacing guarantees literal rows always rank first.
# eps gives each literal row a distinct score to preserve SQL ORDER BY order.
_LITERAL_RANK_FLOOR = 2.0
_LITERAL_RANK_EPS = 1e-6


def _get_driver():
    global _driver, _version_checked
    if _driver is not None:  # fast path - no lock overhead on hot calls
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
        # MCP READ driver: deliberately NO notifications_min_severity filter
        # here (unlike the write/indexer drivers in src/indexer/*). Read queries
        # may surface useful INFORMATION-level notifications - e.g. cartesian
        # product / index-miss performance hints - that we want logged. The
        # INFORMATION "already exists" schema-notification noise that motivated
        # the filter only occurs on the write path's CREATE INDEX statements.
        _driver = GraphDatabase.driver(uri, auth=(user, password))

        # Version check: fail-fast if Neo4j < 5.x (unless in CI with pinned image).
        # _version_checked is protected by _init_lock here - no separate flag needed.
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

    Single-attempt with `connect_timeout` (default 5s) - fails fast on an
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
    if _embedder_instance is not None:  # fast path - no lock overhead on hot calls
        return _embedder_instance
    with _init_lock:
        if _embedder_instance is not None:  # re-check after acquiring lock
            return _embedder_instance
        from src import config
        from src.indexer.embedder import make_embedder
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
        # WI-A factory: backend chosen by EMBEDDER_BACKEND (default ollama →
        # Qwen3Embedder). url/model/dim/auth forwarded to the constructor.
        _embedder_instance = make_embedder(
            url=url, model=model, dim=int(dim_str), auth_token=auth_token,
        )
    return _embedder_instance


def _latest_version(session) -> str | None:
    """Return the latest Odoo version present in the index, by NUMERIC compare.

    Filters:
      - excludes 'unknown' and any non-semver-shaped string (must match `\\d+\\.\\d+`)
      - sorts by `toInteger(split(v,'.')[0])` then minor - handles 9.0 < 17.0 correctly
        (lexicographic compare would put '9.0' > '17.0', a Neo4j 5.x gotcha - see
        project CLAUDE.md).
      - scoped to tenant boundary via $allowed (ADR-0034 WI-4): admin gets None →
        unrestricted; tenant gets their allowed list → version only from their data.

    Returns None when no indexed data exists (no hardcoded fallback). Callers
    should surface a clear error instructing the user to run the indexer.
    """
    # #284: bound under the per-query Neo4j timeout via _single_bounded so a
    # tx-timeout surfaces as OrmQueryTimeout (not a raw ClientError). On the
    # implicit-version path this call runs inside _resolve_model's try block
    # (via _resolve_version -> resolve_version_v2 Tier-3); a raw ClientError
    # would ESCAPE that `except OrmQueryTimeout` (ADR-0023 violation). The
    # query touches only Module nodes (no INHERITS traversal) so a timeout is
    # low-probability, but converting it keeps the clean-text contract intact.
    rec = _single_bounded(
        session,
        """
        MATCH (m:Module)
        WHERE ($own IS NULL OR (size(m.profile) > 0
               AND all(__p IN m.profile WHERE __p IN $own OR __p IN $shared)))
          AND m.odoo_version <> 'unknown' AND m.odoo_version =~ '\\d+\\.\\d+'
        WITH DISTINCT m.odoo_version AS v
        RETURN v
        ORDER BY toInteger(split(v, '.')[0]) DESC,
                 toInteger(split(v, '.')[1]) DESC
        LIMIT 1
        """,
        "latest indexed version",
        **_scope(None),
    )
    return rec["v"] if rec else None


def _resolve_version(version_arg: str, session) -> str:
    """Session-aware version resolution - 3-tier order per ADR-0029.

    Resolution order (delegated to session.resolve_version_v2):
      1. Explicit *version_arg* after sentinel normalization (auto/default/
         latest/version/any/"" all treated as sentinel → None).
      2. Per-(api_key, mcp_session) in-memory pin (24h TTL since last set; #251).
      3. Latest indexed version via _latest_version() Neo4j query.

    Raises ValueError when all three tiers fail (empty index + no session
    + no explicit version).

    All 24 existing call sites are unchanged - this function's external
    signature is preserved.
    """
    api_key_id = _get_api_key_id()
    mcp_session_id = _get_mcp_session_id()
    return _session.resolve_version_v2(version_arg, api_key_id, session, mcp_session_id)


def _resolve_profile(profile_arg: str | None) -> str | None:
    """Session-aware profile resolution - proposes the pinned default (#251).

    Peer of :func:`_resolve_version`. Delegates to
    ``session.resolve_profile_v2`` with the per-session pin key so a tool that
    omits ``profile_name`` inherits the profile pinned via ``set_active_profile``
    for THIS MCP session. The resolution performs NO authorization - the
    returned profile is re-validated (narrowing-only, fail-closed) by the
    ADR-0034 choke in :func:`_scope` / :func:`_effective_allowed`.

    ``resolve_profile_v2`` returns ``None`` at its Tier-3 fallback and never
    touches the ``session`` arg, so ``None`` is passed for it.
    """
    return _session.resolve_profile_v2(
        profile_arg, _get_api_key_id(), None, _get_mcp_session_id()
    )


def _resolve_model(
    model_name: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
    from_module: str | None = None,
    *,
    _reraise_timeout: bool = False,
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
    _reraise_timeout:
        When ``True``, an INHERITS-heavy query timeout re-raises ``OrmQueryTimeout``
        instead of being converted to the clean string. The ``odoo://`` model
        resource handler sets this so a *transient* timeout body is never written
        to the resource LRU cache (``get_or_compute`` stores unconditionally - a
        30s blip would otherwise pin the error for the full TTL). The default
        ``False`` keeps the model_inspect tool path returning a clean ``str``
        (it runs under a plain ``@offload`` that does not catch the exception).
    from_module:
        When set, restrict the inheritance-chain layers to rows where the
        declaring module equals this value. Layers from other modules are
        silently filtered out.  ``"<builtin>"`` is never returned regardless
        of this parameter (magic fields live in synthetic space only).
        Default ``None`` preserves the existing behaviour (all modules).
    """
    # #279 follow-up (ADR-0048 / ADR-0023): both queries below are INHERITS-heavy
    # (the ranking query is the #273 same-name-mesh path-explosion target). Each is
    # wrapped by `_data_bounded` so it runs under the per-query Neo4j timeout
    # (neo4j.Query(timeout=NEO4J_QUERY_TIMEOUT_SECONDS)) - the 600s server-side
    # db.transaction.timeout backstop alone let #273 hang for 19-24h. On timeout
    # `_data_bounded` raises OrmQueryTimeout (clean English, no Cypher leaked).
    # `_resolve_model` returns `str`, and its callers run under a plain `@offload`
    # (model_inspect) / the MCP resource handler - NEITHER catches OrmQueryTimeout,
    # so an uncaught raise would surface as a protocol-level isError and not the
    # ADR-0023 raw-text contract. We therefore catch it HERE (approach (a)) and
    # return the clean user_message string. This is preferred over flipping
    # model_inspect to @offload_bounded_nonorm: (1) it covers the resources.py
    # caller too, which the decorator swap would miss, and (2) it does not change
    # the concurrency semantics of the high-traffic model_inspect tool (whose
    # non-summary methods do not run this INHERITS-heavy query at all).
    try:
        with _get_driver().session() as session:
            odoo_version = _resolve_version(odoo_version, session)

            # Ranking tiers - see docs/adr/0013:
            # T1 is_def_rank: m.is_definition flag (post-reindex, authoritative).
            # T2 field_count: Field nodes declared on this model in this module -
            #                 100% accurate signal pre-reindex on real data
            #                 (defining module always has the most fields).
            # T3 dependents : DEPENDS_ON inbound on Module (manifest depends).
            # T4 edition    : community < enterprise < viindoo < oca < custom.
            # T5 mod_name   : alphabetical tiebreak - eliminates arbitrary order.
            layers = _data_bounded(
                session,
                f"""
                MATCH (m:Model {{name: $name, odoo_version: $v}})-[:DEFINED_IN]->(mod:Module)
                WHERE ($own IS NULL OR (size(m.profile) > 0
                       AND all(__p IN m.profile WHERE __p IN $own OR __p IN $shared)))
                  AND ($from_module IS NULL OR m.module = $from_module)
                WITH m, mod,
                     CASE WHEN coalesce(m.is_definition, false) THEN 0 ELSE 1 END AS is_def_rank,
                     COUNT {{
                         (:Field {{model: $name, module: m.module, odoo_version: $v}})
                     }} AS field_count,
                     COUNT {{ ()-[:{REL_DEPENDS_ON}]->(mod) }} AS dependents,
                     {_edition_rank_cypher("mod")},
                     mod.name AS mod_name
                RETURN m.module AS module_name, coalesce(mod.repo_url, mod.repo) AS repo,
                       mod.edition AS edition, mod.license AS license,
                       coalesce(m.is_definition, false) AS is_definition,
                       COUNT {{ (:Field {{model: $name, odoo_version: $v}}) }} AS fields_count,
                       COUNT {{ (:Method {{model: $name, odoo_version: $v}}) }} AS methods_count
                ORDER BY is_def_rank ASC, field_count DESC, dependents DESC,
                         edition_rank ASC, mod_name ASC
                """,
                f"model resolution for '{model_name}' (Odoo {odoo_version})",
                name=model_name, v=odoo_version, **_scope(profile_name),
                from_module=from_module,
            )

            if not layers:
                if from_module:
                    return (
                        f"Model '{model_name}' not found in module '{from_module}'"
                        f" (Odoo {odoo_version})."
                    )
                return f"Model '{model_name}' not found in Odoo {odoo_version}."

            # M2 (#262): the "Extended by" list MUST use the SAME predicate as
            # _list_extenders - `NOT is_definition` - so the summary "... and N more"
            # count and the paginated extenders total are always equal. The previous
            # `layers[1:]` assumed exactly one definition row on top, which:
            #   - under-counts by 1 when the definition node is out of scope (a pure
            #     _inherit model whose top row is itself an extender), and
            #   - over-counts when >1 module carries is_definition=true.
            # `base` (the "Defined in" line) stays as the top-ranked row (ADR-0013);
            # in the rare no-definition-row case it is also a NOT-is_definition row,
            # so it appears in both sections - identical to what _list_extenders
            # would return, which is the parity contract M2 requires.
            base = layers[0]
            extensions = [row for row in layers if not row["is_definition"]]
            # INHERITS-aware counts (#283): the summary must agree with _list_fields /
            # _list_methods totals. The flat per-layer `fields_count` / `methods_count`
            # only counts entities declared directly on this model name; it MISSES
            # fields/methods inherited from a mixin (e.g. `res_ref` on
            # `viin.approval.request` lives under `abstract.approval.request.fields`).
            # Use the same per-hop-dedup traversal + DISTINCT-name count as the list
            # tools so the summary never contradicts the enumeration. from_module
            # scoping is intentionally NOT applied here - the summary count reflects
            # the model as a whole, counting the deduped own+inherited name set.
            try:
                fields_count = _count_fields_with_inherited(
                    model_name, odoo_version, session, profile_name
                )
                methods_count = _count_methods_with_inherited(
                    model_name, odoo_version, session, profile_name
                )
            except OrmQueryTimeout:
                # Dense-graph fallback (#283): a timed-out INHERITED count must not
                # blank the whole summary - degrade to the flat own-model count
                # rather than failing the model overview. This inner timeout is
                # recoverable; the OUTER ranking/parents `_data_bounded` timeout is
                # not, and still returns a clean string via the function-level except
                # below (#279/#284).
                fields_count = base["fields_count"]
                methods_count = base["methods_count"]

            # DISTINCT on p.name only - the same parent (e.g. mail.thread) is reachable
            # via multiple INHERITS edges (one per module that declares _inherit), and
            # each one resolves to a separate (parent_name, module) pair. Without
            # collapsing here the rendered list shows duplicates like
            # "mail.thread, mail.thread, mail.thread, ..." (M5 install audit).
            parents = _data_bounded(
                session,
                f"""
                MATCH (:Model {{name: $name, odoo_version: $v}})-[r:{REL_INHERITS}]->(p:Model)
                WHERE p.name <> $name
                  AND NOT coalesce(r.unresolved, false)
                  AND {_scope_pred("p")}
                RETURN DISTINCT p.name AS pname
                ORDER BY pname
                """,
                f"inheritance parents for '{model_name}' (Odoo {odoo_version})",
                name=model_name, v=odoo_version, **_scope(profile_name),
            )
    except OrmQueryTimeout as exc:
        # Approach (a): _resolve_model returns str, so surface the clean English
        # timeout message (ADR-0023 - no Cypher leaked) rather than letting the
        # exception escape to FastMCP as a protocol-level isError. The 30s driver
        # timeout itself is the load-bearing #279 protection.
        #
        # #284 review: the odoo:// model resource caches this return value via
        # ResourceCache.get_or_compute, which stores UNCONDITIONALLY. Returning
        # the (transient) timeout string there would poison the LRU entry for the
        # full TTL - a 30s blip becomes a 300s stale-error outage on that URI.
        # The resource handler therefore passes `_reraise_timeout=True` so the
        # exception propagates and is rendered uncached; the sibling field/method
        # resolvers already raise their raw ClientError for the same reason.
        if _reraise_timeout:
            # Resource path: re-raise UNCOUNTED here so the resource handler's
            # own `except OrmQueryTimeout` records the metric exactly once for
            # that path (no double-count). #284 / finding-5.
            raise
        # Tool path (model_inspect summary): record the timeout so ops can see
        # dense-mesh timeouts on this non-ORM read. _resolve_model is NOT inside a
        # bounded-offload pool, so this is the only place the tool-path timeout is
        # observable - no double-count (the bounded pools have their own metric).
        _metric_nonorm_query_timeout("model_inspect")
        return exc.user_message

    # ADR-0023 §1.1: header = "{entity} (Odoo {version})", no decoration.
    # from_module filter info goes as a branch line, not appended to header.
    lines = [f"{model_name} (Odoo {odoo_version})"]
    if from_module:
        lines.append(f"├─ filter: from_module={from_module}")
    # WG-5 T1: append edition label after module name for quick CE/EE identification.
    _base_ed = _edition_label(base.get("edition"), base.get("license"))
    lines.append(f"├─ Defined in:     [{base['repo']}] {base['module_name']} ({_base_ed})")

    if parents:
        parents_str = ", ".join(p["pname"] for p in parents)
        lines.append(f"├─ Inherits from:  {parents_str}")

    if extensions:
        lines.append("├─ Extended by:")
        more_hint = (
            f"model_inspect(model='{model_name}', method='extenders',"
            f" odoo_version='{odoo_version}') for full paginated list"
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


def _provenance_token(
    owner_model: str | None,
    model: str,
    edge_kind: str | None,
    via_field: str | None,
) -> str | None:
    """SSOT for the list-row provenance token (ADR-0023 §D, token-additive).

    Returns the trailing token appended to an inherited/delegated field- or
    method-row (e.g. ``inherited from sale.mixin`` or
    ``delegated via partner_id from res.partner (separate table, fields-only)``),
    or ``None`` for an OWN entity (``owner_model`` equals ``model``, or no owner /
    depth-0) so the caller appends nothing - output for own entities stays
    byte-identical to the pre-inherited behaviour.

    Single source for the wording across :func:`_list_fields._fmt_field_row` and
    :func:`_list_methods._fmt_method` (FIX-6, review #283). The two detail
    renderers (:func:`_render_inherited_field` / :func:`_render_inherited_method`)
    use a distinct capitalised ``├─`` branch grammar and are intentionally NOT
    routed through this helper. ``edge_kind == 'delegates'`` only ever occurs on
    the FIELD path - methods are INHERITS-only (GAP-1), so the method caller
    always lands on the ``inherited from`` branch.
    """
    if not owner_model or owner_model == model:
        return None
    if edge_kind == "delegates":
        # GAP-5: `_inherits` delegation gives the child the owner's FIELDS ONLY,
        # stored in the owner's SEPARATE table via the FK - signal it explicitly
        # so an AI client does not mistake it for ordinary in-place inheritance.
        if via_field:
            return (
                f"delegated via {via_field} from {owner_model}"
                " (separate table, fields-only)"
            )
        return f"delegated from {owner_model} (separate table, fields-only)"
    return f"inherited from {owner_model}"


def _render_inherited_field(
    model_name: str, field_name: str, odoo_version: str, inh: dict
) -> str:
    """Render the detail tree for a field resolved via inheritance.

    ``inh`` is the full record from :func:`orm._resolve_field_inherited`
    (``{ttype, compute, stored, required, related, comodel_name,
    effective_readonly, module, repo, owner_model, edge_kind, ...}``). The tree
    mirrors the own-field detail in :func:`_resolve_field`, with an extra
    provenance branch (``Inherited from`` / ``Delegated from``) before the
    ``Declared in`` line. ``Declared in`` stays the REAL declaring module so the
    AI client can locate the source, while the provenance branch names the
    mixin/owner model the field is reached through.
    """
    owner = inh.get("owner_model") or "?"
    lines = [
        f"{model_name}.{field_name} (Odoo {odoo_version})",
        f"├─ Type:     {inh.get('ttype', '?')}",
        f"├─ Computed: {'Yes' if inh.get('compute') else 'No'}"
        + (f" ({inh['compute']})" if inh.get('compute') else ""),
        f"├─ Stored:   {'Yes' if inh.get('stored', True) else 'No'}",
        f"├─ Required: {'Yes' if inh.get('required', False) else 'No'}",
        f"├─ Related:  {inh.get('related') or '-'}",
    ]
    if inh.get("comodel_name"):
        lines.append(f"├─ Comodel:  {inh['comodel_name']}")
    # A2-followup: label + help parity with own-field detail (V3 fix - ADR-0023).
    # Populated after reindex; absent on pre-reindex graphs - omit gracefully.
    if inh.get("string"):
        lines.append(f"├─ Label:    {inh['string']}")
    if inh.get("help"):
        lines.append(f"├─ Help:     {inh['help']}")
    _eff_ro = inh.get("effective_readonly")
    if _eff_ro is not None:
        lines.append(f"├─ Readonly: {'Yes' if _eff_ro else 'No'}")
        lines.append(
            "│   └─ note: Python definition only; view-level conditional "
            "readonly/invisible/required is in the view detail (Conditional visibility)"
        )
    # Provenance branch - distinguish INHERITS mixin vs _inherits delegation.
    # GAP-5: delegation gives the child the owner's FIELDS ONLY, stored in the
    # owner's SEPARATE table via the FK - signal that explicitly so the AI client
    # does not mistake it for ordinary in-place inheritance.
    if inh.get("edge_kind") == "delegates":
        via = inh.get("via_field")
        if via:
            lines.append(
                f"├─ Delegated via {via} from: {owner}"
                " (separate table, fields-only)"
            )
        else:
            lines.append(
                f"├─ Delegated from: {owner} (separate table, fields-only)"
            )
    else:
        lines.append(f"├─ Inherited from: {owner}")
    # "Declared in" stays the real declaring module of the field on the owner.
    lines.append("├─ Declared in:")
    repo_str = f"[{inh['repo']}] " if inh.get("repo") else ""
    lines.append(f"│   └─ {repo_str}{inh.get('module') or '?'}")
    # FIX-3 (review #283): the field NODE lives on `owner` (the mixin), not the
    # child `model_name`. impact_analysis flat-matches the field on its declaring
    # model, so a hint keyed by `{child}.{field}` returns an EMPTY blast radius
    # and the AI client wrongly concludes "no impact". Key the impact_analysis +
    # find_examples hints by `owner` (where the field is actually declared) so
    # they resolve. The header above still names the child (that is what the user
    # asked about), but the drill-downs point at the real owner.
    lines.append(format_next_step([
        f"find_examples(query='{owner}.{field_name} usage'"
        f", odoo_version='{odoo_version}') for real-world patterns",
        f"impact_analysis(entity_type='field'"
        f", entity_name='{owner}.{field_name}'"
        f", odoo_version='{odoo_version}') for blast radius",
    ]))
    return "\n".join(lines)


def _resolve_field(
    model_name: str,
    field_name: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
    from_module: str | None = None,
    *,
    _reraise_timeout: bool = False,
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
    _reraise_timeout:
        When ``True``, a per-query timeout re-raises ``OrmQueryTimeout`` instead
        of being converted to a clean string. The ``odoo://`` field resource
        handler sets this so a *transient* timeout body is never written to the
        resource LRU cache (mirrors ``_resolve_model``). The default ``False``
        keeps the model_inspect / entity_lookup tool path returning a clean
        ``str`` (those handlers run under ``@offload_neo4j``, which catches the
        raised OrmQueryTimeout, records the metric, and returns the message).
    """
    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        # 5-tier ranking via m_node proxy - see docs/adr/0013.
        # Routed through `_data_bounded` so a tx-timeout on a dense field ranking
        # becomes OrmQueryTimeout (clean English, no Cypher leaked). The PRIMARY
        # query is intentionally NOT caught here - it propagates so the owning
        # @offload_neo4j handler (model_inspect / entity_lookup) records the
        # metric + returns the clean string (tool path), or the field resource
        # handler records + returns it UNCACHED (resource path). The only inner
        # catch is the inherited-fallback below, which honours _reraise_timeout.
        records = _data_bounded(
            session,
            f"""
            MATCH (f:Field {{name: $fn, model: $mn, odoo_version: $v}})
            WHERE ($own IS NULL OR (size(f.profile) > 0
                   AND all(__p IN f.profile WHERE __p IN $own OR __p IN $shared)))
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
            RETURN f, f.module AS module_name, coalesce(mod.repo_url, mod.repo) AS repo
            ORDER BY is_def_rank ASC, field_count DESC, dependents DESC,
                     edition_rank ASC, mod_name ASC
            """,
            f"field detail for '{model_name}.{field_name}' (Odoo {odoo_version})",
            fn=field_name, mn=model_name, v=odoo_version, **_scope(profile_name),
            from_module=from_module,
        )

    # D2: If not found in graph, check whether it's a magic field.
    # Magic fields are synthetic - not in Neo4j - so we build a synthetic record.
    if not records:
        if from_module is None and field_name in MAGIC_FIELDS:
            ttype, _comodel = MAGIC_FIELDS[field_name]
            lines = [
                f"{model_name}.{field_name} (Odoo {odoo_version})",
                f"├─ Type:     {ttype}",
                "├─ Computed: No",
                "├─ Stored:   Yes",
                "├─ Required: No",
                "├─ Related:  -",
                # WI-1 (#238): magic fields are ORM-managed - never writable.
                "├─ Readonly: Yes",
                "├─ Declared in:",
                "│   └─ <builtin>  [ORM magic field - injected at runtime, not in source]",
            ]
            lines.append(format_next_step([
                f"find_examples(query='{model_name}.{field_name} usage'"
                f", odoo_version='{odoo_version}') for real-world patterns",
                f"impact_analysis(entity_type='field'"
                f", entity_name='{model_name}.{field_name}'"
                f", odoo_version='{odoo_version}') for blast radius",
            ]))
            return "\n".join(lines)
        # Inherited fallback: the flat exact-match on the child model MISSED and
        # it is not a magic field. Walk INHERITS|DELEGATES_TO (depth 1-3) to find
        # the field on a mixin (e.g. `res_ref` declared on a mixin model, not on
        # `viin.approval.request` itself). Keeps the exact-first fast path -
        # native fields never pay the BFS cost.
        #
        # from_module semantics (V2 fix): when from_module is set we still run the
        # inherited fallback - the field may be declared on a mixin model that
        # BELONGS to from_module (e.g. `abstract.approval.request.fields` lives in
        # module `viin_approval`). After the BFS we post-filter: only surface the
        # inherited hit if its declaring module matches from_module. If the module
        # doesn't match we keep the "not found" path - the user asked specifically
        # for that module and the field isn't there.
        # FIX-1 (review #283): _resolve_field_inherited is bounded + tx-timeout-
        # mapped. The owning tool handlers now wrap this in @offload_neo4j (PR-1),
        # which catches OrmQueryTimeout at the boundary; the tool-path catch here
        # still returns the clean ADR-0023 string directly (and the resource path
        # re-raises so the transient body is never cached - see below).
        try:
            with _get_driver().session() as session:
                inh = _resolve_field_inherited(
                    model_name, field_name, odoo_version, session, profile_name
                )
        except OrmQueryTimeout as exc:
            # Resource path (_reraise_timeout=True): re-raise so the transient
            # body is never cached (the field resource handler records the metric
            # once and returns the message uncached) - and so the resolver never
            # double-counts the resource-path timeout.
            if _reraise_timeout:
                raise
            # Tool path: @offload_neo4j only counts a RAISED OrmQueryTimeout, so
            # this inherited-fallback timeout (returned as a clean string) must be
            # counted here for parity with _resolve_model (PR-3 M1, ADR-0050).
            _metric_nonorm_query_timeout("model_inspect")
            return exc.user_message
        if inh is not None:
            if from_module is None or inh.get("module") == from_module:
                return _render_inherited_field(model_name, field_name, odoo_version, inh)
        # Freshness hint (#341): probe whether the model is indexed to give a
        # more actionable not-found message.
        _note = _not_found_freshness_note(
            model_name, odoo_version, profile_name, kind="field"
        )
        return (
            f"Field '{field_name}' not found on model"
            f" '{model_name}' in Odoo {odoo_version}.\n{_note}"
        )

    base_f = records[0]["f"]
    lines = [
        f"{model_name}.{field_name} (Odoo {odoo_version})",
        f"├─ Type:     {base_f.get('ttype', '?')}",
        f"├─ Computed: {'Yes' if base_f.get('compute') else 'No'}"
        + (f" ({base_f['compute']})" if base_f.get('compute') else ""),
        f"├─ Stored:   {'Yes' if base_f.get('stored', True) else 'No'}",
        f"├─ Required: {'Yes' if base_f.get('required', False) else 'No'}",
        f"├─ Related:  {base_f.get('related') or '-'}",
    ]
    # B1: render comodel_name for relational fields (only when non-null).
    if base_f.get("comodel_name"):
        lines.append(f"├─ Comodel:  {base_f['comodel_name']}")
    # A2-followup: field label + help text (intent), rendered when present
    # (populated after reindex; absent on pre-reindex graphs).
    if base_f.get("string"):
        lines.append(f"├─ Label:    {base_f['string']}")
    if base_f.get("help"):
        lines.append(f"├─ Help:     {base_f['help']}")
    # WI-1 (#238): writability signal. Graceful degradation - pre-reindex
    # graphs lack effective_readonly (None); omit the line rather than print a
    # misleading "Readonly: No". Only render once the field has been reindexed.
    _eff_ro = base_f.get("effective_readonly")
    if _eff_ro is not None:
        lines.append(f"├─ Readonly: {'Yes' if _eff_ro else 'No'}")
        lines.append(
            "│   └─ note: Python definition only; view-level conditional "
            "readonly/invisible/required is in the view detail (Conditional visibility)"
        )
    lines.append("├─ Declared in:")
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


def _method_override_chain(
    session, model: str, method: str, odoo_version: str,
    profile_name: str | None = None,
) -> list[dict]:
    """Ranked override-chain records for ``method`` declared on ``model``.

    The 5-tier ADR-0013 ranking (is_definition → field_count → dependents →
    edition → module name) shared by :func:`_resolve_method` (own-method detail)
    and the GAP-3 inherited-method detail (where ``model`` is the OWNER model).
    Each record is ``{mth, module_name, repo}``. SSOT for the ranking so the two
    call-sites never drift.

    Bounded by :func:`_bounded` and tx-timeout-mapped to :class:`OrmQueryTimeout`
    (FIX-1, review #283): this query carries a ``COUNT {{ ... INHERITS-adjacent
    DEPENDS_ON }}`` subquery and is called up to 2× per inherited-method resolve
    (own-method detail + the GAP-3 owner-model chain). On a dense graph it could
    hang exactly like the unbounded-traversal class #273/#276 closed; the bound +
    timeout mapping make every INHERITS/COUNT-heavy read in this wave bounded.
    """
    try:
        return session.run(
            _bounded(f"""
            MATCH (mth:Method {{name: $mn, model: $model, odoo_version: $v}})
            WHERE ($own IS NULL OR (size(mth.profile) > 0
                   AND all(__p IN mth.profile WHERE __p IN $own OR __p IN $shared)))
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
            RETURN mth, mth.module AS module_name, coalesce(mod.repo_url, mod.repo) AS repo
            ORDER BY is_def_rank ASC, field_count DESC, dependents DESC,
                     edition_rank ASC, mod_name ASC
        """),
            mn=method, model=model, v=odoo_version, **_scope(profile_name),
        ).data()
    except ClientError as exc:
        if _is_tx_timeout(exc):
            raise OrmQueryTimeout(
                f"Query timed out after {NEO4J_QUERY_TIMEOUT_SECONDS}s while "
                f"resolving the override chain of '{model}.{method}' (Odoo "
                f"{odoo_version}). The inheritance graph may be unusually dense "
                f"- try a more specific model or retry later."
            ) from exc
        raise


def _render_inherited_method(
    model_name: str, method_name: str, odoo_version: str, inh: dict,
    owner_chain: list[dict] | None = None,
) -> str:
    """Render the detail tree for a method resolved via inheritance.

    ``inh`` is the full record from :func:`orm._resolve_method_inherited`
    (``{name, convention_kind, module, repo, signature, docstring, decorators,
    has_super_call, depends, owner_model, edge_kind, ...}``). Mirrors the
    own-method detail in :func:`_resolve_method` with an ``Inherited from``
    provenance branch.

    Methods are inherited via INHERITS only (Python MRO) - ``_inherits``
    delegation NEVER carries methods (GAP-1) - so the provenance is always
    "Inherited from", never "Delegated".

    ``owner_chain`` is the REAL multi-module override chain on the OWNER model
    (GAP-3): the records (``{mth, module_name, repo}``) of every module that
    declares ``method_name`` on ``owner_model``, in the same MRO/last-loaded
    ranked order as :func:`_resolve_method`. When provided (and non-empty) it
    renders the full chain with the true count; when ``None``/empty (defensive)
    it falls back to the single owner entry from ``inh``.
    """
    owner = inh.get("owner_model") or "?"
    lines = [f"{model_name}.{method_name}() (Odoo {odoo_version})"]
    if inh.get("signature"):
        lines.append(f"├─ Signature:   ({inh['signature']})")
    if inh.get("convention_kind"):
        lines.append(f"├─ Convention:  {inh['convention_kind']}")
    if inh.get("docstring"):
        first_line = inh["docstring"].strip().splitlines()[0][:120]
        lines.append(f"├─ Docstring:   {first_line}")
    lines.append(f"├─ Inherited from: {owner}")

    def _fmt_owner_override(r: dict) -> str:
        mth = r["mth"]
        super_info = "✓ calls super()" if mth.get("has_super_call") else "✗ no super()"
        decs = ", ".join(mth.get("decorators") or []) or "-"
        repo_str = f"[{r['repo']}] " if r.get("repo") else ""
        return f"{repo_str}{r['module_name']} - {super_info} - decorators: {decs}"

    if owner_chain:
        # GAP-3: render the REAL override chain on the owner model - every module
        # that declares this method on its owner, MRO/last-loaded ranked, capped
        # with ADR-0023 §3 disclosure. No more misleading hardcoded "(1)".
        chain_total = len(owner_chain)
        lines.append(f"├─ Override chain ({chain_total}):")
        capped_chain = _render_capped(
            owner_chain[:LIST_PREVIEW_MAX_ITEMS],
            _fmt_owner_override,
            cap=LIST_PREVIEW_MAX_ITEMS,
            total=chain_total,
            more_hint=(
                f"find_override_point(model='{owner}', method='{method_name}'"
                f", odoo_version='{odoo_version}') for full override chain"
            ),
        )
        lines.extend(render_list_block(capped_chain, prefix="│   "))
    else:
        # Defensive fallback: no owner chain supplied - render the single owner
        # entry from `inh` (pre-GAP-3 behaviour, but no longer the normal path).
        lines.append("├─ Override chain (1):")
        super_info = "✓ calls super()" if inh.get("has_super_call") else "✗ no super()"
        decs = ", ".join(inh.get("decorators") or []) or "-"
        repo_str = f"[{inh['repo']}] " if inh.get("repo") else ""
        lines.append(
            f"│   └─ {repo_str}{inh.get('module') or '?'} - {super_info}"
            f" - decorators: {decs}"
        )
    # FIX-3 (review #283, symmetric to _render_inherited_field): the method NODE
    # and its whole override chain live on `owner` (the mixin), not the child
    # `model_name`. find_override_point + impact_analysis flat-match on the
    # declaring model, so hints keyed by `{child}.{method}` return EMPTY - wrongly
    # signalling "no override point / no impact". Key both drill-downs by `owner`
    # so they resolve. The header still names the child (what the user asked).
    lines.append(format_next_step([
        f"find_override_point(model='{owner}', method='{method_name}'"
        f", odoo_version='{odoo_version}') for safe hook spot",
        f"impact_analysis(entity_type='method'"
        f", entity_name='{owner}.{method_name}'"
        f", odoo_version='{odoo_version}') for blast radius",
    ]))
    return "\n".join(lines)


def _resolve_method(
    model_name: str,
    method_name: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
    *,
    _reraise_timeout: bool = False,
) -> str:
    # FIX-1 (review #283): _method_override_chain + _resolve_method_inherited are
    # bounded and tx-timeout-mapped to OrmQueryTimeout.
    #
    # _reraise_timeout: the ``odoo://`` method resource handler sets this so a
    # transient timeout body is never written to the resource LRU (mirrors
    # _resolve_model / _resolve_field). The default False keeps the model_inspect
    # / entity_lookup tool path returning a clean ADR-0023 string directly. (The
    # tool-path method-detail timeout is therefore not yet counted in the metric -
    # the deferred M2 gap, PR-3 / issue #287 - matching _resolve_field's M1.)
    _method_not_found = False
    try:
        with _get_driver().session() as session:
            odoo_version = _resolve_version(odoo_version, session)

            # 5-tier ranking via m_node proxy - see docs/adr/0013
            records = _method_override_chain(
                session, model_name, method_name, odoo_version, profile_name
            )

        if not records:
            # Inherited fallback (symmetric to _resolve_field): the flat exact-match
            # on the child model MISSED. Walk INHERITS (depth 1-3) to find the method
            # on a mixin (e.g. `_compute_res_ref` declared on a mixin model, not on
            # `viin.approval.request` itself). Methods are inherited via INHERITS only
            # - `_inherits` delegation never carries methods (GAP-1). Exact-first fast
            # path is preserved - own methods never pay the BFS cost.
            with _get_driver().session() as session:
                inh = _resolve_method_inherited(
                    model_name, method_name, odoo_version, session, profile_name
                )
                owner_chain: list[dict] = []
                if inh is not None and inh.get("owner_model"):
                    # GAP-3: fetch the REAL multi-module override chain on the OWNER
                    # model (same ranking as own methods) so the inherited detail
                    # shows every declaring module, not a misleading hardcoded "(1)".
                    owner_chain = _method_override_chain(
                        session, inh["owner_model"], method_name,
                        odoo_version, profile_name,
                    )
            if inh is not None:
                return _render_inherited_method(
                    model_name, method_name, odoo_version, inh, owner_chain
                )
            # Probe runs outside the try/except - mirrors _resolve_field placement.
            _method_not_found = True
    except OrmQueryTimeout as exc:
        # Consistent with _resolve_field / _resolve_model. Resource path
        # (_reraise_timeout=True): re-raise so the transient body is never cached
        # (propagates out of get_or_compute before the put; the method resource
        # handler records the metric once + returns it uncached) - the resolver
        # never double-counts the resource-path timeout.
        if _reraise_timeout:
            raise
        # Tool path: @offload_neo4j only counts a RAISED OrmQueryTimeout, so this
        # method-detail timeout (returned as a clean string) is counted here for
        # parity with _resolve_field's M1 (PR-3 M2, ADR-0050).
        _metric_nonorm_query_timeout("model_inspect")
        return exc.user_message

    # Freshness hint (#341) - outside try/except to mirror _resolve_field placement.
    if _method_not_found:
        _note = _not_found_freshness_note(
            model_name, odoo_version, profile_name, kind="method"
        )
        return (
            f"Method '{method_name}' not found on model"
            f" '{model_name}' in Odoo {odoo_version}.\n{_note}"
        )

    base_mth = records[0]["mth"]
    lines = [
        f"{model_name}.{method_name}() (Odoo {odoo_version})",
    ]
    # B1: render signature and convention_kind from the authoritative (first-ranked) entry.
    if base_mth.get("signature"):
        lines.append(f"├─ Signature:   ({base_mth['signature']})")
    if base_mth.get("convention_kind"):
        lines.append(f"├─ Convention:  {base_mth['convention_kind']}")
    # B2: render docstring first line (A2a - populated after reindex; absent pre-reindex).
    if base_mth.get("docstring"):
        first_line = base_mth["docstring"].strip().splitlines()[0][:120]
        lines.append(f"├─ Docstring:   {first_line}")
    chain_total = len(records)
    lines.append(f"├─ Override chain ({chain_total}):")

    def _fmt_override(r: dict) -> str:
        mth = r["mth"]
        super_info = "✓ calls super()" if mth.get("has_super_call") else "✗ no super()"
        decs = ", ".join(mth.get("decorators") or []) or "-"
        repo_str = f"[{r['repo']}] " if r.get("repo") else ""
        return f"{repo_str}{r['module_name']} - {super_info} - decorators: {decs}"

    # G4: cap override chain at LIST_PREVIEW_MAX_ITEMS with disclosure (ADR-0023 §3)
    capped_chain = _render_capped(
        records[:LIST_PREVIEW_MAX_ITEMS],
        _fmt_override,
        cap=LIST_PREVIEW_MAX_ITEMS,
        total=chain_total,
        more_hint=(
            f"find_override_point(model='{model_name}', method='{method_name}'"
            f", odoo_version='{odoo_version}') for full override chain"
        ),
    )
    # ADR-0023 §1.2: render via the shared helper so the LAST row - including a
    # "... and N more" disclosure row - always gets the └─ connector. Parent
    # header "Override chain (...)" was appended as a non-last child (├─), so
    # the vertical line must continue: prefix "│   ".
    lines.extend(render_list_block(capped_chain, prefix="│   "))
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
    *,
    _reraise_timeout: bool = False,
) -> str:
    # The three view queries are routed through `_single_bounded`/`_data_bounded`
    # so a tx-timeout becomes OrmQueryTimeout (clean English, no Cypher leaked).
    # No internal catch: the raise propagates to the owning entity_lookup handler
    # (@offload_neo4j) / view resource handler. `_reraise_timeout` is parity-only.
    _ = _reraise_timeout  # parity-only; the timeout always propagates here.
    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        view_rec = _single_bounded(
            session,
            """
            MATCH (v:View {xmlid: $xmlid, odoo_version: $ver})
            WHERE ($own IS NULL OR (size(v.profile) > 0
                   AND all(__p IN v.profile WHERE __p IN $own OR __p IN $shared)))
            AND coalesce(v.unresolved, false) = false
            AND v.module <> '__unresolved__'
            OPTIONAL MATCH (v)-[:DEFINED_IN]->(mod:Module)
            RETURN v, mod.name AS module_name, coalesce(mod.repo_url, mod.repo) AS repo
            """,
            f"view detail for '{xmlid}' (Odoo {odoo_version})",
            xmlid=xmlid, ver=odoo_version, **_scope(profile_name),
        )

        if not view_rec:
            return f"View '{xmlid}' not found in Odoo {odoo_version}."

        # WG-3t WRONG-TARGET fix: this returns parent.xmlid, so the tenant
        # predicate must gate the RETURNED node (parent), not just the child v.
        # Filtering only v would leak a foreign tenant's parent view xmlid.
        parent_rec = _single_bounded(
            session,
            f"""
            MATCH (v:View {{xmlid: $xmlid, odoo_version: $ver}})
                  -[r:{REL_INHERITS_VIEW}]->(parent:View {{odoo_version: $ver}})
            WHERE NOT coalesce(r.unresolved, false)
            AND {_scope_pred("v")}
            AND {_scope_pred("parent")}
            RETURN parent.xmlid AS parent_xmlid
            """,
            f"parent view for '{xmlid}' (Odoo {odoo_version})",
            xmlid=xmlid, ver=odoo_version, **_scope(profile_name),
        )

        extensions = _data_bounded(
            session,
            f"""
            MATCH (ext:View {{odoo_version: $ver}})-[:{REL_INHERITS_VIEW}]->
                  (v:View {{xmlid: $xmlid, odoo_version: $ver}})
            WHERE NOT coalesce(ext.unresolved, false)
            AND ($own IS NULL OR (size(ext.profile) > 0
                 AND all(__p IN ext.profile WHERE __p IN $own OR __p IN $shared)))
            OPTIONAL MATCH (ext)-[:DEFINED_IN]->(mod:Module)
            RETURN ext.xmlid AS ext_xmlid,
                   ext.xpaths_exprs AS xpaths_exprs,
                   ext.xpaths_positions AS xpaths_positions,
                   mod.name AS module_name, coalesce(mod.repo_url, mod.repo) AS repo
            """,
            f"view extension chain for '{xmlid}' (Odoo {odoo_version})",
            xmlid=xmlid, ver=odoo_version, **_scope(profile_name),
        )

    v_props = view_rec["v"]
    repo_str = f"[{view_rec['repo']}] " if view_rec.get("repo") else ""
    mode_label = " (extension)" if v_props.get("mode") == "extension" else ""

    # Build the list of branch (kind, payload) tuples, then render with the
    # correct connector based on whether each branch is the last one.
    # ADR-0023 §1.6: empty Extended by is silently skipped (no "No extensions").
    branches: list[tuple[str, object]] = []
    # B1: render the human label (v.name) when it is non-empty.
    if v_props.get("name"):
        branches.append(("string", v_props["name"]))
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
    _cond_raw = v_props.get("conditions")  # GAP-1 visibility blob (see render below)
    if _cond_raw and _cond_raw not in ("[]", ""):
        branches.append(("conditions", _cond_raw))
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
        if kind == "string":
            lines.append(f"{connector} String: {payload}")
        elif kind == "type":
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
        elif kind == "conditions":
            from src.mcp.tree_builder import format_view_conditions
            lines.extend(format_view_conditions(payload, connector, sub_indent))
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
            # present, has no xpath subtree - handle it separately.
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


# ---------------------------------------------------------------------------
# Wave 1 module-overview + entity-listing helpers moved out of this hub
# (Phase 7 / A1):
#   _describe_module / _module_dep_closure -> src/mcp/describe.py
#   _list_fields / _list_methods / _list_extenders / _list_views_core /
#   _list_views / _list_views_by_module / _list_owl_components /
#   _list_qweb_templates / _list_js_patches (+ _JS_ERA_MAP) -> src/mcp/listings.py
# These are NOT tool modules (no @mcp.tool); server.py imports them near the
# end of this file and re-exports every helper so src.mcp.server._describe_module
# / _list_* keep working for tests + the inspect.py / resources.py call sites
# (which reach them via srv. at call time).  The moved bodies read this hub
# through their own end-of-module `_srv` bind at call time, so
# monkeypatch.setattr(srv, ...) keeps working.
# ---------------------------------------------------------------------------


# Inspect tools (describe_module / model_inspect / module_inspect /
# entity_lookup / profile_inspect) moved to src/mcp/tools/inspect_tools.py
# and the Wave-E session tools (set_active_version / set_active_profile /
# list_available_versions / list_available_profiles) moved to
# src/mcp/tools/session_tools.py (Phase 3).  They are registered via the
# @mcp.tool import-time side effect when server.py imports those modules at
# the end of this file; the public tool symbols are re-exported there too so
# src.mcp.server.<tool> keeps working for tests + external callers.  The
# discriminator impls (_model_inspect / _module_inspect / _entity_lookup /
# _profile_inspect) live in src/mcp/inspect.py and the session-persistence
# helpers in src/mcp/session.py; _describe_module stays in this hub (inspect.py
# uses it too) and the inspect/session wrappers reach the hub through `srv.`
# at call time, so monkeypatch.setattr(srv, ...) keeps working.
# ---------------------------------------------------------------------------
# M10A Stylesheet tools (resolve_stylesheet / find_style_override) and their
# impl helpers (_resolve_stylesheet / _literal_style_lookup /
# _find_style_override) moved to src/mcp/tools/stylesheet.py (Phase 2).
# They are registered via the @mcp.tool import-time side effect when server.py
# imports that module at the end of this file; the public tool symbols plus the
# _resolve_stylesheet / _literal_style_lookup / _find_style_override impls are
# re-exported there so that src.mcp.server._resolve_stylesheet /
# _find_style_override keep working for tests (and monkeypatch.setattr(srv, ...)
# still takes effect, since the async wrapper resolves those names through `srv.`
# at call time), and so _find_examples (still in this hub) can keep calling
# _literal_style_lookup by bare name.
# ---------------------------------------------------------------------------
# ORM-validation tool wrappers (resolve_orm_chain / validate_domain /
# validate_depends / validate_relation) moved to src/mcp/tools/orm_tools.py
# (Phase 1 canary).  They are registered via the @mcp.tool import-time side
# effect when server.py imports that module at the end of this file; the
# public symbols are re-exported there too.  The impl functions
# (_resolve_orm_chain / _validate_domain / _validate_depends /
# _validate_relation) are still imported from src.mcp.orm above, so
# `src.mcp.server._validate_*` keeps working for tests.


def _mcp_host() -> str:
    from src import config
    return config.get("server", "host", fallback="127.0.0.1")


def _mcp_port() -> int:
    from src import config
    return int(config.get("server", "port", fallback="8002"))


# Health endpoint - registered as custom route on MCP app
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request):
    from src.mcp.health import health_handler
    return await health_handler(request)


# Readiness endpoint (WI-D) - cached DB-count readiness probe. Distinct from
# /health liveness: /ready reports whether the index is populated and both DBs
# are reachable, reading from the shared TTL cache so it never scans on the hot
# path. Registered as an HTTP custom route (NOT an MCP tool - tool count is 31 after WI-4).
@mcp.custom_route("/ready", methods=["GET"])
async def ready_check(request: Request):
    from src.mcp.health import ready_handler
    return await ready_handler(request)


# Prometheus metrics endpoint - no auth (mirroring /health bypass in middleware.py).
# Cross-process caveat: this endpoint only reflects metrics from the MCP server
# process (:8002).  Batch-indexer embed calls run in a separate process and are
# NOT visible here.  See src/metrics.py for full caveat.
@mcp.custom_route("/metrics", methods=["GET"])
async def metrics_endpoint(request: Request):
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    output = generate_latest()
    from starlette.responses import Response as _Response

    return _Response(content=output, media_type=CONTENT_TYPE_LATEST)


def _resolve_session_idle_timeout() -> float:
    """Resolve SESSION_IDLE_TIMEOUT (seconds) with a fail-safe value guard (#279).

    Reuses ``_resolve_orm_float`` for the post-dotenv + ValueError-fallback
    semantics (a bad ``SESSION_IDLE_TIMEOUT=abc`` falls back to the default
    instead of crashing startup with a raw ``float()`` ValueError).

    The MCP SDK's ``StreamableHTTPSessionManager`` raises ``ValueError`` for any
    ``session_idle_timeout <= 0`` (it has no "0 = disable" affordance - that is
    expressed as ``None``, which Option B never passes). A misconfigured ``<= 0``
    would therefore crash startup AND, if it didn't, silently disable reaping -
    re-opening the #279 leak. We are NOT offering an intentional opt-out here, so
    clamp any ``<= 0`` back to the 3600s default and log a warning.

    ``_resolve_orm_float`` parses ``SESSION_IDLE_TIMEOUT=nan`` / ``=inf`` to a
    float without raising (Python ``float()`` accepts both), and ``nan <= 0`` is
    ``False`` - so a bare ``<= 0`` guard would let a non-finite value through to
    the SDK. ``nan`` yields a deadline that never compares true, ``inf`` a
    never-expiring one: either silently disables reaping and re-opens #279. We
    therefore reject any non-finite value the same way as ``<= 0``.
    """
    resolved = _resolve_orm_float("SESSION_IDLE_TIMEOUT", 3600.0)
    if not math.isfinite(resolved) or resolved <= 0:
        logging.getLogger(__name__).warning(
            "SESSION_IDLE_TIMEOUT=%s is non-finite or <= 0 - that would disable "
            "streamable-http session reaping (re-opening the #279 leak) and is "
            "rejected by the MCP SDK. Falling back to the 3600s (1h) default.",
            resolved,
        )
        return 3600.0
    return resolved


def _build_streamable_http_app(*, idle_timeout: float, middleware, mcp_server=None):
    """Build the Option B streamable-http app core (#279 item 1, ADR-0049).

    Single source of truth for the manual ``create_streamable_http_app()``
    reproduction - both ``main()`` and ``tests/test_session_idle_timeout.py``
    call this so the construction can never drift out of lockstep (FIX 3).

    Returns ``(app, session_manager)``. The caller (``main()``) is responsible
    for the steps that are NOT part of the Option B core: wrapping the router
    lifespan with ``_lifespan_with_pg``, mounting the feedback sub-app, and
    running uvicorn.

    FastMCP's ``mcp.http_app()`` / ``create_streamable_http_app()`` still do NOT
    forward ``session_idle_timeout`` to ``StreamableHTTPSessionManager`` (verified
    on fastmcp 3.4.2 - its ``http_app()`` signature carries no such kwarg), so we
    reproduce that factory here and add the one kwarg. The MCP SDK's manager DOES
    accept it.

    FRAGILE - depends on FastMCP private internals (``mcp._mcp_server``,
    ``mcp._lifespan_manager()``, ``mcp._get_additional_http_routes()``) plus the
    public-but-undocumented ``StreamableHTTPASGIApp`` / ``create_base_app``. The
    smoke test guards drift.

    fastmcp v3 removed ``mcp._deprecated_settings``; its ``http_app()`` now reads
    ``json_response`` / ``stateless_http`` / ``debug`` from the module-level
    ``fastmcp.settings`` singleton (which is also where ``FASTMCP_JSON_RESPONSE`` /
    ``FASTMCP_STATELESS_HTTP`` env vars are parsed in), so we read from the same
    source to preserve FIX 2 / FIX C behaviour exactly. See ADR-0049 addendum.
    """
    from contextlib import asynccontextmanager as _asynccontextmanager

    import fastmcp as _fastmcp
    from fastmcp.server.http import (
        StreamableHTTPASGIApp as _StreamableHTTPASGIApp,
    )
    from fastmcp.server.http import (
        create_base_app as _create_base_app,
    )
    from mcp.server.streamable_http_manager import (
        StreamableHTTPSessionManager as _StreamableHTTPSessionManager,
    )
    from starlette.routing import Route as _Route

    _mcp = mcp_server if mcp_server is not None else mcp

    # FIX 2: forward json_response / stateless from FastMCP's settings so an
    # operator who sets FASTMCP_JSON_RESPONSE / FASTMCP_STATELESS_HTTP gets the
    # same behaviour http_app() would have given them (both default False, so
    # prod is unaffected). v3's http_app() reads these from the module-level
    # fastmcp.settings singleton (which parses those env vars), so read the same.
    _settings = _fastmcp.settings
    _json_response = bool(getattr(_settings, "json_response", False))
    _stateless = bool(getattr(_settings, "stateless_http", False))
    # FIX C: forward debug the same way FastMCP.http_app() does
    # (debug=fastmcp.settings.debug). getattr fallback keeps us safe if a future
    # fastmcp drops the attr - create_base_app(debug=) is a public kwarg.
    _debug = bool(getattr(_settings, "debug", False))
    # The SDK rejects session_idle_timeout in stateless mode (no sessions to
    # reap). Pass None there so an operator opting into stateless mode does not
    # hit a startup RuntimeError; reaping is moot when there is no session state.
    _idle = None if _stateless else idle_timeout

    session_manager = _StreamableHTTPSessionManager(
        app=_mcp._mcp_server,
        json_response=_json_response,
        stateless=_stateless,
        session_idle_timeout=_idle,
    )
    streamable_asgi = _StreamableHTTPASGIApp(session_manager)

    @_asynccontextmanager
    async def _mcp_session_lifespan(app):
        # Inner lifespan (becomes _existing_lifespan in main(), wrapped by
        # _lifespan_with_pg). Starts/stops the session manager - mirrors the
        # lifespan FastMCP's create_streamable_http_app() would have built.
        async with _mcp._lifespan_manager(), session_manager.run():
            try:
                yield
            finally:
                # Gracefully terminate active streamable-HTTP transports before
                # session_manager.run()'s finally cancels the task group. Without this,
                # active SSE/streaming responses abort mid-flight and Uvicorn logs
                # "ASGI callable returned without completing response". Parity with
                # fastmcp 3.4.2 create_streamable_http_app (PrefectHQ/fastmcp#3025).
                # terminate() is idempotent (mcp SDK), so double-terminate is a no-op.
                for transport in list(
                    getattr(session_manager, "_server_instances", {}).values()
                ):
                    try:
                        await transport.terminate()
                    except Exception:
                        logger.debug(
                            "Error terminating streamable-HTTP transport on shutdown",
                            exc_info=True,
                        )

    app = _create_base_app(
        routes=[
            _Route(
                "/mcp",
                endpoint=streamable_asgi,
                methods=["GET", "POST", "DELETE"],
            ),
            *_mcp._get_additional_http_routes(),
        ],
        middleware=middleware,
        debug=_debug,
        lifespan=_mcp_session_lifespan,
    )
    app.state.fastmcp_server = _mcp
    app.state.path = "/mcp"
    return app, session_manager


# --- Tool wrapper modules (import for @mcp.tool side effect) -----------------
# These imports must stay at the end of the file: the tool bodies live in
# src/mcp/tools/* and import `mcp` + kwargs/offload decorators from this module,
# so they require those names (defined above) to exist first.  Importing the
# module triggers the @mcp.tool decorators that register the tools onto `mcp`.
#
# A fresh import is FORCED on every (re)load of this module: `mcp` is a NEW
# FastMCP instance each time server.py executes (whether on first import, after
# `sys.modules.pop("src.mcp.server")` + re-import as ~15 tests do, or via
# `importlib.reload(server)`).  If a tool module were left cached in
# sys.modules, its @mcp.tool decorators would NOT re-run, so its tools would be
# registered on the OLD `mcp` and missing from the new one (tool count drops).
# Popping the tool modules first guarantees they re-execute against the current
# `mcp`, keeping the tool surface complete after any reload.
for _tool_mod in (
    "src.mcp.tools.orm_tools",       # Phase 1
    "src.mcp.tools.stylesheet",      # Phase 2
    "src.mcp.tools.inspect_tools",   # Phase 3
    "src.mcp.tools.session_tools",   # Phase 3
    "src.mcp.tools.spec",            # Phase 4
    "src.mcp.tools.cli",             # #336 (cli_help split out of spec)
    "src.mcp.tools.discovery",       # Phase 5
    "src.mcp.tools.guidance",        # A2 (split out of discovery)
    "src.mcp.tools.test_tools",      # WI-4 test-surface tools (25->31)
):
    sys.modules.pop(_tool_mod, None)
    # H3 fix: popping the submodule from sys.modules is NOT enough - the parent
    # package object (`src.mcp.tools`) RETAINS the submodule as an ATTRIBUTE, so a
    # later `from src.mcp.tools import <mod>` binds that STALE attribute WITHOUT a
    # fresh import (no @mcp.tool re-run -> tools registered on the dead `mcp`). This
    # silently dropped the 6 WI-4 test tools (31->25) whenever a sibling test did
    # `importlib.reload(src.mcp.server)` (e.g. test_nonorm_query_bounds), making
    # test_tool_count_sync / test_entrypoint_tool_surface flaky under full
    # collection. Deleting the package attribute forces the from-import below to do
    # a real re-import against the current `mcp`. (The older modules masked the bug
    # because earlier server.py imports happen to re-import them transitively.)
    _pkg_name, _, _sub = _tool_mod.rpartition(".")
    _pkg = sys.modules.get(_pkg_name)
    if _pkg is not None and hasattr(_pkg, _sub):
        try:
            delattr(_pkg, _sub)
        except AttributeError:
            pass
del _tool_mod, _pkg_name, _sub, _pkg

from src.mcp.tools import cli as _cli_tools  # noqa: E402,F401 - #336 cli_help split
from src.mcp.tools import discovery as _discovery_tools  # noqa: E402,F401
from src.mcp.tools import guidance as _guidance_tools  # noqa: E402,F401
from src.mcp.tools import inspect_tools as _inspect_tools  # noqa: E402,F401
from src.mcp.tools import orm_tools as _orm_tools  # noqa: E402,F401
from src.mcp.tools import session_tools as _session_tools  # noqa: E402,F401
from src.mcp.tools import spec as _spec_tools  # noqa: E402,F401
from src.mcp.tools import stylesheet as _stylesheet_tools  # noqa: E402,F401
from src.mcp.tools import test_tools as _test_tools  # noqa: E402,F401 - WI-4 test-surface

# Re-exports from cli.py (#336 split): _cli_help / cli_help moved out of spec.py
# to keep that module under TOOL_MODULE_MAX_LINES. Tests import these via
# src.mcp.server (e.g. test_mcp_spec_tools.py spec_tools fixture), so they must
# remain accessible under the same server namespace as before.
from src.mcp.tools.cli import (  # noqa: E402,F401
    _cli_help,
    cli_help,
)

# Phase 5 / A2 re-exports: the two public discovery tools, plus the impl symbols
# that tests import via src.mcp.server (directly, e.g. test_mcp_find_examples.py
# imports _find_examples; test_mcp_impact_analysis.py imports _impact_analysis +
# _compute_risk; test_calibration_eval.py imports _compute_risk; or as attributes
# off a popped + re-imported module).  The impl bodies read the hub through _srv.
# at call time so monkeypatch.setattr(srv, ...) keeps working.
from src.mcp.tools.discovery import (  # noqa: E402,F401
    _compute_risk,
    _find_examples,
    _impact_analysis,
    find_examples,
    impact_analysis,
)

# A2 re-exports: the three public guidance tools (split out of discovery in A2),
# plus the impl symbols that tests import via src.mcp.server (directly, e.g.
# test_cross_tenant_isolation.py imports _fetch_method_for_diff +
# _diff_method_across_versions; test_mcp_pattern_tools.py imports _suggest_pattern
# / _check_module_exists / _find_override_point; or as attributes off a popped +
# re-imported module).  _suggest_pattern is also reached lazily by
# src/mcp/inspect.py (entity_lookup kind='pattern') through srv._suggest_pattern,
# so the re-export keeps that path working.  The impl bodies read the hub through
# _srv. at call time so monkeypatch.setattr(srv, ...) keeps working.  The two
# guidance-only constants (_VALID_PATTERN_LANGUAGES / _ANTI_PATTERNS_BASE, §2.7 -
# verified guidance-only) now live solely in tools/guidance.py and are NOT
# re-exported (no external importer).
from src.mcp.tools.guidance import (  # noqa: E402,F401
    _anti_patterns_for_convention,
    _check_module_exists,
    _diff_method_across_versions,
    _ee_confusion_live,
    _fetch_method_for_diff,
    _find_override_point,
    _format_check_module_exists,
    _format_find_override_point,
    _format_suggest_pattern,
    _suggest_pattern,
    check_module_exists,
    find_override_point,
    suggest_pattern,
)

# Phase 3 re-exports: the five inspect tools + the four Wave-E session tools.
# Tests + external callers import these public tool symbols from src.mcp.server;
# preserve the path after the move. The wrappers reach the hub through `srv.` at
# call time so monkeypatch.setattr(srv, ...) keeps working.
from src.mcp.tools.inspect_tools import (  # noqa: E402,F401
    describe_module,
    entity_lookup,
    model_inspect,
    module_inspect,
    profile_inspect,
)

# Backward-compat re-exports: tests + external callers import these public tool
# symbols from src.mcp.server; preserve the path after the move.
from src.mcp.tools.orm_tools import (  # noqa: E402,F401
    resolve_orm_chain,
    validate_depends,
    validate_domain,
    validate_relation,
)
from src.mcp.tools.session_tools import (  # noqa: E402,F401
    list_available_profiles,
    list_available_versions,
    set_active_profile,
    set_active_version,
)

# Phase 4 re-exports: the five public spec tools, plus the impl + const symbols
# that tests import via src.mcp.server (directly, e.g. test_lint_matcher_unit.py,
# or as attributes off a popped + re-imported module, e.g. test_mcp_spec_tools.py's
# `spec_tools` fixture which accesses mcp_server._lookup_core_api etc.).  The impl
# bodies reach the hub through `srv.` at call time so monkeypatch.setattr(srv, ...)
# keeps working.  The whole lint const/cache cluster (incl. the sole mutable
# compiled-pattern cache, §2.7 - exactly one home) now lives solely in
# tools/spec.py; _LINT_V0_BANNER is the only one tests import, re-exported here
# for path stability.
from src.mcp.tools.spec import (  # noqa: E402,F401
    _LINT_V0_BANNER,
    _api_version_diff,
    _build_noqa_suppress,
    _compile_lint_pattern,
    _find_deprecated_usage,
    _format_deprecated_usage,
    _lint_check,
    _lint_check_xml,
    _lint_match_kind,
    _lookup_core_api,
    _match_lint_rule_lines,
    api_version_diff,
    find_deprecated_usage,
    lint_check,
    lookup_core_api,
)

# Phase 2 re-exports: the two public stylesheet tools, plus the three impl
# helpers. _resolve_stylesheet / _find_style_override are imported by tests via
# src.mcp.server and resolved through srv. by the async wrapper at call time (so
# monkeypatch.setattr(srv, "_find_style_override", ...) keeps working).
# _literal_style_lookup must be re-exported too: _find_examples (now in
# tools/discovery.py, Phase 5) reaches it through _srv in its style
# literal-first path.
from src.mcp.tools.stylesheet import (  # noqa: E402,F401
    _find_style_override,
    _literal_style_lookup,
    _resolve_stylesheet,
    find_style_override,
    resolve_stylesheet,
)

# --- Module-overview + entity-listing helper modules (Phase 7 / A1) ----------
# src/mcp/describe.py + src/mcp/listings.py hold the _describe_module /
# _module_dep_closure / _list_* read helpers moved out of this hub.  They are
# NOT tool modules (no @mcp.tool, no registration side effect) - they are
# imported here only to re-export their symbols so src.mcp.server._describe_module
# / _list_* keep resolving for tests + the inspect.py / resources.py call sites.
#
# They are POPPED before import for the SAME reload-safety reason as the tool
# modules above: each binds `_srv = sys.modules['src.mcp.server']` at end-of-
# module.  If left cached across a `sys.modules.pop('src.mcp.server')` + re-import
# (~15 tests do this), their `_srv` would stay pinned to the STALE server
# generation, so a monkeypatch on the freshly re-imported `srv` would not be
# observed by these bodies.  Popping forces a re-execute against the current
# generation, re-binding `_srv` to it.
for _helper_mod in (
    "src.mcp.describe",
    "src.mcp.listings",
):
    sys.modules.pop(_helper_mod, None)
del _helper_mod

from src.mcp.describe import (  # noqa: E402,F401
    _describe_module,
    _module_dep_closure,
)
from src.mcp.listings import (  # noqa: E402,F401
    _list_extenders,
    _list_fields,
    _list_js_patches,
    _list_methods,
    _list_owl_components,
    _list_qweb_templates,
    _list_views,
    _list_views_by_module,
    _list_views_core,
    _not_found_freshness_note,
    _probe_model_indexed,
)


def main() -> None:
    """Server startup entrypoint (invoked by ``python -m src.mcp``).

    Lives in ``src/mcp/server.py`` as a plain importable function rather than an
    ``if __name__ == "__main__"`` block on purpose: running this file directly
    (``python -m src.mcp.server``) makes it ``__main__``, and the tool wrapper
    modules then re-import it under its real name ``src.mcp.server`` - creating a
    SECOND FastMCP ``mcp`` instance that carries all 31 ``@mcp.tool`` registrations
    while ``__main__.mcp`` stays empty. The served app would then be built from the
    0-tool ``__main__`` instance (MCP ``tools/list`` returns 0). Keeping startup in
    ``main()`` and serving via ``python -m src.mcp`` (see ``src/mcp/__main__.py``)
    loads this module exactly once under its real name, so the served ``mcp`` is the
    one that owns the 31 tools.

    Uses the module-global ``mcp`` - the instance the ``@mcp.tool`` decorators in
    ``src/mcp/tools/*`` registered against when this module was imported.
    """
    import logging as _logging

    from src import config as _config
    _config.init_dotenv()

    # HIGH #3: fail-fast on zero-value / mis-ordered ORM concurrency knobs now
    # that init_dotenv() has settled the env. A bad value here silently reverts
    # a #273/#276 protection, so refuse to start rather than serve degraded.
    _validate_orm_env()

    from src.logging_config import configure_logging as _configure_logging
    _configure_logging(level=_logging.INFO)

    # --- Option B: build the streamable-http app directly so we can pass
    # session_idle_timeout (#279 item 1, ADR-0049) ------------------------------
    # The core construction lives in the module-level _build_streamable_http_app()
    # helper (single source of truth - tests/test_session_idle_timeout.py calls
    # the SAME helper so the two can never drift). main() owns the wrapping:
    # lifespan compose with _lifespan_with_pg, feedback mount, uvicorn.
    # ADR-0049 records the 3 triggers to revert to the http_app() kwarg once
    # upstream forwards session_idle_timeout. SESSION_IDLE_TIMEOUT (default 3600s
    # = 1h, value-guarded) reaps abandoned streamable-http sessions (#279).
    import uvicorn as _uvicorn
    from starlette.middleware import Middleware as _Middleware

    from src.mcp.middleware import AuthMiddleware

    _session_idle_timeout = _resolve_session_idle_timeout()
    _app, _session_manager = _build_streamable_http_app(
        idle_timeout=_session_idle_timeout,
        middleware=[_Middleware(AuthMiddleware)],
    )

    # --- Resilient PG startup: degraded mode + background retry (incident 2026-05-19) ---
    # AuthMiddleware.dispatch calls auth_store() → get_pool() on every authenticated
    # request. If init_pool() never ran, get_pool() raises RuntimeError. Previously
    # we blocked startup on _ensure_pg() - but that turned any DB blip into uvicorn
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
        except Exception as e:  # noqa: BLE001 - any failure → degraded mode
            _log.warning(
                "PG pool init failed at startup - entering DEGRADED mode."
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
                            " - degraded mode cleared",
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
                        "%d Neo4j nodes have no `profile` property - these are invisible"
                        " to profile-scoped MCP queries. Run a full reindex per ADR-0016"
                        " to backfill.",
                        _row["legacy_count"],
                    )
        except Exception:
            pass  # startup warning is best-effort - never block startup

        # Bootstrap admin settings catalogue (idempotent, best-effort).
        # Runs after PG pool init attempt. Swallows errors so a missing
        # app_settings table (m13_010 not yet applied) never blocks startup.
        try:
            from src.settings_registry import bootstrap_settings_safe as _bootstrap
            # MCP runs on the osm_reader DSN (no UPDATE on app_settings), and
            # reads ZERO metadata columns from the DB, so it only INSERTs
            # missing rows - never converges metadata (converge_metadata=False).
            await _asyncio.to_thread(_bootstrap, converge_metadata=False)
        except Exception:  # noqa: BLE001
            pass  # non-fatal - logged inside bootstrap_settings_safe

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

            # Close the Neo4j driver singleton so the GC destructor does not
            # emit "Driver's destructor called while session still open" warnings
            # (neo4j/_sync/driver.py:547).  _driver is module-level; reset to
            # None so a subsequent startup re-initializes cleanly.
            global _driver
            if _driver is not None:
                try:
                    _driver.close()
                except Exception:  # noqa: BLE001
                    pass
                _driver = None

    _app.router.lifespan_context = _lifespan_with_pg
    # --------------------------------------------------------------------------

    # Mount feedback API on MCP port so remote users can submit thumbs-up/down.
    # feedback.router exposes POST /api/feedback and GET /api/feedback/{pattern_id}.
    # Auth is already enforced by AuthMiddleware above - no loopback guard needed.
    # We wrap the router in a mini FastAPI sub-app (include_router) and mount it
    # at the root prefix '' so its /api/feedback paths remain unchanged.
    from fastapi import FastAPI as _FastAPI

    from src.web_ui.routes import deploy_key as _deploy_key
    from src.web_ui.routes import feedback as _feedback

    _feedback_app = _FastAPI()
    _feedback_app.include_router(_feedback.router)
    # Mount tenant self-service deploy-key endpoint (ADR-0034 D7, WI-I).
    # GET /api/tenant/deploy-key - tenant_id resolved from X-API-Key auth state,
    # never from a user-supplied path parameter (cross-tenant leakage impossible).
    _feedback_app.include_router(_deploy_key.router)
    _app.mount("", _feedback_app)

    # #227 backpressure: cap the number of concurrent connections uvicorn will
    # service. Beyond this, uvicorn returns HTTP 503 immediately instead of
    # letting the accept-backlog grow unbounded (which turns overload into
    # latency + OOM). There are now THREE independent inner bounds - the embed
    # semaphore (EMBEDDER_MAX_CONCURRENCY), the ORM semaphore
    # (ORM_QUERY_MAX_CONCURRENCY) and the non-ORM heavy-read semaphore
    # (NONORM_READ_MAX_CONCURRENCY, #276 G6). The connection ceiling is a
    # multiple of their SUM so that all bounded pools can be fully saturated and
    # there is still ample headroom for cheap tools + /health while the three
    # semaphores independently bound the expensive slots. Tunable via
    # MCP_LIMIT_CONCURRENCY.
    _limit_concurrency = int(
        os.getenv(
            "MCP_LIMIT_CONCURRENCY",
            str(
                (
                    EMBEDDER_MAX_CONCURRENCY
                    + ORM_QUERY_MAX_CONCURRENCY
                    + NONORM_READ_MAX_CONCURRENCY
                )
                * 16
            ),
        )
    )
    _uvicorn.run(
        _app,
        host=_mcp_host(),
        port=_mcp_port(),
        timeout_graceful_shutdown=0,
        lifespan="on",
        access_log=True,
        limit_concurrency=_limit_concurrency,
    )


if __name__ == "__main__":
    # Backward-compat: running `python -m src.mcp.server` makes THIS file
    # __main__, so the tool wrapper modules re-import src.mcp.server under its
    # real name and register the 31 @mcp.tool decorators onto a SECOND FastMCP
    # instance, leaving __main__.mcp empty. Delegating to the real module's
    # main() serves the instance that actually owns the tools (the import below
    # binds main from src.mcp.server #2, whose module-global mcp has 31 tools).
    # The clean entrypoint `python -m src.mcp` (src/mcp/__main__.py) avoids the
    # double-instance entirely; this guard only keeps the old command working.
    from src.mcp.server import main as _main

    _main()
