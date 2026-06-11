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
from contextlib import asynccontextmanager, contextmanager, nullcontext
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
    FIND_EXAMPLES_ANN_LIMIT,
    GLOBAL_PROFILE,
    HNSW_ITERATIVE_SCAN,
    IMPACT_MODULES_MAX,
    IMPACT_RISK_HIGH_THRESHOLD,
    IMPACT_RISK_MED_THRESHOLD,
    LIST_PREVIEW_FIELDS_MAX,
    LIST_PREVIEW_MAX_ITEMS,
    LIST_PREVIEW_PATCHES_MAX,
    MAGIC_FIELDS,
    NEO4J_QUERY_TIMEOUT_SECONDS,
    NONORM_READ_MAX_CONCURRENCY,
    NONORM_SLOT_ACQUIRE_TIMEOUT,
    ORM_QUERY_MAX_CONCURRENCY,
    ORM_SLOT_ACQUIRE_TIMEOUT,
    PG_POOL_MAX_CONN,
    PG_POOL_MIN_CONN,
    REL_DEPENDS_ON,
    REL_DEPENDS_ON_FIELD,
    REL_INHERITS,
    REL_INHERITS_VIEW,
    REL_TARGETS_MODEL,
    REL_USES_FIELD,
    SNIPPET_PREVIEW_MAX_LINES,
    STYLE_CHUNK_TYPES,
    TIMEOUT_EMBEDDER_READ_QUERY,
    VALID_CHUNK_TYPES,
)
from src.mcp import session as _session
from src.mcp.example_lexical import lexical_example_lookup
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
    _ANCESTOR_TAGGED_PROLOGUE_INHERITS_ONLY,
    OrmQueryTimeout,
    _ancestor_owner_names,
    _bounded,
    _count_fields_with_inherited,
    _count_methods_with_inherited,
    _edition_rank_cypher,
    _is_tx_timeout,
    _list_fields_with_inherited,
    _list_methods_with_inherited,
    _resolve_field_inherited,
    _resolve_method_inherited,
)
from src.mcp.orm import (
    # ORM-validation impls: the tool wrappers moved to tools/orm_tools.py
    # (Phase 1) but tests still import these via src.mcp.server. `X as X` marks
    # an intentional re-export so ruff keeps F401 active for the genuinely
    # internal names above (instead of a blanket per-block noqa).
    _resolve_orm_chain as _resolve_orm_chain,
)
from src.mcp.orm import (
    _validate_depends as _validate_depends,
)
from src.mcp.orm import (
    _validate_domain as _validate_domain,
)
from src.mcp.orm import (
    _validate_relation as _validate_relation,
)
from src.mcp.refs import mint_refs
from src.mcp.resources import register_resources
from src.mcp.style_literal import is_literal_token
from src.mcp.tool_log_middleware import UsageLogMiddleware as _UsageLogMiddleware
from src.mcp.tree_builder import render_list_block

logger = logging.getLogger(__name__)

# Sentinel api_key_id for direct _impl calls (tests, CLI) — refs are scoped
# to this namespace and do not collide with production tenant refs.
_ANONYMOUS_API_KEY_ID = "anonymous"


# Render-only edition label — WG-5 T1.
# Maps raw license string → human-readable label for MCP output.
# License facts (Odoo S.A., https://www.odoo.com/documentation/19.0/legal/licenses.html):
#   OEEL-1 = Odoo Enterprise Edition License — Odoo S.A.'s OWN Enterprise add-ons.
#   OPL-1  = Odoo Proprietary License — Odoo S.A.'s license for THIRD-PARTY / proprietary
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
# S.A.'s own Enterprise license — not a Viindoo license). A first-party Viindoo
# module (`edition='viindoo'`) must read "Viindoo Enterprise (EE)" — never
# "Odoo Enterprise (EE)" — even on the defensive edge where its license string
# would otherwise map elsewhere.
_FIRST_PARTY_EDITIONS: frozenset[str] = frozenset({"viindoo"})


def _edition_label(edition: str | None, license: str | None = None) -> str:
    """Return a human-readable edition label for MCP output.

    Resolution order:
      1. A DEFINITIVE first-party ``edition`` enum (``_FIRST_PARTY_EDITIONS``)
         wins outright — license can never override a known first-party author
         (#263 / N3: even if a module's license string would otherwise map to
         Odoo Enterprise, a ``viindoo`` edition still reads "Viindoo Enterprise").
      2. Otherwise ``license`` (SPDX string) — more specific than a generic
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
         (``css_mod/static/...``) — a close approximation of repo-relative.
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
    to its own checkout — the git URL (``github.com/odoo/odoo``) — never the
    server checkout directory name (``odoo_17.0``), which is host-specific
    detail the client neither knows nor needs.

    Returns None when repo_id is None, the repo is unknown, or the repo has no
    URL (locally-registered repos) — callers then fall back to the dirname.
    Successful lookups (incl. a genuine NULL url) are memoised; transient DB
    failures are not cached so a later call can retry.  Note: the cache has no
    TTL/invalidation — a url set AFTER the first lookup serves the stale value
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

mcp = FastMCP("odoo-semantic", instructions=INSTRUCTIONS)
# Register 7 MCP resources (odoo:// URIs) — Pattern 8, Wave F.
register_resources(mcp)
# Register FastMCP-layer usage logging middleware so that on_call_tool has
# access to context.message.name (the real tool name) — see F5 fix in
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

# Session-mutating tools (set_active_version, set_active_profile) — write the
# per-(api_key, mcp_session) in-memory pin store but are idempotent and
# non-destructive.  readOnlyHint=False because they mutate session state
# (the in-memory pin — no DB write since #251).
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
# set_active_profile, list_available_profiles) intentionally do NOT use this —
# they are how a client discovers/sets the version in the first place.
# MCP Resources (odoo://{version}/...) keep sentinel support: the version is
# always present in the URI path, so the silent-omission failure mode cannot
# occur there.
RequiredOdooVersion = Annotated[
    str,
    Field(
        description=(
            "REQUIRED — pass the concrete Odoo version explicitly on every call "
            "(e.g. '17.0'). Passing it per call is the correct, race-free choice "
            "under concurrency: parallel sessions or concurrent sub-agents that "
            "share one MCP session each get the version they pass, regardless of "
            "any session pin. 'auto' is a single-actor convenience that reuses "
            "the pin set by set_active_version; it is NOT safe when multiple "
            "actors share a session (the pin is last-write-wins) — pass the "
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
    INSIDE the worker thread — the slot's lifetime is tied to the THREAD, never
    the (possibly cancelled) caller coroutine. That is the #276 / CRITICAL-2
    cancel-safety invariant: a client disconnect cancels the wrapper coroutine
    but the worker thread keeps the slot until its own ``finally`` releases it.

    Built once on first ``.get()`` — lazily and post-dotenv, so a ``.env``-only
    cap is honoured (``config.init_dotenv()`` runs in __main__ AFTER this module
    imports; the import-time constant can be stale — ADR-0031). ``cap_in_use`` /
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
# #276 G7 — CANCEL-SAFE SLOT: the original guard used an asyncio.Semaphore whose
# slot was released in a coroutine `finally`. When a client disconnected mid-embed
# FastMCP cancelled the coroutine; the `finally: sem.release()` ran on cancel —
# but the underlying embed thread (inside embed_async's own to_thread) kept
# running and STILL held the Ollama connection. The slot freed early → the exact
# #276 pool-drain. Fixed by porting the offload_bounded thread-held
# BoundedSemaphore pattern: acquire()/release() now happen INSIDE the worker
# thread, so the slot's lifetime is tied to the THREAD, not the coroutine. A
# cancel can no longer free a slot while a thread is still embedding. In the
# worker thread we call the SYNC embed path DIRECTLY (NOT embed_async — that
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
# acquired/released inside the worker thread — no event-loop affinity, no
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
    returns the whole string unchanged when it already fits — a cheap no-op for
    normal short queries).
    """
    from src.indexer.embedder import split_by_token_budget

    chars_per_token = getattr(embedder, "chars_per_token", None) or 4.0
    return split_by_token_budget(text, EMBEDDER_TOKEN_BUDGET, chars_per_token)[0]


def _embed_sync_query(embedder, payload: list[str]) -> list[list[float]]:
    """Run a SINGLE query embed SYNCHRONOUSLY, routed through the short-timeout client.

    Called only from inside the embed worker thread (#276 G7). Prefers the HTTP
    base's ``_embed_with_timeout`` so the query goes through the dedicated
    short-timeout (TIMEOUT_EMBEDDER_READ_QUERY) client — never the 1200s batch
    client. Falls back to the Protocol-guaranteed sync ``embed`` for embedders
    without that method (e.g. FakeEmbedder in tests). We deliberately do NOT call
    ``embed_async`` here — that would nest a second to_thread / child loop inside
    this already-offloaded thread (R-A6).
    """
    fn = getattr(embedder, "_embed_with_timeout", None)
    if callable(fn):
        return fn(payload, TIMEOUT_EMBEDDER_READ_QUERY)
    return embedder.embed(payload)


def _embed_query_in_thread(embedder, payload: list[str]) -> list[float]:
    """Worker-thread body for a single query embed — thread-held slot (#276 G7).

    Acquires the embed BoundedSemaphore HERE (on the worker thread) so the slot
    lives with the thread, not the caller coroutine. On acquire-timeout: fast
    fail with EmbedOverloaded. The sync embed runs while the slot is held and the
    slot is released only in this thread's ``finally`` — a cancelled coroutine
    can never free it early while the embed is still in flight.
    """
    sem = _get_embed_semaphore()  # builds the pool → populates cap_in_use below
    # _get_embed_semaphore() (just called, same thread) always populates
    # cap_in_use / timeout_in_use under the pool lock before returning, so these
    # are never None here — read them directly (no default-constant fallback).
    cap = _embed_pool.cap_in_use
    slot_timeout = _embed_pool.timeout_in_use
    if not sem.acquire(timeout=slot_timeout):
        raise EmbedOverloaded(
            "server busy — too many concurrent embedding requests"
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
    therefore blocks the whole server — a single slow/locked query freezes
    /health and every concurrent request (the #227 504). This decorator wraps a
    sync handler in an ``async def`` that runs the original body in a worker
    thread via ``asyncio.to_thread``, so the loop stays free.

    Mechanism notes:
      * ``functools.wraps`` copies ``__wrapped__`` so ``inspect.signature``
        (which FastMCP uses to build the input schema) resolves to the ORIGINAL
        handler signature — the generic ``*a, **k`` wrapper is invisible to
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
# and validated at server startup by _validate_orm_env() — a value of 0 or an
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
#     `await asyncio.to_thread(...)` raises CancelledError immediately — but the
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
# value — there is no import-time/.env mismatch to warn about (#275 LOW).
class OrmOverloaded(RuntimeError):
    """Raised when the bounded ORM semaphore cannot be acquired in time.

    Caught in ``offload_bounded`` and surfaced to the MCP client as a fast,
    actionable overload *string* (NOT a protocol-level error) — uniform with the
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
# impact_analysis — a 6-query fan-out over TARGETS_MODEL / DEPENDS_ON / BOUND_TO
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
    pool while patching the other — the exact missed-fix class that bit the embed
    path in #275 (fixed late in #278).

    THREAD-LIFETIME RELEASE (the #276 / CRITICAL-2 invariant — preserved verbatim):
      ``acquire()``/``release()`` run INSIDE the worker thread, so a slot's
      lifetime is bound to the THREAD, never the (possibly cancelled) caller
      coroutine. When a client disconnects mid-call FastMCP cancels the wrapper
      coroutine and the awaited ``to_thread`` future raises ``CancelledError``
      immediately — but the worker thread keeps running and STILL HOLDS the slot
      until its own ``finally`` releases it. Overload + timeout metric/log
      bookkeeping also runs in-thread, so it is recorded even after a cancel.
      ``functools.wraps`` preserves ``__wrapped__`` so FastMCP introspects the
      ORIGINAL handler signature; ``asyncio.to_thread`` copies the current
      ``contextvars.Context`` so per-request ContextVars propagate into the thread.

    The four observable strings that differ between the ORM and non-ORM pools are
    parameters so the log lines (ops grep them) and the client-facing busy string
    stay byte-identical to the pre-consolidation behaviour:
      * ``log_label``         — overload-log prefix ("ORM tool" | "non-ORM read").
      * ``overload_phrase``   — exception text ("ORM-validation requests" |
                                "heavy read requests").
      * ``timeout_log_prefix``— timeout-log prefix ("ORM query" | "non-ORM read
                                query").
      * ``timeout_msg_default``— wrapper fallback when the timeout exc has no
                                ``user_message``.
    ``call_context_fn`` (the ORM model/version/profile context, ``None`` for the
    non-ORM pool whose tools have a different signature) decides whether the log
    lines append a trailing context string — when ``None`` the format strings
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
            # None here — read them directly (no _default_* fallback needed).
            cap = pool.cap_in_use
            slot_timeout = pool.timeout_in_use
            if not sem.acquire(timeout=slot_timeout):
                # Saturated: fast-reject. Record metric + log here so cancel-storms
                # are never invisible (the coroutine may already be gone).
                metric_overloaded(tool_name)
                if call_context_fn is not None:
                    logger.warning(
                        "%s overloaded — semaphore full (max %d): tool=%s %s",
                        log_label,
                        cap,
                        tool_name,
                        call_context_fn(a, k),
                    )
                else:
                    logger.warning(
                        "%s overloaded — semaphore full (max %d): tool=%s",
                        log_label,
                        cap,
                        tool_name,
                    )
                raise OrmOverloaded(
                    f"server busy — too many concurrent {overload_phrase}"
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
# (those are defined a little further down — the bindings must come after them).


def _validate_orm_env() -> None:
    """Fail-fast guard for the ORM concurrency / timeout env knobs (HIGH #3).

    Called once at server startup (NOT at import — see the call site in the
    __main__ block), so pytest collection and tool imports never trip these
    assertions. Values are re-read from ``os.getenv`` here rather than trusting
    the import-time constants, because ``config.init_dotenv()`` runs in the
    __main__ block AFTER this module imports — so a ``.env``-only value would
    not yet be reflected in the module-level constant when this runs. Each
    foot-gun below silently reverts a load-bearing #273/#276 protection:

      * NEO4J_QUERY_TIMEOUT_SECONDS <= 0 — the neo4j driver treats 0 as
        "no timeout", reverting the core #273 per-query-timeout fix.
      * ORM_QUERY_MAX_CONCURRENCY <= 0 — every ORM call fast-rejects forever
        (0 slots can never be acquired).
      * ORM_SLOT_ACQUIRE_TIMEOUT >= NEO4J_QUERY_TIMEOUT_SECONDS — the reject is
        no longer "fast", so an overloaded server pins a worker-thread slot for
        as long as the query itself would run. The .env.example states this
        constraint; this enforces it.
      * NONORM_READ_MAX_CONCURRENCY <= 0 / NONORM_SLOT_ACQUIRE_TIMEOUT >=
        NEO4J_QUERY_TIMEOUT_SECONDS — same two foot-guns for the separate
        non-ORM heavy-read pool (#276 G6).
      * EMBEDDER_MAX_CONCURRENCY <= 0 — BoundedSemaphore(0) can never be
        acquired, so every query-embed fast-rejects forever (#276 G7); same
        foot-gun the ORM/non-ORM pools already guard.
      * EMBEDDER_SLOT_ACQUIRE_TIMEOUT >= EMBEDDER_TIMEOUT_READ_QUERY — the
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
            "(BoundedSemaphore(0) can never be acquired) — #276 G7."
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

    Logs only structural identifiers (model / version / profile) — never the
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
    # value is purely numeric like '17.0' — otherwise leave it None rather
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


# Bounded offload for the 4 ORM-validation tools (#273): a thread-held
# BoundedSemaphore so a fan-out burst of dense ORM traversals cannot drain the
# shared ThreadPoolExecutor / Neo4j pool. The slot is thread-bound (#276
# CRITICAL-2 cancel-safety — see _make_bounded_offload). Accepted trade-off: a
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
        "ORM query timed out — the request was too expensive to"
        " complete; narrow the model/version and retry."
    ),
    call_context_fn=_orm_call_context,
)


# Bounded offload for NON-ORM heavy reads (#276 G6, currently impact_analysis — a
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
        "Query timed out — the request was too expensive to"
        " complete; narrow the model/version and retry."
    ),
    call_context_fn=None,
)


# --- Pool-less Neo4j offload for the read-surface discriminator tools --------
# `@offload_neo4j` = `@offload` + an OrmQueryTimeout catch that records the
# non-ORM timeout metric + returns the clean English `user_message` string
# (ADR-0023 raw-text contract — never a protocol-level isError). It is the
# backstop that turns a *raised* OrmQueryTimeout into a clean string; the
# per-query work (routing bare `session.run(...)` through `_data_bounded` /
# `_single_bounded`, which wrap the Cypher in neo4j.Query(timeout=...) and
# convert a tx-timeout ClientError → OrmQueryTimeout) still happens at every
# query site. Both halves are required — see the timeout-hardening design §1.1.
#
# Kept SEPARATE from `@offload` (not a retrofit): `@offload` also wraps handlers
# that do non-Neo4j work (Postgres reads, on-disk file reads) and embed-then-
# offload handlers (find_style_override embeds on the event loop BEFORE the
# to_thread hop) — catching OrmQueryTimeout there would be pointless (no Neo4j)
# or wrong (would swallow / mislabel a timeout from a different subsystem).
#
# POOL-LESS by design (NO semaphore). The tools this wraps are single bounded
# queries or small fixed multi-query helpers, each individually 30s-bounded by
# the per-query Neo4j timeout — not the heavy fan-out drain the bounded pools
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
        re-raises to the async wrapper which only *returns* ``user_message`` —
        it does NOT re-record, so the metric fires exactly once.
      * A non-OrmQueryTimeout exception propagates unchanged — we never swallow
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
                "Query timed out — narrow the entity/version and retry.",
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
    via the shared ``_bounded`` helper (reused from src.mcp.orm — a peer module
    server already imports — so there is NO duplicate timeout helper). Neo4j
    Result consumption is LAZY, so the transaction-timeout ``ClientError`` fires
    during ``.data()``, not during ``session.run`` — both are therefore inside
    the try here. A tx-timeout ``ClientError`` becomes ``OrmQueryTimeout`` so the
    ``offload_bounded_nonorm`` wrapper records the metric in-thread and surfaces a
    clean English message; any other ``ClientError`` propagates unchanged.

    ``label`` is a short English noun phrase naming what was being resolved (e.g.
    "impact analysis for 'sale.order'"), used only in the timeout message — never
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

    Uses a ContextVar so the value is isolated per coroutine — concurrent
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
    the tool-body execution context — so the tool body still reads the
    ``'default'`` sentinel even though the middleware (reading in its OWN
    context) logged the correct numeric PK. That asymmetry is exactly the #248
    bug: ``set_active_version`` / ``set_active_profile`` skipped the persist and
    returned "session context unavailable".

    When the ContextVar value is still the ``'default'`` sentinel we therefore
    make a second, additive attempt: recover the numeric PK directly from the
    CURRENT HTTP request's own ``X-API-Key`` header via the warm auth cache
    (the same machinery the middleware uses). This derives the id ONLY from the
    request's own header — never from a shared/global — so it cannot bleed an
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
    # ContextVar is the sentinel — try the per-request header-recovery fallback.
    recovered = _recover_api_key_id_from_request()
    return recovered if recovered is not None else value


def _recover_api_key_id_from_request() -> int | None:
    """Recover the numeric api_key_id from the current request's X-API-Key header.

    Used as the #248 fallback when ``_api_key_id_var`` is still ``'default'``
    inside a tool body (FastMCP context-boundary loss on stateful
    streamable-HTTP). Reuses the exact warm-cache lookup the FastMCP middleware
    uses, so the id is the same numeric PK ``AuthMiddleware`` already resolved
    for THIS request.

    SECURITY: the key material comes solely from ``get_http_request()`` — the
    request bound to the current ASGI task — so the recovered id can never be
    one belonging to a different concurrent request / tenant. Returns ``None``
    on any of: no active HTTP request, no ``X-API-Key`` header, or a cache miss
    (TTL edge) — the caller then keeps the graceful ``'default'`` sentinel.
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
    is a real error worth surfacing loudly — #248) from the benign stdio / CLI
    no-op (gentle note). Header presence is a far more reliable HTTP-auth signal
    than ``_get_api_key_id()`` (which can read ``'default'`` on the #248
    propagation path). Never raises — absence of an HTTP request returns False.
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
         boundary, so no warm-cache recovery dance is needed — a plain header
         read suffices when the ContextVar did not propagate.

    Returns the ``_session._NO_SESSION_SENTINEL`` for stdio / no active HTTP
    request / header-less callers (which reproduces the pre-#251 single-pin
    semantics). Never raises.
    """
    value = _mcp_session_id_var.get()
    if value != _session._NO_SESSION_SENTINEL:
        return value
    # ContextVar is the sentinel — try a direct per-request header read.
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
    treat as the unrestricted ``'*'`` admin sentinel) — doing so would let a
    tenant-scoped key read ACROSS tenants. Raising instead surfaces a clean deny
    at the read entry points (which already wrap tenant resolution in
    ``try/except`` → structured ToolResult error, or let the FastMCP layer turn
    the raise into a JSON-RPC error). No data is served on this edge.
    """


def _get_tenant_id() -> int | None:
    """Return the tenant_id for the current async/sync context (ADR-0034 D4.1 plumbing).

    Populated by UsageLogMiddleware (tool_log_middleware.py) from
    request.state.tenant_id before each tool call, reset in the finally block.
    Returns None when not set — this covers:
      - Unit tests and CLI invocations (no request context)
      - Global/admin keys (tenant_id IS NULL in DB)
      - Any code path that has not yet been wired to carry tenant context

    None means admin/global access (legacy NULL-tenant key, unit tests, CLI) —
    consumed by ``_effective_allowed`` (WI-4) as "unrestricted" while a real
    tenant id scopes every user-data query to that tenant's allowed profiles.

    ContextVar semantics: each coroutine has its own isolated copy so
    concurrent requests cannot interfere with each other's tenant scope.

    #248 context-boundary fallback (SECURITY — tenant-isolation bypass)
    ------------------------------------------------------------------
    On the stateful streamable-HTTP transport FastMCP runs the tool body in a
    ``contextvars.Context`` captured per-connection BEFORE the per-call
    ``UsageLogMiddleware.on_call_tool`` runs. ``_set_server_tenant_id(tenant_id)``
    therefore mutates a context that is NOT an ancestor of the tool-body context,
    so a bare ``_tenant_id_var.get()`` reads ``None`` for EVERY HTTP call — even a
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
        admin — unchanged, legitimate unrestricted access).
      - ContextVar None, header present, warm-cache hit with int   → that int
        (tenant-scoped key — CLOSES the bypass).
      - ContextVar None, header present, warm-cache hit with None   → ``None``
        (genuine admin / global key — tenant_id IS NULL in DB; correct).
      - ContextVar None, header present, warm-cache MISS (TTL race) → FAIL-CLOSED:
        resolve AUTHORITATIVELY via ``verify_api_key_full`` (the same DB path the
        middleware uses on cache miss). Only a confirmed NULL tenant returns
        ``None``; a real tenant returns its int; if even the authoritative lookup
        is unavailable/fails we RAISE ``TenantResolutionDenied`` rather than widen
        to unrestricted. The warm cache is populated by AuthMiddleware BEFORE this
        hook fires, so this DB edge is rare — correctness over micro-perf.

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
    # ContextVar is None — could be a genuine admin/global key OR the #248
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

    Returns an ``int`` when the request's key is tenant-scoped — this is what
    closes the GUC='*' bypass.

    Raises ``TenantResolutionDenied`` on the dangerous edge: an AUTHENTICATED key
    (``X-API-Key`` present) whose tenant is neither cached nor resolvable via the
    authoritative DB lookup. Failing closed here is mandatory — returning ``None``
    would widen a scoped key to the unrestricted ``'*'`` GUC.

    SECURITY: the key material comes solely from ``get_http_request()`` — the
    request bound to the current ASGI task — so the recovered tenant can never be
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

    # 3) Authenticated request — consult the warm tenant cache first. The
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

    Reuses ``verify_api_key_full`` — the same authoritative lookup
    ``AuthMiddleware`` uses on a cache miss — returning the (key_id, tenant_id,
    user_id, owner_is_admin) tuple. We also warm the in-memory caches on success
    so the next call in this TTL window takes the fast path.

    Returns the tenant_id int (tenant-scoped key) or ``None`` ONLY when the DB
    authoritatively confirms tenant_id IS NULL (genuine admin/global key).

    Raises ``TenantResolutionDenied`` when the key cannot be authoritatively
    resolved — verify returns ``None`` (key vanished / deactivated mid-window),
    an unexpected shape, or the DB is unavailable. Failing closed is mandatory:
    an authenticated key with an unknown tenant must never widen to the
    unrestricted ``'*'`` GUC.
    """
    try:
        from src.db.pg import auth_store
        result = auth_store().verify_api_key_full(raw_key)
    except Exception as exc:
        # DB unavailable / verify raised — cannot confirm admin status, so deny.
        raise TenantResolutionDenied(
            "tenant could not be resolved for an authenticated key "
            "(authoritative lookup unavailable) — denying to preserve tenant isolation"
        ) from exc
    if result is None or not (isinstance(result, tuple) and len(result) == 4):
        # Key not active/valid, or an unexpected return shape — deny.
        raise TenantResolutionDenied(
            "tenant could not be resolved for an authenticated key "
            "(key inactive or lookup returned no row) — denying to preserve tenant isolation"
        )
    key_id, tenant_id, user_id, owner_is_admin = result
    # Read-side escalation guard (ADR-0034, mirrors AuthMiddleware): a user-owned,
    # non-admin key with tenant_id IS NULL is the invalid "unrestricted" state a
    # scoped key must NEVER be in. AuthMiddleware 401s it upstream, but this
    # authoritative path must not diverge — re-applying the guard here means that
    # even on a path that bypassed the middleware we deny instead of widening to
    # the '*' GUC. Raise BEFORE warming caches so the bad state is never cached.
    from src.mcp.middleware import _is_null_tenant_escalation
    if _is_null_tenant_escalation(tenant_id, user_id, owner_is_admin):
        raise TenantResolutionDenied(
            "authenticated key resolved to a non-admin owner with NULL tenant "
            "(escalation state) — denying to preserve tenant isolation"
        )
    # Warm the caches so the rest of this request / TTL window is fast and
    # consistent with the middleware's own population.
    try:
        from src.mcp.middleware import _cache_set, _cache_set_owner, _cache_set_tenant
        _cache_set(raw_key, key_id)
        _cache_set_tenant(raw_key, tenant_id)
        _cache_set_owner(raw_key, user_id, owner_is_admin)
    except Exception:
        pass  # cache warming is best-effort — never block on it
    # tenant_id int → scoped key; None → DB-confirmed admin/global (unrestricted).
    return tenant_id


# ContextVar storage for API key ID — populated by UsageLogMiddleware.
# ContextVar is used instead of threading.local() because asyncio multiplexes
# coroutines in a single thread: a threading.local write in coroutine A is
# shared with coroutine B (same thread), so one request's finally-reset would
# wipe another's value mid-execution → 'default' sentinel crash on
# set_active_version / set_active_profile.  ContextVar gives each coroutine
# its own isolated copy, propagated to worker threads by anyio (if needed).
_api_key_id_var: ContextVar[str] = ContextVar("_api_key_id", default="default")

# ContextVar storage for tenant_id — populated alongside _api_key_id_var
# by UsageLogMiddleware from request.state.tenant_id (ADR-0034 D4.1).
_tenant_id_var: ContextVar[int | None] = ContextVar("_tenant_id", default=None)

# ContextVar storage for the MCP transport session id (#251). Populated by
# UsageLogMiddleware from the ``mcp-session-id`` header before each tool /
# resource call so the per-session version/profile pin (src.mcp.session) is
# keyed by (api_key_id, mcp_session_id) — concurrent Claude Code sessions on
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
    listing) — the flat union ``own ∪ shared`` with optional explicit narrowing.

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

    Pure function (no I/O) — easy to unit-test.

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
    automatically cleared on COMMIT/ROLLBACK — zero pool-leak risk.

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
    is ENABLED but NOT FORCED — PostgreSQL skips all policy evaluation for the
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
        pass  # pgvector <0.8 — silently ignored; supported deploys enforce >=0.8


def _scope_pred(alias: str) -> str:
    """Canonical fail-closed tenant choke-point predicate for Neo4j node *alias*.

    Single source of truth for the Cypher fragment (WG-3t — avoids per-site drift)::

        ($own IS NULL OR (size(<alias>.profile) > 0
                          AND all(__p IN <alias>.profile WHERE __p IN $own OR __p IN $shared)))

    The ``size(...) > 0`` guard closes the F-6 vacuous-truth hole: an empty
    ``profile=[]`` array makes ``all(__p IN [] ...)`` evaluate to TRUE in Cypher
    (universal quantification over the empty set), which would fail-OPEN — letting
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
    profiles or a shared/global profile — so another tenant's private node (which
    also carries the shared base in its ``profile[]``) is denied (its foreign private
    profile fails the ``all(...)``), and a same-name cross-tenant collision
    fail-closes (denied to both).

    ``profile_name`` is a NON-ESCALATING narrowing filter (WG-3t T3 — fixes the
    Neo4j/pgvector split-brain). It can only shrink the visible set *within*
    ``own ∪ shared``; it can never widen it:

    - admin (own=None), no profile_name      → ``own=None`` (unrestricted).
    - admin (own=None), explicit profile      → narrow to ``own=[profile]``, keep ``shared``
      (admin convenience; shared/CE base nodes with [own, base] still visible).
    - tenant, no profile_name                 → full ``(own, shared)`` boundary.
    - tenant, profile_name ∈ own∪shared       → narrow own to ``[profile]``, keep ``shared``
      (nodes that carry [own, base] both remain visible — shared is never stripped).
    - tenant, profile_name ∉ own∪shared       → deny-all (``own=[], shared=[]``);
      a tenant cannot borrow another tenant's profile name to escalate.
    """
    # #251: when the caller omits an explicit profile, inject the per-session
    # pinned default (set via set_active_profile) BEFORE the ADR-0034 tenant
    # narrowing below. The injection is NARROWING-ONLY — the existing tenant
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


# find_examples rerank coefficients — extracted so calibration harness can
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
      - sorts by `toInteger(split(v,'.')[0])` then minor — handles 9.0 < 17.0 correctly
        (lexicographic compare would put '9.0' > '17.0', a Neo4j 5.x gotcha — see
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
    """Session-aware version resolution — 3-tier order per ADR-0029.

    Resolution order (delegated to session.resolve_version_v2):
      1. Explicit *version_arg* after sentinel normalization (auto/default/
         latest/version/any/"" all treated as sentinel → None).
      2. Per-(api_key, mcp_session) in-memory pin (24h TTL since last set; #251).
      3. Latest indexed version via _latest_version() Neo4j query.

    Raises ValueError when all three tiers fail (empty index + no session
    + no explicit version).

    All 24 existing call sites are unchanged — this function's external
    signature is preserved.
    """
    api_key_id = _get_api_key_id()
    mcp_session_id = _get_mcp_session_id()
    return _session.resolve_version_v2(version_arg, api_key_id, session, mcp_session_id)


def _resolve_profile(profile_arg: str | None) -> str | None:
    """Session-aware profile resolution — proposes the pinned default (#251).

    Peer of :func:`_resolve_version`. Delegates to
    ``session.resolve_profile_v2`` with the per-session pin key so a tool that
    omits ``profile_name`` inherits the profile pinned via ``set_active_profile``
    for THIS MCP session. The resolution performs NO authorization — the
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
        to the resource LRU cache (``get_or_compute`` stores unconditionally — a
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
    # (neo4j.Query(timeout=NEO4J_QUERY_TIMEOUT_SECONDS)) — the 600s server-side
    # db.transaction.timeout backstop alone let #273 hang for 19-24h. On timeout
    # `_data_bounded` raises OrmQueryTimeout (clean English, no Cypher leaked).
    # `_resolve_model` returns `str`, and its callers run under a plain `@offload`
    # (model_inspect) / the MCP resource handler — NEITHER catches OrmQueryTimeout,
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

            # Ranking tiers — see docs/adr/0013:
            # T1 is_def_rank: m.is_definition flag (post-reindex, authoritative).
            # T2 field_count: Field nodes declared on this model in this module —
            #                 100% accurate signal pre-reindex on real data
            #                 (defining module always has the most fields).
            # T3 dependents : DEPENDS_ON inbound on Module (manifest depends).
            # T4 edition    : community < enterprise < viindoo < oca < custom.
            # T5 mod_name   : alphabetical tiebreak — eliminates arbitrary order.
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
            # _list_extenders — `NOT is_definition` — so the summary "... and N more"
            # count and the paginated extenders total are always equal. The previous
            # `layers[1:]` assumed exactly one definition row on top, which:
            #   - under-counts by 1 when the definition node is out of scope (a pure
            #     _inherit model whose top row is itself an extender), and
            #   - over-counts when >1 module carries is_definition=true.
            # `base` (the "Defined in" line) stays as the top-ranked row (ADR-0013);
            # in the rare no-definition-row case it is also a NOT-is_definition row,
            # so it appears in both sections — identical to what _list_extenders
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
            # scoping is intentionally NOT applied here — the summary count reflects
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
                # blank the whole summary — degrade to the flat own-model count
                # rather than failing the model overview. This inner timeout is
                # recoverable; the OUTER ranking/parents `_data_bounded` timeout is
                # not, and still returns a clean string via the function-level except
                # below (#279/#284).
                fields_count = base["fields_count"]
                methods_count = base["methods_count"]

            # DISTINCT on p.name only — the same parent (e.g. mail.thread) is reachable
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
        # timeout message (ADR-0023 — no Cypher leaked) rather than letting the
        # exception escape to FastMCP as a protocol-level isError. The 30s driver
        # timeout itself is the load-bearing #279 protection.
        #
        # #284 review: the odoo:// model resource caches this return value via
        # ResourceCache.get_or_compute, which stores UNCONDITIONALLY. Returning
        # the (transient) timeout string there would poison the LRU entry for the
        # full TTL — a 30s blip becomes a 300s stale-error outage on that URI.
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
        # observable — no double-count (the bounded pools have their own metric).
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
    depth-0) so the caller appends nothing — output for own entities stays
    byte-identical to the pre-inherited behaviour.

    Single source for the wording across :func:`_list_fields._fmt_field_row` and
    :func:`_list_methods._fmt_method` (FIX-6, review #283). The two detail
    renderers (:func:`_render_inherited_field` / :func:`_render_inherited_method`)
    use a distinct capitalised ``├─`` branch grammar and are intentionally NOT
    routed through this helper. ``edge_kind == 'delegates'`` only ever occurs on
    the FIELD path — methods are INHERITS-only (GAP-1), so the method caller
    always lands on the ``inherited from`` branch.
    """
    if not owner_model or owner_model == model:
        return None
    if edge_kind == "delegates":
        # GAP-5: `_inherits` delegation gives the child the owner's FIELDS ONLY,
        # stored in the owner's SEPARATE table via the FK — signal it explicitly
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
        f"├─ Related:  {inh.get('related') or '—'}",
    ]
    if inh.get("comodel_name"):
        lines.append(f"├─ Comodel:  {inh['comodel_name']}")
    # A2-followup: label + help parity with own-field detail (V3 fix — ADR-0023).
    # Populated after reindex; absent on pre-reindex graphs — omit gracefully.
    if inh.get("string"):
        lines.append(f"├─ Label:    {inh['string']}")
    if inh.get("help"):
        lines.append(f"├─ Help:     {inh['help']}")
    _eff_ro = inh.get("effective_readonly")
    if _eff_ro is not None:
        lines.append(f"├─ Readonly: {'Yes' if _eff_ro else 'No'}")
        lines.append(
            "│   └─ note: readonly reflects the Python field definition only "
            "(view-level/states/attrs readonly not captured)"
        )
    # Provenance branch - distinguish INHERITS mixin vs _inherits delegation.
    # GAP-5: delegation gives the child the owner's FIELDS ONLY, stored in the
    # owner's SEPARATE table via the FK — signal that explicitly so the AI client
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

        # 5-tier ranking via m_node proxy — see docs/adr/0013.
        # Routed through `_data_bounded` so a tx-timeout on a dense field ranking
        # becomes OrmQueryTimeout (clean English, no Cypher leaked). The PRIMARY
        # query is intentionally NOT caught here — it propagates so the owning
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
                # WI-1 (#238): magic fields are ORM-managed — never writable.
                "├─ Readonly: Yes",
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
        # Inherited fallback: the flat exact-match on the child model MISSED and
        # it is not a magic field. Walk INHERITS|DELEGATES_TO (depth 1-3) to find
        # the field on a mixin (e.g. `res_ref` declared on a mixin model, not on
        # `viin.approval.request` itself). Keeps the exact-first fast path —
        # native fields never pay the BFS cost.
        #
        # from_module semantics (V2 fix): when from_module is set we still run the
        # inherited fallback — the field may be declared on a mixin model that
        # BELONGS to from_module (e.g. `abstract.approval.request.fields` lives in
        # module `viin_approval`). After the BFS we post-filter: only surface the
        # inherited hit if its declaring module matches from_module. If the module
        # doesn't match we keep the "not found" path — the user asked specifically
        # for that module and the field isn't there.
        # FIX-1 (review #283): _resolve_field_inherited is bounded + tx-timeout-
        # mapped. The owning tool handlers now wrap this in @offload_neo4j (PR-1),
        # which catches OrmQueryTimeout at the boundary; the tool-path catch here
        # still returns the clean ADR-0023 string directly (and the resource path
        # re-raises so the transient body is never cached — see below).
        try:
            with _get_driver().session() as session:
                inh = _resolve_field_inherited(
                    model_name, field_name, odoo_version, session, profile_name
                )
        except OrmQueryTimeout as exc:
            # Resource path (_reraise_timeout=True): re-raise so the transient
            # body is never cached (the field resource handler records the metric
            # once and returns the message uncached). Tool path: returning the
            # clean string here means this inherited-fallback timeout is NOT
            # counted by @offload_neo4j (the decorator only counts a RAISED
            # OrmQueryTimeout); adding that metric is Phase 3 / PR-3 (design M1).
            if _reraise_timeout:
                raise
            return exc.user_message
        if inh is not None:
            if from_module is None or inh.get("module") == from_module:
                return _render_inherited_field(model_name, field_name, odoo_version, inh)
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
    # WI-1 (#238): writability signal. Graceful degradation — pre-reindex
    # graphs lack effective_readonly (None); omit the line rather than print a
    # misleading "Readonly: No". Only render once the field has been reindexed.
    _eff_ro = base_f.get("effective_readonly")
    if _eff_ro is not None:
        lines.append(f"├─ Readonly: {'Yes' if _eff_ro else 'No'}")
        lines.append(
            "│   └─ note: readonly reflects the Python field definition only "
            "(view-level/states/attrs readonly not captured)"
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

    Methods are inherited via INHERITS only (Python MRO) — ``_inherits``
    delegation NEVER carries methods (GAP-1) — so the provenance is always
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
        decs = ", ".join(mth.get("decorators") or []) or "—"
        repo_str = f"[{r['repo']}] " if r.get("repo") else ""
        return f"{repo_str}{r['module_name']} — {super_info} — decorators: {decs}"

    if owner_chain:
        # GAP-3: render the REAL override chain on the owner model — every module
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
        # Defensive fallback: no owner chain supplied — render the single owner
        # entry from `inh` (pre-GAP-3 behaviour, but no longer the normal path).
        lines.append("├─ Override chain (1):")
        super_info = "✓ calls super()" if inh.get("has_super_call") else "✗ no super()"
        decs = ", ".join(inh.get("decorators") or []) or "—"
        repo_str = f"[{inh['repo']}] " if inh.get("repo") else ""
        lines.append(
            f"│   └─ {repo_str}{inh.get('module') or '?'} — {super_info}"
            f" — decorators: {decs}"
        )
    # FIX-3 (review #283, symmetric to _render_inherited_field): the method NODE
    # and its whole override chain live on `owner` (the mixin), not the child
    # `model_name`. find_override_point + impact_analysis flat-match on the
    # declaring model, so hints keyed by `{child}.{method}` return EMPTY — wrongly
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
    # tool-path method-detail timeout is therefore not yet counted in the metric —
    # the deferred M2 gap, PR-3 / issue #287 — matching _resolve_field's M1.)
    try:
        with _get_driver().session() as session:
            odoo_version = _resolve_version(odoo_version, session)

            # 5-tier ranking via m_node proxy — see docs/adr/0013
            records = _method_override_chain(
                session, model_name, method_name, odoo_version, profile_name
            )

        if not records:
            # Inherited fallback (symmetric to _resolve_field): the flat exact-match
            # on the child model MISSED. Walk INHERITS (depth 1-3) to find the method
            # on a mixin (e.g. `_compute_res_ref` declared on a mixin model, not on
            # `viin.approval.request` itself). Methods are inherited via INHERITS only
            # — `_inherits` delegation never carries methods (GAP-1). Exact-first fast
            # path is preserved — own methods never pay the BFS cost.
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
            return (
                f"Method '{method_name}' not found on model"
                f" '{model_name}' in Odoo {odoo_version}."
            )
    except OrmQueryTimeout as exc:
        # Consistent with _resolve_field / _resolve_model. Resource path
        # (_reraise_timeout=True): re-raise so the transient body is never cached
        # (propagates out of get_or_compute before the put; the method resource
        # handler records the metric once + returns it uncached). Tool path
        # (default): return the clean ADR-0023 string directly. The tool-path
        # method-detail timeout metric is the deferred M2 gap (PR-3 / issue #287),
        # matching _resolve_field's M1.
        if _reraise_timeout:
            raise
        return exc.user_message

    base_mth = records[0]["mth"]
    lines = [
        f"{model_name}.{method_name}() (Odoo {odoo_version})",
    ]
    # B1: render signature and convention_kind from the authoritative (first-ranked) entry.
    if base_mth.get("signature"):
        lines.append(f"├─ Signature:   ({base_mth['signature']})")
    if base_mth.get("convention_kind"):
        lines.append(f"├─ Convention:  {base_mth['convention_kind']}")
    # B2: render docstring first line (A2a — populated after reindex; absent pre-reindex).
    if base_mth.get("docstring"):
        first_line = base_mth["docstring"].strip().splitlines()[0][:120]
        lines.append(f"├─ Docstring:   {first_line}")
    chain_total = len(records)
    lines.append(f"├─ Override chain ({chain_total}):")

    def _fmt_override(r: dict) -> str:
        mth = r["mth"]
        super_info = "✓ calls super()" if mth.get("has_super_call") else "✗ no super()"
        decs = ", ".join(mth.get("decorators") or []) or "—"
        repo_str = f"[{r['repo']}] " if r.get("repo") else ""
        return f"{repo_str}{r['module_name']} — {super_info} — decorators: {decs}"

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
    # ADR-0023 §1.2: render via the shared helper so the LAST row — including a
    # "... and N more" disclosure row — always gets the └─ connector. Parent
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
    # The three view queries are routed through `_single_bounded` / `_data_bounded`
    # so a tx-timeout on a dense view-inheritance chain becomes OrmQueryTimeout
    # (clean English, no Cypher leaked) instead of escaping as a raw ClientError.
    # There is no internal catch here: the raise propagates out so the owning
    # entity_lookup handler (now @offload_neo4j) records the metric + returns the
    # clean string (tool path), or the view resource handler records + returns it
    # UNCACHED (resource path). The `_reraise_timeout` parameter exists for
    # signature parity with _resolve_model/_resolve_field/_resolve_method (the
    # resource render passes it); because nothing here converts the timeout to a
    # string, both paths already propagate identically.
    _ = _reraise_timeout  # parity-only flag; the timeout always propagates here.
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
    _query_vec=None,
    _use_lexical: bool = False,
) -> str:
    # _query_vec: when the async tool wrapper has already embedded the query off
    # the event loop (#227), it passes the vector here so this blocking body can
    # run inside asyncio.to_thread without re-embedding. When None (sync tests,
    # entity_lookup, CLI), we embed synchronously as before — never on a loop.
    if not query.strip():
        # ADR-0023 §2: tool output must be English-only.
        return (
            "find_examples: empty query — provide a description of the"
            " feature you want to find\nFound 0 results\n"
        )

    from src.embedding.instructions import INSTRUCT_NL_TO_CODE

    driver = _driver or _get_driver()

    with driver.session() as session:
        if odoo_version in ("auto", "latest"):
            odoo_version = _resolve_version("auto", session)

    selected_types = [t for t in (chunk_types or []) if t in VALID_CHUNK_TYPES]

    # Issue #255 (WI-7, Decision E): literal-first for style-only queries.
    # Only engage when ALL requested chunk_types are style types AND the query is
    # a verbatim CSS/SCSS token.  NL queries and non-style chunk_types are untouched.
    style_only = bool(selected_types) and set(selected_types) <= STYLE_CHUNK_TYPES
    want_literal = style_only and is_literal_token(query)

    # MAJOR-1 (issue #255 review): defer the embedder fetch until we know a literal
    # query actually needs ANN backfill. A pure-literal style path never fetches the
    # embedder, so a literal lookup survives an init-time embedder failure
    # (EmbedderDimMismatch, config error) — symmetric with _find_style_override.
    embedder = _embedder
    query_vec: list[float] | None = None
    if not want_literal:
        # Standard path: embed now (sync body / pre-embedded async path).
        if _query_vec is not None:
            query_vec = _query_vec
        elif _use_lexical:
            # Caller already tried and failed to embed (async wrapper embed-failure
            # path, or explicit lexical-only mode for testing).  Skip embed entirely
            # and fall through to the lexical fallback below.
            pass
        else:
            try:
                if embedder is None:
                    embedder = _get_embedder()
            except Exception:
                # Embedder init failed — fall back to lexical keyword search.
                _use_lexical = True
            if not _use_lexical:
                try:
                    # Cap the query to the token budget before INSTRUCT so a giant
                    # paste cannot blow the embedder context (#227, sync path).
                    capped = _cap_query_text(embedder, query)
                    instruct = getattr(embedder, "query_instruction", INSTRUCT_NL_TO_CODE)
                    query_vec = embedder.embed([instruct + capped])[0]
                except Exception:
                    # Embed failed — fall back to lexical keyword search.
                    _use_lexical = True
    else:
        # Literal style token: carry pre-embedded vec (may be None from async wrapper).
        query_vec = _query_vec

    # C3 (WI-4): fail-closed tenant filter at the pgvector ANN layer. The
    # Neo4j rerank only deprioritises non-allowed modules — it does NOT drop
    # their chunks — so isolation MUST be enforced here, in the SQL, before
    # rows are fetched. allowed=None -> admin/unrestricted (no clause);
    # allowed=[] -> deny-all (ANY('{}') matches nothing). global sentinel rows
    # (profile_name='__global__') are excluded when scoped — fail-closed.
    allowed = _effective_allowed(profile_name)
    prof_sql = "" if allowed is None else " AND profile_name = ANY(%s)"

    # Extra columns needed by find_examples (beyond the base 6 in _literal_style_lookup).
    _STYLE_EXTRA_COLS = ["model_name", "line_start", "repo", "repo_id"]

    # Use injected connection (test path) or check out from pool (production).
    _pg_ctx = nullcontext(_pg_conn) if _pg_conn is not None else _checkout_pg()
    with _pg_ctx as pg:
        # RLS wiring (WI-7 / ADR-0034 A2): set app.allowed_profiles GUC for
        # the duration of this read transaction. Armed-but-dormant: owner
        # bypass means this is a no-op until ops enables FORCE RLS.
        with _rls_read_tx(pg, allowed):
            with pg.cursor() as cur:
                # (0) LEXICAL FALLBACK (issue #264, WI-9): when the embedder is
                # unavailable (embedder init or embed call failed), run a
                # keyword ILIKE search against entity_name.  Results are labelled
                # match: lexical to signal degraded quality.  Tenant choke
                # (ADR-0034) is preserved: allowed is passed through unchanged.
                if _use_lexical:
                    lex_rows = lexical_example_lookup(
                        cur, query, odoo_version, allowed,
                        min(limit, FIND_EXAMPLES_ANN_LIMIT),
                        selected_types,
                        extra_cols=_STYLE_EXTRA_COLS,
                    )
                    for r in lex_rows:
                        r.setdefault("model_name", None)
                        r.setdefault("line_start", None)
                        r.setdefault("repo", None)
                        r.setdefault("repo_id", None)
                    # Return with a degraded banner so agents know quality is lower.
                    if not lex_rows:
                        return (
                            f'find_examples: "{query}" ({odoo_version})\n'
                            "Found 0 results  "
                            "[degraded: embedder unavailable — lexical search returned nothing]\n"
                        )
                    header = (
                        f'find_examples: "{query}" ({odoo_version})\n'
                        f"Found {len(lex_rows)} results  "
                        "[degraded: embedder unavailable — lexical keyword match]\n"
                    )
                    sep = "─" * 41
                    lines = [header]
                    for i, chunk in enumerate(lex_rows, 1):
                        entity = f'[{chunk["module"]}] {chunk["entity_name"]}'
                        if chunk["model_name"] and chunk["chunk_type"] == "view":
                            entity += f" (model: {chunk['model_name']})"
                        chunk_label = chunk["chunk_type"]
                        if chunk["chunk_idx"] > 0:
                            chunk_label += f" chunk {chunk['chunk_idx'] + 1}"
                        lines.append(sep)
                        lines.append(
                            f"#{i} · score - · match: lexical"
                            f" · {chunk_label} · {entity}"
                        )
                        file_path = _portable_path(
                            chunk["file_path"] or "",
                            repo=chunk.get("repo"), module=chunk.get("module"),
                        )
                        repo_label = _repo_url_for_id(chunk.get("repo_id")) or chunk.get("repo")
                        repo_pfx = f"[{repo_label}] " if repo_label else ""
                        line_sfx = (
                            f":{chunk['line_start']}"
                            if chunk.get("line_start") is not None else ""
                        )
                        lines.append(f"   File: {repo_pfx}{file_path}{line_sfx}")
                        lines.append("   ┌" + "─" * 42)
                        for line in chunk["content"].splitlines():
                            lines.append(f"   │ {line}")
                        lines.append("   └" + "─" * 42)
                        lines.append("")
                    lines.append(format_next_step([
                        f"suggest_pattern(intent='{query}', odoo_version='{odoo_version}')"
                        " for curated patterns",
                    ]))
                    return "\n".join(lines)

                # (1) LITERAL-FIRST for style-only queries (issue #255 WI-7).
                literal_rows: list[dict] = []
                if want_literal:
                    literal_rows = _literal_style_lookup(
                        cur, query, odoo_version, allowed,
                        min(limit, FIND_EXAMPLES_ANN_LIMIT),
                        extra_cols=_STYLE_EXTRA_COLS,
                    )
                    # Fill in missing keys expected by the render loop.
                    for r in literal_rows:
                        r.setdefault("model_name", None)
                        r.setdefault("line_start", None)
                        r.setdefault("repo", None)
                        r.setdefault("repo_id", None)

                # (2) ANN: for non-literal paths, or as backfill when literal under-fills.
                remaining = min(limit, FIND_EXAMPLES_ANN_LIMIT) - len(literal_rows)
                ann_rows: list[dict] = []
                if remaining > 0 and query_vec is None and want_literal:
                    # Literal style path — attempt lazy embed for backfill. Fetch
                    # the embedder here (not at the top) so an embedder failure
                    # degrades to literal-only instead of erroring the whole call.
                    try:
                        if embedder is None:
                            embedder = _get_embedder()
                        capped = _cap_query_text(embedder, query)
                        instruct = getattr(embedder, "query_instruction", INSTRUCT_NL_TO_CODE)
                        query_vec = embedder.embed([instruct + capped])[0]
                    except Exception:
                        query_vec = None  # degrade to literal-only

                if remaining > 0 and query_vec is not None:
                    if selected_types:
                        placeholders = ",".join(["%s"] * len(selected_types))
                        _set_iterative_scan(cur)  # HNSW recall mitigation (AC5/ADR-0047)
                        params = [query_vec, odoo_version, *selected_types]
                        if allowed is not None:
                            params.append(allowed)
                        params += [query_vec, remaining if want_literal
                                   else min(limit, FIND_EXAMPLES_ANN_LIMIT)]
                        cur.execute(
                            f"""SELECT chunk_type, module, entity_name, model_name, file_path,
                                       chunk_idx, content, 1 - (vec <=> %s::vector) AS cosine,
                                       line_start, repo, repo_id
                                FROM embeddings
                                WHERE odoo_version = %s AND chunk_type IN ({placeholders}){prof_sql}
                                ORDER BY vec <=> %s::vector LIMIT %s""",
                            params,
                        )
                    else:
                        _set_iterative_scan(cur)  # HNSW recall mitigation (AC5/ADR-0047)
                        params = [query_vec, odoo_version]
                        if allowed is not None:
                            params.append(allowed)
                        params += [query_vec, min(limit, FIND_EXAMPLES_ANN_LIMIT)]
                        cur.execute(
                            f"""SELECT chunk_type, module, entity_name, model_name, file_path,
                                      chunk_idx, content, 1 - (vec <=> %s::vector) AS cosine,
                                      line_start, repo, repo_id
                               FROM embeddings WHERE odoo_version = %s{prof_sql}
                               ORDER BY vec <=> %s::vector LIMIT %s""",
                            params,
                        )
                    ann_rows = [
                        dict(chunk_type=r[0], module=r[1], entity_name=r[2], model_name=r[3],
                             file_path=r[4], chunk_idx=r[5], content=r[6], cosine=float(r[7]),
                             line_start=r[8], repo=r[9], repo_id=r[10], match="semantic")
                        for r in cur.fetchall()
                    ]

    # (3) MERGE + DEDUP for literal-first paths.
    if want_literal:
        seen = {
            (r["chunk_type"], r["module"], r["file_path"], r["entity_name"], r["chunk_idx"])
            for r in literal_rows
        }
        raw = literal_rows + [
            r for r in ann_rows
            if (r["chunk_type"], r["module"], r["file_path"], r["entity_name"], r["chunk_idx"])
            not in seen
        ]
        raw = raw[:min(limit, FIND_EXAMPLES_ANN_LIMIT)]
    else:
        raw = ann_rows  # standard ANN path, no literal rows

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
            f" WHERE {_scope_pred('m')}"
            f" WITH m, name"
            f" OPTIONAL MATCH (dep)-[:{REL_DEPENDS_ON}]->(m)"
            f" RETURN name, count(dep) AS dependents",
            names=module_names, v=odoo_version, **_scope(profile_name),
        ).data()
        dependents_map = {r["name"]: r["dependents"] for r in dep_rows}

        in_chain_set: set[str] = set()
        if context_module and module_names:
            chain_rows = session.run(
                "MATCH (ctx:Module {name: $ctx, odoo_version: $v})"
                " -[:DEPENDS_ON*1..]->(tgt:Module)"
                " WHERE tgt.name IN $names"
                f" AND {_scope_pred('ctx')}"
                " RETURN DISTINCT tgt.name AS name",
                ctx=context_module, v=odoo_version, names=module_names,
                **_scope(profile_name),
            ).data()
            in_chain_set = {r["name"] for r in chain_rows}

    # M1 fix: literal rows have cosine=None — guard against TypeError in score math.
    # Assign a floor score with a small epsilon to preserve SQL ORDER BY order so
    # literal rows always sort above semantic hits (LITERAL_RANK_FLOOR > max cosine*rerank).
    n_lit = sum(1 for c in raw if c.get("cosine") is None)
    lit_idx = 0
    for chunk in raw:
        dependents = dependents_map.get(chunk["module"], 0)
        if chunk.get("cosine") is None:
            # Literal hit: floor score preserves SQL order, ranks above all semantic.
            chunk["score"] = _LITERAL_RANK_FLOOR + (n_lit - lit_idx) * _LITERAL_RANK_EPS
            lit_idx += 1
        else:
            chunk["score"] = chunk["cosine"] * (1 + _RERANK_LOG_COEFF * math.log(dependents + 1))
        if chunk["module"] in in_chain_set:
            chunk["score"] += _RERANK_CHAIN_BOOST

    reranked = sorted(raw, key=lambda c: c["score"], reverse=True)[:limit]

    # G2: disclose ANN/literal candidate counts so callers know the search pool size.
    if want_literal:
        n_lit_shown = sum(1 for c in reranked if c.get("cosine") is None)
        n_sem_shown = len(reranked) - n_lit_shown
        ann_note = f"{n_lit_shown} literal + {n_sem_shown} semantic"
    else:
        ann_used = min(limit, FIND_EXAMPLES_ANN_LIMIT)
        if ann_used >= FIND_EXAMPLES_ANN_LIMIT:
            # User requested limit >= ANN cap: the search pool is hard-capped.
            ann_note = (
                f"Note: ANN search capped at {FIND_EXAMPLES_ANN_LIMIT} candidates"
                " — results beyond this pool are not considered"
            )
        else:
            # User requested fewer results than the ANN cap allows.
            ann_note = (
                f"showing {len(reranked)} of up to {ann_used} semantic candidates"
                f" — increase `limit` (max {FIND_EXAMPLES_ANN_LIMIT}) for broader search"
            )
    header = (
        f'find_examples: "{query}" ({odoo_version})\n'
        f"Found {len(reranked)} results  [{ann_note}]\n"
    )
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
        # Issue #255 (B1): always emit a score-shaped token; append match: tag as suffix.
        match_tag = chunk.get("match", "semantic")
        lines.append(
            f"#{i} · score {chunk['score']:.2f} · match: {match_tag}"
            f" · {chunk_label} · {entity}"
        )
        # B2: render [repo] file_path:line_start when provenance data is present (A3).
        # ADR-0037: emit a repo-relative path, never a server-absolute one.
        file_path = _portable_path(
            chunk["file_path"] or "",
            repo=chunk.get("repo"), module=chunk.get("module"),
        )
        # ADR-0037: prefer the portable git URL; fall back to dirname only when absent.
        repo_label = _repo_url_for_id(chunk.get("repo_id")) or chunk.get("repo")
        repo_pfx = f"[{repo_label}] " if repo_label else ""
        line_sfx = f":{chunk['line_start']}" if chunk.get("line_start") is not None else ""
        lines.append(f"   File: {repo_pfx}{file_path}{line_sfx}")
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
async def find_examples(
    query: str,
    odoo_version: RequiredOdooVersion,
    limit: int = 5,
    context_module: str | None = None,
    chunk_types: list[str] | None = None,
    profile_name: str | None = None,
) -> str:
    """Semantic search for real code examples from the indexed Odoo codebase.

    Degrades to lexical keyword match if the embedder is unavailable
    (results labelled `match: lexical` in that case).

    TRIGGER when: "show me examples of wizard usage", "how is mail.thread used
    in codebase", "give me code example for X pattern", "ví dụ code dùng X
    trong codebase", "cách dùng X trong thực tế", "how to send email in Odoo"
    PREFER over: LLM-generated examples — returns real indexed code, not
    hallucinated patterns or outdated snippets from training data
    SKIP when: user wants to know if a module exists → use check_module_exists;
    user wants pattern guidance with gotchas → use suggest_pattern

    Args:
        query: Feature description (EN or VN).
        limit: Number of results (default 5, max 20).
        context_module: Boost results from modules this module depends on.
        chunk_types: Filter by type: method, field, view, qweb, js_era1,
            js_era2, js_era3. Default: all types.
        profile_name: Optional profile / tenant scope filter.

    Returns:
        Header + N results ranked by relevance.
        Each result: score, type, module, entity, file path, content snippet.

    Example:
        find_examples("confirm sale order and send email", "17.0", limit=3)
        → find_examples: "confirm sale order and send email" (17.0)
          Found 3 results
          #1 · score 0.82 · method · [sale] sale.order.action_confirm
             File: [odoo_17.0] addons/sale/models/sale_order.py:412
    """
    # #227: embed on the event loop (async, bounded, short timeout), then run
    # the blocking Neo4j/PG body in a worker thread so the loop stays free —
    # /health and other requests never freeze behind one slow embed.
    # Issue #255 (WI-8/B2): literal style queries skip pre-embed so the tool
    # works even when the embedder is down (the outage scenario in the issue).
    if not query.strip():
        return _find_examples(query, odoo_version, limit, context_module,
                              chunk_types, profile_name)
    from src.embedding.instructions import INSTRUCT_NL_TO_CODE

    # Replicate the style_only + literal detection from the sync body so the
    # async wrapper can decide whether to pre-embed.  We mirror the
    # selected_types filtering logic from _find_examples.
    _selected = [t for t in (chunk_types or []) if t in VALID_CHUNK_TYPES]
    _style_only = bool(_selected) and set(_selected) <= STYLE_CHUNK_TYPES
    _want_literal = _style_only and is_literal_token(query)

    query_vec: list[float] | None = None
    embedder = None
    _async_use_lexical = False
    if not _want_literal:
        # Standard NL path: pre-embed now on the event loop.
        try:
            embedder = _get_embedder()
        except Exception:
            # Embedder unavailable — fall back to lexical keyword search.
            _async_use_lexical = True
        if not _async_use_lexical:
            try:
                instruct = getattr(embedder, "query_instruction", INSTRUCT_NL_TO_CODE)
                query_vec = await _embed_query(embedder, instruct, query)
            except EmbedOverloaded as e:
                # Overloaded is a transient server condition, not an outage —
                # return the clean message rather than a degraded lexical result
                # (retrying momentarily is better than lower-quality output).
                return f"find_examples: {e}\nFound 0 results\n"
            except Exception:
                # Embed failed (timeout, model not loaded, etc.) — fall back to
                # lexical keyword search so the agent still gets useful results.
                _async_use_lexical = True
    else:
        # Literal style token: best-effort embedder fetch for ANN backfill.
        # Failure here is non-fatal — sync body will use literal-only results.
        try:
            embedder = _get_embedder()
        except Exception:
            embedder = None
    return await asyncio.to_thread(
        _find_examples,
        query, odoo_version, limit, context_module, chunk_types, profile_name,
        _embedder=embedder, _query_vec=query_vec, _use_lexical=_async_use_lexical,
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
        # G5 (#276): every heavy read below runs through _data_bounded /
        # _single_bounded, which wrap the Cypher in neo4j.Query(timeout=...) so a
        # runaway traversal (TARGETS_MODEL fan-out, DEPENDS_ON / BOUND_TO chains)
        # surfaces as a bounded OrmQueryTimeout instead of a zombie transaction.
        _label = f"impact analysis for '{entity_name}' (Odoo {odoo_version})"
        if entity_type == "field":
            exists = _single_bounded(
                session,
                "MATCH (f:Field {name: $fn, model: $mn, odoo_version: $v}) "
                f"WHERE {_scope_pred('f')} "
                "RETURN count(f) AS c",
                _label,
                fn=member_name, mn=model_name, v=odoo_version,
                **_scope(profile_name),
            )["c"]
            if not exists:
                return (
                    f"Entity '{entity_name}' not found in Odoo {odoo_version}."
                )
        elif entity_type == "method":
            exists = _single_bounded(
                session,
                "MATCH (mth:Method {name: $mn, model: $model, odoo_version: $v}) "
                f"WHERE {_scope_pred('mth')} "
                "RETURN count(mth) AS c",
                _label,
                mn=member_name, model=model_name, v=odoo_version,
                **_scope(profile_name),
            )["c"]
            if not exists:
                return (
                    f"Entity '{entity_name}' not found in Odoo {odoo_version}."
                )
        else:  # model
            exists = _single_bounded(
                session,
                "MATCH (m:Model {name: $mn, odoo_version: $v}) "
                "WHERE coalesce(m.unresolved, false) = false "
                "AND m.module <> '__unresolved__' "
                f"AND {_scope_pred('m')} "
                "RETURN count(m) AS c",
                _label,
                mn=model_name, v=odoo_version, **_scope(profile_name),
            )["c"]
            if not exists:
                return (
                    f"Entity '{entity_name}' not found in Odoo {odoo_version}."
                )

        # ------------------------------------------------------------------ #
        # Query 2: views targeting model (DISTINCT to avoid TARGETS_MODEL fan-out)
        # ------------------------------------------------------------------ #
        views = _data_bounded(session, f"""
            MATCH (m:Model {{name: $mn, odoo_version: $v}})<-[:{REL_TARGETS_MODEL}]-(view:View)
            WHERE ($own IS NULL OR (size(view.profile) > 0
                   AND all(__p IN view.profile WHERE __p IN $own OR __p IN $shared)))
            RETURN DISTINCT view.xmlid AS xmlid, view.module AS module
            ORDER BY view.module, view.xmlid
        """, _label, mn=model_name, v=odoo_version, **_scope(profile_name))

        # ------------------------------------------------------------------ #
        # Query 3: methods on this model (with super call filter for field;   #
        #          all overrides for method entity_type)                       #
        # ------------------------------------------------------------------ #
        if entity_type == "field":
            methods = _data_bounded(session, """
                MATCH (mth:Method {model: $mn, odoo_version: $v})
                WHERE mth.has_super_call = true
                AND ($own IS NULL OR (size(mth.profile) > 0
                     AND all(__p IN mth.profile WHERE __p IN $own OR __p IN $shared)))
                RETURN DISTINCT mth.name AS name, mth.module AS module
                ORDER BY mth.module, mth.name
            """, _label, mn=model_name, v=odoo_version, **_scope(profile_name))
        elif entity_type == "method":
            methods = _data_bounded(session, """
                MATCH (mth:Method {name: $mn2, model: $mn, odoo_version: $v})
                WHERE ($own IS NULL OR (size(mth.profile) > 0
                       AND all(__p IN mth.profile WHERE __p IN $own OR __p IN $shared)))
                RETURN DISTINCT mth.name AS name, mth.module AS module
                ORDER BY mth.module
            """, _label, mn2=member_name, mn=model_name, v=odoo_version,
                **_scope(profile_name))
        else:  # model
            methods = _data_bounded(session, """
                MATCH (mth:Method {model: $mn, odoo_version: $v})
                WHERE ($own IS NULL OR (size(mth.profile) > 0
                       AND all(__p IN mth.profile WHERE __p IN $own OR __p IN $shared)))
                RETURN DISTINCT mth.name AS name, mth.module AS module
                ORDER BY mth.module, mth.name
            """, _label, mn=model_name, v=odoo_version, **_scope(profile_name))

        # ------------------------------------------------------------------ #
        # Query 4: JS patches on components bound to this model               #
        # ------------------------------------------------------------------ #
        js_patches = _data_bounded(session, """
            MATCH (m:Model {name: $mn, odoo_version: $v})<-[:BOUND_TO]-(comp:OWLComp)
                  <-[:PATCHES]-(jp:JSPatch)
            WHERE ($own IS NULL OR (size(jp.profile) > 0
                   AND all(__p IN jp.profile WHERE __p IN $own OR __p IN $shared)))
            RETURN DISTINCT jp.target AS target, jp.patch_name AS patch_name,
                   jp.module AS module, jp.era AS era
            ORDER BY jp.module, jp.target
        """, _label, mn=model_name, v=odoo_version, **_scope(profile_name))

        # ------------------------------------------------------------------ #
        # Query 5: dependent modules of all modules defining this model       #
        # ------------------------------------------------------------------ #
        dep_modules = _data_bounded(session, f"""
            MATCH (m:Model {{name: $mn, odoo_version: $v}})-[:DEFINED_IN]->(defmod:Module)
                  <-[:{REL_DEPENDS_ON}]-(depmod:Module)
            WHERE ($own IS NULL OR (size(depmod.profile) > 0
                   AND all(__p IN depmod.profile WHERE __p IN $own OR __p IN $shared)))
            RETURN DISTINCT depmod.name AS dep_name
            ORDER BY depmod.name
        """, _label, mn=model_name, v=odoo_version, **_scope(profile_name))

        # For model entity_type: also collect defining modules as "extensions"
        if entity_type == "model":
            def_modules = _data_bounded(session, """
                MATCH (m:Model {name: $mn, odoo_version: $v})-[:DEFINED_IN]->(mod:Module)
                WHERE ($own IS NULL OR (size(m.profile) > 0
                       AND all(__p IN m.profile WHERE __p IN $own OR __p IN $shared)))
                RETURN DISTINCT m.module AS module_name
                ORDER BY m.module
            """, _label, mn=model_name, v=odoo_version, **_scope(profile_name))
        else:
            def_modules = []

        # ------------------------------------------------------------------ #
        # Query 6 (field only): methods that USES_FIELD / DEPENDS_ON_FIELD    #
        # Traverses A2d edges — populated after reindex; empty pre-reindex.   #
        # ------------------------------------------------------------------ #
        uses_field_methods: list[dict] = []
        depends_on_field_methods: list[dict] = []
        if entity_type == "field":
            uses_field_methods = _data_bounded(
                session,
                f"""
                MATCH (mth:Method {{odoo_version: $v}})
                      -[:{REL_USES_FIELD}]->(f:Field {{name: $fn, model: $mn, odoo_version: $v}})
                WHERE ($own IS NULL OR (size(mth.profile) > 0
                       AND all(__p IN mth.profile WHERE __p IN $own OR __p IN $shared)))
                RETURN DISTINCT mth.name AS name, mth.model AS model, mth.module AS module
                ORDER BY mth.module, mth.model, mth.name
                """,
                _label,
                fn=member_name, mn=model_name, v=odoo_version,
                **_scope(profile_name),
            )
            depends_on_field_methods = _data_bounded(
                session,
                f"""
                MATCH (mth:Method {{odoo_version: $v}})
                      -[:{REL_DEPENDS_ON_FIELD}]->(f:Field {{name: $fn, model: $mn,
                                                              odoo_version: $v}})
                WHERE ($own IS NULL OR (size(mth.profile) > 0
                       AND all(__p IN mth.profile WHERE __p IN $own OR __p IN $shared)))
                RETURN DISTINCT mth.name AS name, mth.model AS model, mth.module AS module
                ORDER BY mth.module, mth.model, mth.name
                """,
                _label,
                fn=member_name, mn=model_name, v=odoo_version,
                **_scope(profile_name),
            )

    # ---------------------------------------------------------------------- #
    # Build output tree — G1: all sections capped + disclosure (ADR-0023 §3) #
    # Risk score and counts in labels use the REAL total, not the cap.        #
    # ---------------------------------------------------------------------- #
    view_count = len(views)
    method_count = len(methods)
    js_count = len(js_patches)
    total = view_count + method_count + js_count
    risk = _compute_risk(view_count, method_count, js_count)

    # Helper: append a capped sub-list (items already formatted as strings)
    # Each sub-item is indented under its section header with tree connectors.
    def _append_capped_section(
        out: list[str],
        header: str,
        items: list,
        formatter,  # (item) -> str
        cap: int,
        total_count: int,
        more_hint: str,
    ) -> None:
        out.append(f"├─ {header}:")
        capped = _render_capped(
            items[:cap], formatter,
            cap=cap, total=total_count,
            more_hint=more_hint,
        )
        # ADR-0023 §1.2: the shared helper attaches └─ to the LAST row, which
        # includes the "... and N more" disclosure row when total_count > cap.
        # Header was appended as a non-last child (├─) → prefix "│   ".
        out.extend(render_list_block(capped, prefix="│   "))

    lines = [f"impact_analysis({entity_type}, {entity_name}, {odoo_version})"]
    lines.append(f"├─ Risk: {risk} ({total} affected entities)")

    # --- Views section ---
    if views:
        _append_capped_section(
            lines,
            f"Views ({view_count})",
            views,
            lambda v_item: f"[{v_item['module']}] {v_item['xmlid']}",
            cap=LIST_PREVIEW_MAX_ITEMS,
            total_count=view_count,
            more_hint=(
                f"model_inspect(model='{model_name}', method='views'"
                f", odoo_version='{odoo_version}') for full view list"
            ),
        )
    else:
        lines.append("├─ Views: none")

    # --- Methods section ---
    if entity_type == "field":
        methods_label = (
            f"Methods on {model_name} with super() ({method_count})"
            f" — field-level filter not yet implemented (M5)"
        )
    elif entity_type == "method":
        methods_label = f"Override chain ({method_count})"
    else:
        methods_label = f"Methods ({method_count})"

    if entity_type == "field":
        # For field: capped list of super()-calling methods
        if methods:
            _append_capped_section(
                lines,
                methods_label,
                methods,
                lambda m_item: f"[{m_item['module']}] {m_item['name']}",
                cap=LIST_PREVIEW_MAX_ITEMS,
                total_count=method_count,
                more_hint=(
                    f"model_inspect(model='{model_name}', method='methods'"
                    f", odoo_version='{odoo_version}') for full method list"
                ),
            )
        else:
            lines.append(f"├─ {methods_label}: none")
        # B2: field-level blast radius from USES_FIELD / DEPENDS_ON_FIELD edges (A2d).
        # Omit sections entirely when empty (pre-reindex: edges not present yet).
        if uses_field_methods:
            uses_count = len(uses_field_methods)
            _append_capped_section(
                lines,
                f"Methods using this field ({uses_count})",
                uses_field_methods,
                lambda m_item: f"[{m_item['module']}] {m_item['model']}.{m_item['name']}",
                cap=LIST_PREVIEW_MAX_ITEMS,
                total_count=uses_count,
                more_hint=(
                    f"model_inspect(model='{model_name}', method='methods'"
                    f", odoo_version='{odoo_version}') for full method list"
                ),
            )
        if depends_on_field_methods:
            dep_count = len(depends_on_field_methods)
            _append_capped_section(
                lines,
                f"Compute-dependent methods ({dep_count})",
                depends_on_field_methods,
                lambda m_item: f"[{m_item['module']}] {m_item['model']}.{m_item['name']}",
                cap=LIST_PREVIEW_MAX_ITEMS,
                total_count=dep_count,
                more_hint=(
                    f"model_inspect(model='{model_name}', method='methods'"
                    f", odoo_version='{odoo_version}') for full method list"
                ),
            )
    elif methods:
        _append_capped_section(
            lines,
            methods_label,
            methods,
            lambda m_item: f"[{m_item['module']}] {m_item['name']}",
            cap=LIST_PREVIEW_MAX_ITEMS,
            total_count=method_count,
            more_hint=(
                f"model_inspect(model='{model_name}', method='methods'"
                f", odoo_version='{odoo_version}') for full method list"
            ),
        )
    else:
        lines.append(f"├─ {methods_label}: none")

    # --- JS patches section ---
    if js_patches:
        _append_capped_section(
            lines,
            f"JS patches ({js_count})",
            js_patches,
            lambda jp: (
                f"[{jp['module']}] {jp['target']}"
                f" via {jp['patch_name']} (era: {jp['era']})"
            ),
            cap=LIST_PREVIEW_MAX_ITEMS,
            total_count=js_count,
            more_hint=(
                f"model_inspect(model='{model_name}', method='summary'"
                f", odoo_version='{odoo_version}') for JS overview"
            ),
        )
    else:
        lines.append("├─ JS patches: none")

    # --- For model entity_type: extension modules section (capped) ---
    if entity_type == "model" and def_modules:
        def_count = len(def_modules)
        mod_names_preview = [d["module_name"] for d in def_modules[:LIST_PREVIEW_MAX_ITEMS]]
        preview_str = ", ".join(mod_names_preview)
        if def_count > LIST_PREVIEW_MAX_ITEMS:
            overflow = def_count - LIST_PREVIEW_MAX_ITEMS
            preview_str += (
                f", ... and {overflow} more"
                f" (use model_inspect(model='{model_name}', method='summary'"
                f", odoo_version='{odoo_version}') for full list)"
            )
        lines.append(f"├─ Defined/extended in ({def_count}): {preview_str}")

    # --- Dependent modules section (capped at IMPACT_MODULES_MAX) ---
    if dep_modules:
        dep_total = len(dep_modules)
        dep_names_preview = [d["dep_name"] for d in dep_modules[:IMPACT_MODULES_MAX]]
        preview_str = ", ".join(dep_names_preview)
        if dep_total > IMPACT_MODULES_MAX:
            overflow = dep_total - IMPACT_MODULES_MAX
            preview_str += (
                f", ... and {overflow} more"
                " (run with profile_name=<profile> to scope)"
            )
        lines.append(f"├─ Dependent modules ({dep_total}): {preview_str}")
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
@offload_bounded_nonorm
def impact_analysis(
    entity_type: str,
    entity_name: str,
    odoo_version: RequiredOdooVersion,
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
    _query_vec=None,
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
    except Exception:
        logger.warning("suggest_pattern: embedder unavailable", exc_info=True)
        return (
            "suggest_pattern: embedder unavailable.\n"
            "Hint: check Ollama is running (default: http://localhost:11434)."
        )

    with driver.session() as session:
        v = _resolve_version(odoo_version, session)

    if _query_vec is not None:
        intent_vec = _query_vec
    else:
        try:
            # Cap intent to the token budget before INSTRUCT (#227, sync path).
            capped = _cap_query_text(embedder, intent)
            instruct = getattr(embedder, "query_instruction", INSTRUCT_NL_TO_CODE)
            intent_vec = embedder.embed([instruct + capped])[0]
        except Exception:
            logger.warning("suggest_pattern: embedding query failed", exc_info=True)
            return (
                "suggest_pattern: embedding query failed — try again shortly, "
                "or verify the embedder service is reachable."
            )

    # Use injected connection (test path) or check out from pool (production).
    # RLS note (WI-7 / ADR-0034 D3/A2 / FUFU-2): pattern catalogue chunks carry
    # the explicit profile_name = '__global__' sentinel (m13_021). The SELECT
    # filters on it directly so this read is immune to GUC state; the
    # embeddings_tenant RLS policy passes the sentinel unconditionally via the
    # "profile_name = '__global__'" branch — no GUC wiring needed here.
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
                         AND profile_name = %s
                       ORDER BY vec <=> %s::vector
                       LIMIT %s""",
                    [intent_vec, GLOBAL_PROFILE, intent_vec, limit],
                )
            else:
                cur.execute(
                    """SELECT entity_name, file_path,
                              1 - (vec <=> %s::vector) AS cosine
                       FROM embeddings
                       WHERE chunk_type = 'pattern_example'
                         AND module = '__patterns__'
                         AND profile_name = %s
                         AND entity_name LIKE %s
                       ORDER BY vec <=> %s::vector
                       LIMIT %s""",
                    [intent_vec, GLOBAL_PROFILE, f"{language}__%", intent_vec, limit],
                )
            ranked = cur.fetchall()

    if not ranked:
        next_line = format_next_step([
            f"find_examples(query='{intent}', odoo_version='{v}')"
            " for real-world variants",
        ])
        return (
            f"suggest_pattern({intent!r}, {v!r}, language={language})\n"
            "├─ No curated patterns available for this query. "
            "The pattern catalogue may not be populated for this version/profile.\n"
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
                # G7: add escape-hatch hint to odoo://pattern/{id} resource
                lines.append(
                    f"{prefix}│   ... ({extra} more lines"
                    f" — read full via odoo://{version}/pattern/{pid})"
                )
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


def _ee_confusion_live() -> dict[str, str | None]:
    """Build EE confusion map from live DB (cached 60 s by get_ee_modules).

    Falls back to static list when DB is unreachable — same as get_ee_modules().
    Called on every _check_module_exists invocation so admin CRUD changes
    propagate within one 60 s cache window (WI-R F-007 fix).
    """
    from src.data.ee_modules import get_ee_modules
    return {m["name"]: m["vt_equivalent"] for m in get_ee_modules()}


def _check_module_exists(
    name: str, odoo_version: str = "auto", *,
    profile_name: str | None = None,
    _driver=None,
) -> str:
    """Report whether `name` is indexed + flag EE-confusion (per ADR-0003 §2).

    Edition-first strategy: query Neo4j for indexed edition (OEEL-1 detected),
    fallback to DB-backed guard list if not indexed.  Both paths produce the same
    EE warning.  Guard list is read via get_ee_modules() (60 s cache) so that
    admin CRUD changes take effect within one cache window (WI-R F-007).
    """
    driver = _driver or _get_driver()
    with driver.session() as session:
        v = _resolve_version(odoo_version, session)
        rec = session.run("""
            MATCH (m:Module {name: $n, odoo_version: $v})
            WHERE ($own IS NULL OR (size(m.profile) > 0
                   AND all(__p IN m.profile WHERE __p IN $own OR __p IN $shared)))
            RETURN m.edition AS edition,
                   m.license AS license,
                   m.viindoo_equivalent_qname AS vvq,
                   m.repo AS repo
        """, n=name, v=v, **_scope(profile_name)).single()

    indexed = rec is not None
    edition = rec["edition"] if rec else None
    license_val = rec["license"] if rec else None
    repo = rec.get("repo") if rec else None
    vvq_db = rec.get("vvq") if rec else None

    # Build live EE confusion map from DB (cached 60 s).  Falls back to static
    # list when DB is unreachable — transparent to callers (WI-R F-007 fix).
    confusion = _ee_confusion_live()

    # Edition-first: check Neo4j for 'enterprise' (from OEEL-1 detection at index time).
    # OPL-1 is NOT mapped to 'enterprise' by _detect_module_edition (it falls to 'custom'),
    # so it never trips the EE-confusion gate below — the indexed `edition` enum is the
    # sole signal (ADR-0036; #263 regression fix removed the prior license-based check).
    is_ee_confusion = False
    ee_source = ""  # track source for output messaging
    viindoo_equivalent = None

    # Gate EE-confusion on the indexed `edition` enum only (ADR-0036). OEEL-1
    # (Odoo S.A.'s OWN Enterprise license) is detected as edition="enterprise" at
    # index time, so the enum check covers it. OPL-1 is the Odoo Proprietary License
    # for third-party/proprietary apps (edition="viindoo"/"custom") and must NOT be
    # flagged as Odoo Enterprise — doing so mislabeled Viindoo OPL-1 addons such as
    # to_base / viin_hr (#263, regression from PR #165).
    if indexed and edition == "enterprise":
        is_ee_confusion = True
        ee_source = "indexed"
        viindoo_equivalent = vvq_db or confusion.get(name)
    elif name in confusion:
        # Not indexed (or not marked 'enterprise') but in guard list
        is_ee_confusion = True
        ee_source = "dict"
        viindoo_equivalent = confusion.get(name)

    return _format_check_module_exists(
        name=name, version=v, indexed=indexed, edition=edition,
        license_val=license_val, repo=repo,
        is_ee_confusion=is_ee_confusion, viindoo_equivalent=viindoo_equivalent,
        ee_source=ee_source,
    )


def _format_check_module_exists(
    *, name: str, version: str, indexed: bool, edition: str | None,
    license_val: str | None = None,
    repo: str | None, is_ee_confusion: bool, viindoo_equivalent: str | None,
    ee_source: str = "",
) -> str:
    lines = [f"check_module_exists({name!r}, {version})"]
    lines.append(f"├─ Indexed:         {'Yes' if indexed else 'No'}")
    if indexed and edition:
        repo_suffix = f" [{repo}]" if repo else ""
        # WG-5 T1: derive human-readable edition label from license (preferred)
        # or from indexed edition enum.
        ed_label = _edition_label(edition, license_val)
        lines.append(f"├─ Edition:         {ed_label}{repo_suffix}")
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
            source_hint = f" (license={license_val})" if license_val else ""
        elif ee_source == "dict":
            source_hint = " (legacy hardcoded dict)"
        # ADR-0023 §2: English-only tool output.
        lines.append(
            f"├─ ⚠ WARNING: this is an Odoo Enterprise module{source_hint}. "
            "Do NOT depend on it in a Viindoo Community stack — "
            "this violates the GPL/Enterprise license boundary."
        )
    elif not indexed:
        # ADR-0023 §4.4: terminal branch — module genuinely not found, no
        # operator-shell hint (agents cannot execute shell commands).
        lines.append(
            "└─ Not indexed in this profile. "
            "Verify the module name, or call list_available_profiles to see indexed scope."
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
    *,
    _reraise_timeout: bool = False,
) -> str:
    """Layer-0 module overview: manifest + model/view/JS counts.

    Distinct from check_module_exists (1–3 lines, YES/NO + edition) — this
    tool returns the full architecture tree (~10–15 lines) per ADR-0023 §1.7.
    Runs 1 Module query + 4 aggregate queries (Models defined, Models
    extended, Views by type, JS patches).

    Each query is routed through ``_data_bounded`` / ``_single_bounded`` with
    its OWN sub-step label so a tx-timeout becomes OrmQueryTimeout (clean
    English, no Cypher leaked) and the timeout message names which sub-step
    died. There is no internal catch: the raise propagates so the owning
    describe_module / module_inspect handler (now ``@offload_neo4j``) records the
    metric + returns the clean string (tool path), or the module resource
    handler records + returns it UNCACHED (resource path). ``_reraise_timeout``
    is signature parity with the sibling resolvers (the module resource render
    passes it); nothing here converts the timeout to a string, so both paths
    already propagate identically.
    """
    _ = _reraise_timeout  # parity-only flag; the timeout always propagates here.
    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        mod_rec = _single_bounded(
            session,
            """
            MATCH (m:Module {name: $n, odoo_version: $v})
            WHERE ($own IS NULL OR (size(m.profile) > 0
                   AND all(__p IN m.profile WHERE __p IN $own OR __p IN $shared)))
            RETURN m.repo AS repo, m.path AS path, m.version_raw AS version_raw,
                   m.edition AS edition,
                   m.license AS license,
                   m.viindoo_equivalent_qname AS vvq,
                   m.license_notice AS license_notice,
                   m.repo_url AS repo_url,
                   m.auto_install AS auto_install,
                   m.application AS application,
                   m.category AS category,
                   m.summary AS summary,
                   m.external_python AS external_python,
                   m.external_bin AS external_bin
            """,
            f"module manifest for '{name}' (Odoo {odoo_version})",
            n=name, v=odoo_version, **_scope(profile_name),
        )

        if not mod_rec:
            return (
                f"No module named '{name}' indexed for Odoo {odoo_version}."
            )

        # depends-list is intentionally NOT tenant-scoped (no _scope_pred("d")).
        # It returns only d.name — dependency names from THIS module's own manifest.
        # Safety rests on the scoped `mod_rec` query above, which early-returns if the
        # caller is not entitled to module $n@$v; this is a SEPARATE session.run that
        # re-matches `m` by name+version only (NOT scoped) — fine, because that prior
        # gate already proved entitlement and d.name is just a name the caller's own
        # manifest declared. Contrast _module_dep_closure below, which filters `dep`
        # because it returns dependency node CONTENT (dep.repo / dep.repo_url).
        # Filtering names here would only hide a dep the tenant itself declared when
        # its name collides with another tenant's private module (ADR-0034 A3) — no
        # confidentiality gain, real UX loss.
        depends = _data_bounded(
            session,
            f"""
            MATCH (m:Module {{name: $n, odoo_version: $v}})
                  -[:{REL_DEPENDS_ON}]->(d:Module)
            RETURN d.name AS name
            ORDER BY d.name ASC
            """,
            f"dependencies for '{name}' (Odoo {odoo_version})",
            n=name, v=odoo_version,
        )

        defines = _data_bounded(
            session,
            """
            MATCH (model:Model {module: $n, odoo_version: $v})
            WHERE coalesce(model.is_definition, false) = true
              AND model.module <> '__unresolved__'
              AND ($own IS NULL OR (size(model.profile) > 0
                   AND all(__p IN model.profile WHERE __p IN $own OR __p IN $shared)))
            RETURN model.name AS name
            ORDER BY model.name ASC
            """,
            f"models defined in '{name}' (Odoo {odoo_version})",
            n=name, v=odoo_version, **_scope(profile_name),
        )

        extends = _data_bounded(
            session,
            """
            MATCH (model:Model {module: $n, odoo_version: $v})
            WHERE coalesce(model.is_definition, false) = false
              AND model.module <> '__unresolved__'
              AND ($own IS NULL OR (size(model.profile) > 0
                   AND all(__p IN model.profile WHERE __p IN $own OR __p IN $shared)))
            RETURN model.name AS name
            ORDER BY model.name ASC
            """,
            f"models extended in '{name}' (Odoo {odoo_version})",
            n=name, v=odoo_version, **_scope(profile_name),
        )

        view_breakdown = _data_bounded(
            session,
            """
            MATCH (view:View {module: $n, odoo_version: $v})
            WHERE ($own IS NULL OR (size(view.profile) > 0
                   AND all(__p IN view.profile WHERE __p IN $own OR __p IN $shared)))
            RETURN view.type AS type, count(view) AS c
            ORDER BY c DESC, type ASC
            """,
            f"view breakdown for '{name}' (Odoo {odoo_version})",
            n=name, v=odoo_version, **_scope(profile_name),
        )

        js_rec = _single_bounded(
            session,
            """
            MATCH (j:JSPatch {module: $n, odoo_version: $v})
            WHERE ($own IS NULL OR (size(j.profile) > 0
                   AND all(__p IN j.profile WHERE __p IN $own OR __p IN $shared)))
            RETURN count(j) AS c
            """,
            f"JS patch count for '{name}' (Odoo {odoo_version})",
            n=name, v=odoo_version, **_scope(profile_name),
        )
        js_count = js_rec["c"] if js_rec else 0

    lines = [f"{name} (Odoo {odoo_version})"]

    # B1/ADR-0037: render repo identity + repo-relative path so agents can locate
    # the module in their OWN checkout. Prefer the portable git URL (Repo URL);
    # the server checkout dirname (Repo:) is host-specific, shown only as a
    # fallback when no URL is known — never both (it would be redundant noise).
    if mod_rec.get("repo_url"):
        lines.append(f"├─ Repo URL: {mod_rec['repo_url']}")
    elif mod_rec.get("repo"):
        lines.append(f"├─ Repo: {mod_rec['repo']}")
    if mod_rec.get("path"):
        # Anchor strip on the dirname (mod_rec['repo']) for legacy absolute rows;
        # post-reindex mod_rec['path'] is already relative → idempotent no-op.
        lines.append(
            f"├─ Path: {_portable_path(mod_rec['path'], repo=mod_rec.get('repo'), module=name)}"
        )

    # B2: render auto_install / application flags (only when True — not noise).
    if mod_rec.get("auto_install"):
        lines.append("├─ Auto-install: yes")
    if mod_rec.get("application"):
        lines.append("├─ Application: yes")

    # B2: render category when present.
    if mod_rec.get("category"):
        lines.append(f"├─ Category: {mod_rec['category']}")

    # B2: render external deps (python + bin) when non-empty.
    ext_py = mod_rec.get("external_python") or []
    ext_bin = mod_rec.get("external_bin") or []
    if ext_py or ext_bin:
        parts = []
        if ext_py:
            parts.append("python: " + ", ".join(ext_py))
        if ext_bin:
            parts.append("bin: " + ", ".join(ext_bin))
        lines.append("├─ External deps: " + "; ".join(parts))

    # ADR-0036: surface license_notice as a visible marker (D3 — never silent).
    # Only emitted when non-null (i.e. module is ingest_flagged; skip action
    # means the module never reaches here at all).
    if mod_rec.get("license_notice"):
        lines.append(f"├─ License notice: {mod_rec['license_notice']}")

    # Manifest sub-tree (non-last parent → "│   " sublist indent).
    lines.append("├─ Manifest:")
    manifest_rows: list[tuple[str, str]] = []
    if depends:
        # Inline list with cap + escape-hatch hint when truncated (G6).
        dep_names = ", ".join(d["name"] for d in depends[:LIST_PREVIEW_MAX_ITEMS])
        if len(depends) > LIST_PREVIEW_MAX_ITEMS:
            dep_names += (
                f", ... and {len(depends) - LIST_PREVIEW_MAX_ITEMS} more"
                f" (use module_inspect(name='{name}', method='dependencies'"
                f", odoo_version='{odoo_version}') for full list)"
            )
        manifest_rows.append(("Depends", dep_names))
    else:
        manifest_rows.append(("Depends", "—"))
    # WG-5 T1: human-readable edition label derived from license (preferred) or edition enum.
    edition_str = _edition_label(mod_rec.get("edition"), mod_rec.get("license"))
    if mod_rec.get("vvq"):
        edition_str += f" (Viindoo equivalent: {mod_rec['vvq']})"
    manifest_rows.append(("Edition", edition_str))
    manifest_rows.append(("Version", mod_rec.get("version_raw") or "—"))
    if mod_rec.get("summary"):
        manifest_rows.append(("Summary", mod_rec["summary"]))
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


def _module_dep_closure(
    name: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
) -> str:
    """Transitive DEPENDS_ON closure for a module — returns all dependencies + load order.

    Traverses (:Module)-[:DEPENDS_ON*]->(:Module) up to depth 20 to collect
    the full transitive closure.  Then computes a topological load order using
    path-length as a proxy (shorter path = loaded earlier) with alphabetical
    tiebreak for determinism.  Each dependency line shows [repo] name (repo_url).

    B2: This is surfaced as module_inspect(method='dependencies') per ADR-0028
    consolidation — no new top-level tool.
    """
    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        # Verify the module exists first.
        exists_rec = _single_bounded(
            session,
            "MATCH (m:Module {name: $n, odoo_version: $v}) "
            f"WHERE {_scope_pred('m')} "
            "RETURN count(m) AS c",
            f"module existence for '{name}' (Odoo {odoo_version})",
            n=name, v=odoo_version, **_scope(profile_name),
        )
        exists = exists_rec["c"] if exists_rec else 0
        if not exists:
            return f"No module named '{name}' indexed for Odoo {odoo_version}."

        # Collect full transitive closure with min path-length (Dijkstra-style)
        # and repo/repo_url for each dependency.
        # PATHS(p) gives the variable-length path; length(p) = hop count.
        # §2.4: this is a VLP (`DEPENDS_ON*1..20`) — the #273-class risk. We only
        # BOUND it here (the 30s per-query timeout is the load-bearing protection);
        # a per-hop name-dedup rewrite is OUT of scope for this hardening wave. If
        # nonorm_query_timeout_total{tool="module_inspect"} spikes on this path,
        # escalate to the per-hop rewrite. `DEPENDS_ON` is a manifest-dependency
        # DAG (far less dense than the same-name INHERITS mesh), so the depth-20
        # cap + 30s bound make it safe now.
        dep_rows = _data_bounded(
            session,
            f"""
            MATCH path = (:Module {{name: $n, odoo_version: $v}})
                         -[:{REL_DEPENDS_ON}*1..20]->(dep:Module {{odoo_version: $v}})
            WHERE ($own IS NULL OR (size(dep.profile) > 0
                   AND all(__p IN dep.profile WHERE __p IN $own OR __p IN $shared)))
            WITH dep, min(length(path)) AS min_depth
            RETURN dep.name AS dep_name,
                   dep.repo AS repo,
                   dep.repo_url AS repo_url,
                   min_depth
            ORDER BY min_depth DESC, dep.name ASC
            """,
            f"dependency closure for '{name}' (Odoo {odoo_version})",
            n=name, v=odoo_version, **_scope(profile_name),
        )

    if not dep_rows:
        lines = [f"{name} dependency closure (Odoo {odoo_version})"]
        lines.append("├─ No transitive dependencies found.")
        lines.append(format_next_step([
            f"describe_module(name='{name}', odoo_version='{odoo_version}')"
            " for full module overview",
        ]))
        return "\n".join(lines)

    # Build load order: sort by (min_depth DESC, name ASC) — already ordered by Cypher.
    # Odoo loads deepest transitive dependencies FIRST (e.g. 'base' before 'sale').
    # index 1 = first to be installed / loaded; deepest deps have highest min_depth.
    lines = [f"{name} dependency closure (Odoo {odoo_version})"]
    lines.append(f"├─ Transitive dependencies ({len(dep_rows)}) — load order:")
    last_idx = len(dep_rows) - 1
    for i, row in enumerate(dep_rows):
        connector = "└─" if i == last_idx else "├─"
        repo_str = f"[{row['repo']}] " if row.get("repo") else ""
        url_str = f"  ({row['repo_url']})" if row.get("repo_url") else ""
        lines.append(
            f"│   {connector} {i + 1:>2}. {repo_str}{row['dep_name']}{url_str}"
        )
    lines.append(format_next_step([
        f"describe_module(name='{name}', odoo_version='{odoo_version}')"
        " for full module overview",
        f"module_inspect(name='{name}', method='summary', odoo_version='{odoo_version}')"
        " for manifest detail",
    ]))
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

        # INHERITS-aware enumeration: own fields (depth 0) + fields inherited
        # from mixins (depth 1-3 via INHERITS|DELEGATES_TO), deduped by name
        # with the nearest owner winning (child overrides mixin). The dedup +
        # SKIP/LIMIT happen IN-QUERY, so pagination is consistent with the
        # matching DISTINCT-name count below. Provenance fields owner_model /
        # inherit_depth / edge_kind are carried for the `inherited from` /
        # `delegated via` row labels. Bounded by _bounded() (issue #273).
        #
        # FIX (#284 follow-up): these two helpers are bounded + tx-timeout-mapped
        # to OrmQueryTimeout on a dense inheritance graph. The list path catches
        # that HERE and returns the clean degraded English string directly. The
        # @offload_neo4j boundary on model_inspect/entity_lookup (PR-1 #287) only
        # backstops a RAISED OrmQueryTimeout; this inline catch keeps the list
        # path self-contained (note: it does NOT yet emit the timeout metric —
        # deferred to PR-3 / issue #287 M3). Mirrors the detail path
        # (_resolve_field): surface the clean string instead of raising.
        try:
            rows = _list_fields_with_inherited(
                model, odoo_version, session, profile_name,
                module=module, kind=kind,
                skip=start_index, limit=effective_limit,
            )

            # Separate count query (same traversal + DISTINCT-name dedup) so the
            # "Showing X of N" total always matches the paginated, deduped rows.
            total = _count_fields_with_inherited(
                model, odoo_version, session, profile_name, module=module, kind=kind
            )
        except OrmQueryTimeout as exc:
            return exc.user_message

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
        # Dedup magic names against own AND inherited owners. A mixin can declare
        # a magic-named field (e.g. `display_name`), so the flat own-model check
        # alone would double-show it once the inherited rows surface it in the
        # paginated list. FIX-2 (review #283): reuse the shared bounded owner-set
        # helper (`_ancestor_owner_names`, the SAME _ANCESTOR_TAGGED_PROLOGUE the
        # listing uses) instead of a hand-rolled re-implementation of the 3-hop
        # BFS — one SSOT, both bounded. On a tx-timeout the magic check degrades
        # to the flat own-model dedup (existing_names from the model alone) so a
        # dense graph never crashes the whole list (it can at worst double-show a
        # magic-named field that a mixin also declares — a cosmetic degradation,
        # not a failure).
        existing_names: set[str] = set()
        try:
            with _get_driver().session() as _dedup_session:
                owner_names = _ancestor_owner_names(
                    model, odoo_version, _dedup_session, profile_name
                )
                # RAW-ESCAPE fix: this was a BARE `_dedup_session.run(_bounded(...))`
                # — timeout-bounded but raising a RAW neo4j ClientError, so the
                # `except OrmQueryTimeout` below was BLIND to it (the same bug
                # class as the #286 override_rec fix). Route through
                # `_single_bounded` so a tx-timeout becomes OrmQueryTimeout and the
                # existing degrade-to-flat fallback actually fires.
                _dedup_rec = _single_bounded(
                    _dedup_session,
                    """
                    UNWIND $owners AS owner_model
                    MATCH (f:Field {model: owner_model, odoo_version: $v})
                    WHERE f.name IN $magic_names
                      AND ($own IS NULL OR (size(f.profile) > 0
                           AND all(__p IN f.profile WHERE __p IN $own OR __p IN $shared)))
                      AND f.module <> '__unresolved__'
                    RETURN collect(DISTINCT f.name) AS names
                    """,
                    f"magic-field dedup for '{model}'",
                    owners=owner_names, v=odoo_version,
                    magic_names=magic_names_list, **_scope(profile_name),
                )
            existing_names = set(_dedup_rec["names"]) if _dedup_rec else set()
        except OrmQueryTimeout:
            # Degrade to flat own-model magic dedup — never crash the list.
            try:
                with _get_driver().session() as _flat_session:
                    # RAW-ESCAPE fix: same bare-`_bounded` → `_single_bounded`
                    # conversion so the inner `except OrmQueryTimeout` below
                    # actually catches a tx-timeout on the flat fallback too.
                    _flat_rec = _single_bounded(
                        _flat_session,
                        """
                        MATCH (f:Field {model: $m, odoo_version: $v})
                        WHERE f.name IN $magic_names
                          AND ($own IS NULL OR (size(f.profile) > 0
                               AND all(__p IN f.profile
                                       WHERE __p IN $own OR __p IN $shared)))
                          AND f.module <> '__unresolved__'
                        RETURN collect(DISTINCT f.name) AS names
                        """,
                        f"magic-field flat dedup for '{model}'",
                        m=model, v=odoo_version,
                        magic_names=magic_names_list, **_scope(profile_name),
                    )
                existing_names = set(_flat_rec["names"]) if _flat_rec else set()
            except OrmQueryTimeout:
                existing_names = set()
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
        # Continuation hint uses start_index (ADR-0023 §5.5 Amendment 2026-05-19).
        # Do NOT suggest raising limit= — the cap is intentional (ADR-0023 §3).
        # The global start_index footer below handles cross-module pagination.
        more_hint = (
            f"model_inspect(model='{model}', method='fields', odoo_version='{odoo_version}',"
            f" start_index={start_index + cap})"
        )
        # Build rendered strings with inline refs.
        raw_rows = [r for r, _ in sub_items]
        def _fmt_field_row(r: dict) -> str:
            # B1: include stored/compute/comodel_name in field row summary.
            # WI-1 (#238): also surface related= / readonly / required so AI
            # clients don't try to set a non-writable field in create()/write().
            parts = [f"{r['name']} : {r['ttype']}"]
            if r.get("compute"):
                parts.append(f"compute={r['compute']}")
            elif not r.get("stored", True):
                # stored=False without compute is unusual but surfaceable.
                parts.append("stored=False")
            if r.get("related"):
                parts.append(f"related={r['related']}")
            if r.get("comodel_name"):
                parts.append(f"-> {r['comodel_name']}")
            # effective_readonly is None on pre-reindex nodes — only flag when
            # explicitly True (graceful degradation, mirrors detail view).
            if r.get("effective_readonly"):
                parts.append("readonly")
            if r.get("required"):
                parts.append("required")
            # Provenance token (ADR-0023 token-additive): tag fields that come
            # from a mixin so the AI client knows they are inherited, not own.
            # Own fields (owner_model == model) get no token — output unchanged.
            # _provenance_token is the SSOT for the wording (FIX-6, review #283).
            token = _provenance_token(
                r.get("owner_model"), model, r.get("edge_kind"), r.get("via_field")
            )
            if token:
                parts.append(token)
            return " | ".join(parts)

        rendered_strs = _render_capped(
            raw_rows,
            _fmt_field_row,
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
    elif total > 0 and start_index >= total:
        # start_index past the end (cursor over-run): rows is empty, so the
        # "rows {start+1}-{end}" branch would render an inverted range
        # (e.g. "26-25 of 25"). Disclose the over-run cleanly instead.
        lines.append(
            f"├─ No rows at start_index={start_index} (total={total});"
            f" last row is at index {total - 1}."
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

        # INHERITS-aware enumeration (symmetric to _list_fields): own methods
        # (depth 0) + methods inherited from mixins (depth 1-3 via INHERITS ONLY
        # — `_inherits` delegation NEVER carries methods, GAP-1; DELEGATES_TO is
        # the field-only path), deduped by name with the nearest owner winning.
        # Dedup + SKIP/LIMIT happen IN-QUERY so pagination matches the
        # DISTINCT-name count below. Carries owner_model for provenance labels
        # (edge_kind is always 'inherits' on the method path).
        # FIX (#284 follow-up): the three acquisitions below are all bounded by
        # the per-query Neo4j timeout. The list path catches a tx-timeout HERE and
        # returns the clean degraded string directly; the @offload_neo4j boundary
        # on model_inspect/entity_lookup (PR-1 #287) only backstops a RAISED
        # OrmQueryTimeout, so this inline catch keeps the list path self-contained
        # (note: does NOT yet emit the metric — deferred to PR-3 / issue #287 M4).
        # Wrap the whole acquisition in `try/except OrmQueryTimeout: return
        # exc.user_message`, mirroring _resolve_field. The override_rec query was a BARE
        # `session.run(_bounded(...)).single()` that raises a RAW neo4j
        # ClientError on timeout (NOT routed through orm.py's ClientError ->
        # OrmQueryTimeout conversion), so it would not even reach this catch —
        # route it through `_single_bounded` (the SAME conversion helper the
        # codebase already uses) so its timeout becomes OrmQueryTimeout too.
        try:
            rows = _list_methods_with_inherited(
                model, odoo_version, session, profile_name,
                module=module, skip=start_index, limit=effective_limit,
            )
            # Map convention_kind → the `kind` key the existing renderer expects.
            for _r in rows:
                _r["kind"] = _r.get("convention_kind")

            total = _count_methods_with_inherited(
                model, odoo_version, session, profile_name, module=module
            )

            # Override-marker (GAP-2): a method is marked (*) when it is declared
            # in >=2 modules ON ITS OWNER MODEL. For an INHERITED method the owner
            # is the mixin, not the child — so counting modules only on {model: $m}
            # would never mark an inherited method even when it is overridden N
            # times on its owner. Compute the override set per (method_name,
            # owner_model) over the SAME INHERITS-only ancestor set the method
            # listing uses (NOT DELEGATES_TO — methods are not delegated, GAP-1),
            # so an inherited method overridden across modules on its owner gets
            # the (*) marker in the child listing. Keyed by (name, owner) so a
            # same-named method on two different owners cannot cross-contaminate
            # the marker. Routed through _single_bounded so a tx-timeout becomes
            # OrmQueryTimeout (not a raw ClientError) and joins the catch below.
            override_rec = _single_bounded(
                session,
                _ANCESTOR_TAGGED_PROLOGUE_INHERITS_ONLY + """
                MATCH (mth:Method {model: owner_model, odoo_version: $v})
                WHERE """ + _scope_pred("mth") + """
                  AND mth.module <> '__unresolved__'
                WITH mth.name AS name, owner_model,
                     count(DISTINCT mth.module) AS modcount
                WHERE modcount >= 2
                RETURN collect([name, owner_model]) AS overrides
                """,
                f"method override markers (including inherited) for '{model}'"
                f" (Odoo {odoo_version})",
                mn=model, v=odoo_version, **_scope(profile_name),
            )
        except OrmQueryTimeout as exc:
            return exc.user_message
        override_keys = {
            (name, owner) for name, owner in (override_rec["overrides"] or [])
        } if override_rec else set()

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
            f"model_inspect(model='{model}', method='methods', odoo_version='{odoo_version}',"
            f" start_index={start_index + cap})"
        )

        raw_rows = [r for r, _ in sub_items]

        def _fmt_method(r):
            marker = "(*)" if (r["name"], r.get("owner_model") or model) in override_keys else ""
            kind_str = r.get("kind") or "private"
            base = f"{r['name']}{marker} : {kind_str}"
            # Provenance token (ADR-0023 token-additive): tag inherited methods.
            # Methods are inherited via INHERITS only (Python MRO) — _inherits
            # delegation NEVER carries methods (GAP-1), so edge_kind is always
            # 'inherits' here and the token can only read "inherited from".
            # _provenance_token is the SSOT for the wording (FIX-6, review #283).
            token = _provenance_token(
                r.get("owner_model"), model, r.get("edge_kind"), r.get("via_field")
            )
            if token:
                base += f" | {token}"
            return base

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
    elif total > 0 and start_index >= total:
        # start_index past the end (cursor over-run): rows is empty, so the
        # "rows {start+1}-{end}" branch would render an inverted range
        # (e.g. "26-25 of 25"). Disclose the over-run cleanly instead.
        lines.append(
            f"├─ No rows at start_index={start_index} (total={total});"
            f" last row is at index {total - 1}."
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


def _list_extenders(
    model: str,
    odoo_version: str = "auto",
    profile_name: str | None = None,
    limit: int = 200,
    start_index: int = 0,
    api_key_id: str = _ANONYMOUS_API_KEY_ID,
) -> str:
    """Layer-5 — list all modules that extend (but do not define) a model.

    Uses the same ranking heuristic as _resolve_model summary but filters to
    extension modules only (NOT coalesce(m.is_definition, false)).
    `start_index` is a zero-based pagination cursor (Cypher SKIP).
    `api_key_id` scopes minted refs to the calling tenant (default: 'anonymous').
    """
    cap = LIST_PREVIEW_MAX_ITEMS
    effective_limit = min(limit, cap)

    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        rows = _data_bounded(
            session,
            f"""
            MATCH (m:Model {{name: $name, odoo_version: $v}})-[:DEFINED_IN]->(mod:Module)
            WHERE NOT coalesce(m.is_definition, false)
              AND ($own IS NULL OR (size(m.profile) > 0
                   AND all(__p IN m.profile WHERE __p IN $own OR __p IN $shared)))
            WITH m, mod,
                 COUNT {{
                     (:Field {{model: $name, module: m.module, odoo_version: $v}})
                 }} AS field_count,
                 COUNT {{ ()-[:{REL_DEPENDS_ON}]->(mod) }} AS dependents,
                 {_edition_rank_cypher("mod")},
                 mod.name AS mod_name
            RETURN m.module AS module_name, coalesce(mod.repo_url, mod.repo) AS repo
            ORDER BY field_count DESC, dependents DESC, edition_rank ASC, mod_name ASC
            SKIP $skip
            LIMIT $limit
            """,
            f"extenders for '{model}' (Odoo {odoo_version})",
            name=model, v=odoo_version, **_scope(profile_name),
            skip=start_index, limit=effective_limit,
        )

        total_rec = _single_bounded(
            session,
            """
            MATCH (m:Model {name: $name, odoo_version: $v})-[:DEFINED_IN]->(:Module)
            WHERE NOT coalesce(m.is_definition, false)
              AND ($own IS NULL OR (size(m.profile) > 0
                   AND all(__p IN m.profile WHERE __p IN $own OR __p IN $shared)))
            RETURN count(m) AS c
            """,
            f"extender count for '{model}' (Odoo {odoo_version})",
            name=model, v=odoo_version, **_scope(profile_name),
        )
        total = total_rec["c"] if total_rec else 0

    header = f"Extenders of {model} (Odoo {odoo_version})"
    if total == 0:
        next_line = format_next_step([
            f"model_inspect(model='{model}', method='summary', odoo_version='{odoo_version}')"
            " for model overview",
        ])
        return f"{header}\n├─ (none — model not extended or not indexed)\n{next_line}"

    # Mint opaque refs for each extender module.
    ext_items = [{"module_name": r["module_name"], "model": model} for r in rows]
    ref_ids = mint_refs(ext_items, api_key_id, kind="module")

    lines = [header]
    shown = len(rows)
    end_index = start_index + shown

    for (r, ref_id) in zip(rows, ref_ids):
        repo = r.get("repo") or "?"
        mod_name = r.get("module_name") or "?"
        lines.append(f"├─ [ref={ref_id}] [{repo}] {mod_name}")

    if total > end_index:
        next_count = min(cap, total - end_index)
        lines.append(
            f"├─ Showing rows {start_index + 1}-{end_index} of {total}."
            f" Call model_inspect(model='{model}', method='extenders',"
            f" odoo_version='{odoo_version}',"
            f" start_index={end_index}) for next {next_count}."
        )
    elif start_index >= total:
        # start_index past the end (cursor over-run): rows is empty, so the
        # "rows {start+1}-{end}" branch would render an inverted range
        # (e.g. "26-25 of 25"). Disclose the over-run cleanly instead.
        lines.append(
            f"├─ No rows at start_index={start_index} (total={total});"
            f" last row is at index {total - 1}."
        )
    elif start_index > 0:
        lines.append(
            f"├─ Showing rows {start_index + 1}-{end_index} of {total} (last page)."
        )
    else:
        # Single full page (total <= cap, start_index == 0): still disclose the
        # complete count so the agent knows nothing was truncated (L3).
        lines.append(f"├─ Showing all {total} of {total}.")

    next_hints: list[str] = []
    next_hints.append(
        f"model_inspect(model='{model}', method='summary', odoo_version='{odoo_version}')"
        " for model overview",
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

    `view_type` filters by View.type (form/tree/list/kanban/search/...).
    'list' is the v18+ tag alias for 'tree'.
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

    # T2 — list/tree alias: v17 stores 'tree' in DB while source XML uses <list>;
    # v18 hard-renamed to 'list' in DB.  Treat the two values as interchangeable
    # so that view_type='tree' matches v18 views (DB='list') and vice-versa.
    # Strategy: pass BOTH alias values to Cypher via a $view_types list so the
    # Cypher filter becomes `v.type IN $view_types` — a single predicate handles
    # NULL (no filter), single-value (exact), and alias-pair cases.
    if view_type is None:
        view_types: list[str] | None = None  # pass-through: no type filter
    elif view_type in ("tree", "list"):
        view_types = ["tree", "list"]  # alias pair
    else:
        view_types = [view_type]  # exact match for all other types

    with _get_driver().session() as session:
        odoo_version = _resolve_version(odoo_version, session)

        scope_noun = f"'{model}'" if is_model_scoped else f"module '{module}'"
        if is_model_scoped:
            rows = _data_bounded(
                session,
                f"""
                MATCH (v:View {{model: $filter_val, odoo_version: $ver}})
                WHERE ($own IS NULL OR (size(v.profile) > 0
                       AND all(__p IN v.profile WHERE __p IN $own OR __p IN $shared)))
                  AND ($view_types IS NULL OR v.type IN $view_types)
                  AND v.module <> '__unresolved__'
                OPTIONAL MATCH (mod:Module {{name: v.module, odoo_version: $ver}})
                WITH v, mod,
                     {_edition_rank_cypher("mod")},
                     mod.name AS mod_name
                RETURN v.xmlid AS xmlid, v.type AS type,
                       v.module AS module, coalesce(mod.repo_url, mod.repo) AS repo,
                       edition_rank, mod_name
                ORDER BY edition_rank ASC, mod_name ASC, v.xmlid ASC
                SKIP $skip
                LIMIT $limit
                """,
                f"view list for {scope_noun} (Odoo {odoo_version})",
                filter_val=model, ver=odoo_version, view_types=view_types,
                **_scope(profile_name), skip=start_index, limit=effective_limit,
            )

            total_rec = _single_bounded(
                session,
                """
                MATCH (v:View {model: $filter_val, odoo_version: $ver})
                WHERE ($own IS NULL OR (size(v.profile) > 0
                       AND all(__p IN v.profile WHERE __p IN $own OR __p IN $shared)))
                  AND ($view_types IS NULL OR v.type IN $view_types)
                  AND v.module <> '__unresolved__'
                RETURN count(v) AS c
                """,
                f"view count for {scope_noun} (Odoo {odoo_version})",
                filter_val=model, ver=odoo_version, view_types=view_types,
                **_scope(profile_name),
            )
        else:
            rows = _data_bounded(
                session,
                f"""
                MATCH (v:View {{module: $filter_val, odoo_version: $ver}})
                WHERE ($own IS NULL OR (size(v.profile) > 0
                       AND all(__p IN v.profile WHERE __p IN $own OR __p IN $shared)))
                  AND ($view_types IS NULL OR v.type IN $view_types)
                  AND v.module <> '__unresolved__'
                OPTIONAL MATCH (mod:Module {{name: v.module, odoo_version: $ver}})
                WITH v, mod,
                     {_edition_rank_cypher("mod")},
                     mod.name AS mod_name
                RETURN v.xmlid AS xmlid, v.type AS type,
                       v.module AS module, coalesce(mod.repo_url, mod.repo) AS repo,
                       edition_rank, mod_name
                ORDER BY edition_rank ASC, mod_name ASC, v.xmlid ASC
                SKIP $skip
                LIMIT $limit
                """,
                f"view list for {scope_noun} (Odoo {odoo_version})",
                filter_val=module, ver=odoo_version, view_types=view_types,
                **_scope(profile_name), skip=start_index, limit=effective_limit,
            )

            total_rec = _single_bounded(
                session,
                """
                MATCH (v:View {module: $filter_val, odoo_version: $ver})
                WHERE ($own IS NULL OR (size(v.profile) > 0
                       AND all(__p IN v.profile WHERE __p IN $own OR __p IN $shared)))
                  AND ($view_types IS NULL OR v.type IN $view_types)
                  AND v.module <> '__unresolved__'
                RETURN count(v) AS c
                """,
                f"view count for {scope_noun} (Odoo {odoo_version})",
                filter_val=module, ver=odoo_version, view_types=view_types,
                **_scope(profile_name),
            )

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
            f"{pager_tool}, start_index={start_index + cap})"
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
    elif total > 0 and start_index >= total:
        # start_index past the end (cursor over-run): rows is empty, so the
        # "rows {start+1}-{end}" branch would render an inverted range
        # (e.g. "26-25 of 25"). Disclose the over-run cleanly instead.
        lines.append(
            f"├─ No rows at start_index={start_index} (total={total});"
            f" last row is at index {total - 1}."
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

        rows = _data_bounded(
            session,
            """
            MATCH (c:OWLComp {module: $mod, odoo_version: $v})
            WHERE ($own IS NULL OR (size(c.profile) > 0
                   AND all(__p IN c.profile WHERE __p IN $own OR __p IN $shared)))
              AND ($bound_model IS NULL OR c.bound_model = $bound_model)
              AND c.module <> '__unresolved__'
            RETURN c.name AS name, c.bound_model AS bound_model,
                   c.template AS template
            ORDER BY c.name ASC
            SKIP $skip
            LIMIT $limit
            """,
            f"OWL components in '{module}' (Odoo {odoo_version})",
            mod=module, v=odoo_version, bound_model=bound_model,
            **_scope(profile_name), skip=start_index, limit=effective_limit,
        )

        total_rec = _single_bounded(
            session,
            """
            MATCH (c:OWLComp {module: $mod, odoo_version: $v})
            WHERE ($own IS NULL OR (size(c.profile) > 0
                   AND all(__p IN c.profile WHERE __p IN $own OR __p IN $shared)))
              AND ($bound_model IS NULL OR c.bound_model = $bound_model)
              AND c.module <> '__unresolved__'
            RETURN count(c) AS c
            """,
            f"OWL component count in '{module}' (Odoo {odoo_version})",
            mod=module, v=odoo_version, bound_model=bound_model,
            **_scope(profile_name),
        )
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
        f", odoo_version='{odoo_version}', start_index={start_index + cap})"
    )
    raw_rows = rows
    rendered = _render_capped(
        raw_rows,
        lambda r: (
            f"{r['name']} : {r.get('bound_model') or '(unbound)'}"
            + (f" | template={r['template']}" if r.get("template") else "")
        ),
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

        rows = _data_bounded(
            session,
            f"""
            MATCH (t:QWebTmpl {{module: $mod, odoo_version: $v}})
            WHERE {_scope_pred("t")}
              AND t.module <> '__unresolved__'
            OPTIONAL MATCH (t)-[:EXTENDS_TMPL]->(parent:QWebTmpl)
            WHERE NOT coalesce(parent.unresolved, false)
              AND {_scope_pred("parent")}
            RETURN t.xmlid AS xmlid, parent.xmlid AS parent_xmlid
            ORDER BY t.xmlid ASC
            SKIP $skip
            LIMIT $limit
            """,
            f"QWeb templates in '{module}' (Odoo {odoo_version})",
            mod=module, v=odoo_version, **_scope(profile_name),
            skip=start_index, limit=effective_limit,
        )

        total_rec = _single_bounded(
            session,
            """
            MATCH (t:QWebTmpl {module: $mod, odoo_version: $v})
            WHERE ($own IS NULL OR (size(t.profile) > 0
                   AND all(__p IN t.profile WHERE __p IN $own OR __p IN $shared)))
              AND t.module <> '__unresolved__'
            RETURN count(t) AS c
            """,
            f"QWeb template count in '{module}' (Odoo {odoo_version})",
            mod=module, v=odoo_version, **_scope(profile_name),
        )
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
        f", odoo_version='{odoo_version}', start_index={start_index + cap})"
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

        _js_label_noun = f"'{module}'" if module else (
            f"target '{target}'" if target else "all modules"
        )
        rows = _data_bounded(
            session,
            f"""
            MATCH (j:JSPatch {{odoo_version: $v}})
            WHERE ($own IS NULL OR (size(j.profile) > 0
                   AND all(__p IN j.profile WHERE __p IN $own OR __p IN $shared)))
              AND ($target IS NULL OR j.target = $target)
              AND ($module IS NULL OR j.module = $module)
              AND ($era IS NULL OR j.era = $era)
              AND j.module <> '__unresolved__'
            OPTIONAL MATCH (mod:Module {{name: j.module, odoo_version: $v}})
            WITH j, mod,
                 {_edition_rank_cypher("mod")},
                 mod.name AS mod_name
            RETURN j.target AS target, j.patch_name AS patch_name,
                   j.era AS era, j.module AS module, coalesce(mod.repo_url, mod.repo) AS repo,
                   j.file_path AS file_path,
                   edition_rank, mod_name
            ORDER BY edition_rank ASC, mod_name ASC, j.target ASC, j.patch_name ASC
            SKIP $skip
            LIMIT $limit
            """,
            f"JS patches for {_js_label_noun} (Odoo {odoo_version})",
            v=odoo_version, target=target, module=module, era=era_filter,
            **_scope(profile_name), skip=start_index, limit=effective_limit,
        )

        total_rec = _single_bounded(
            session,
            """
            MATCH (j:JSPatch {odoo_version: $v})
            WHERE ($own IS NULL OR (size(j.profile) > 0
                   AND all(__p IN j.profile WHERE __p IN $own OR __p IN $shared)))
              AND ($target IS NULL OR j.target = $target)
              AND ($module IS NULL OR j.module = $module)
              AND ($era IS NULL OR j.era = $era)
              AND j.module <> '__unresolved__'
            RETURN count(j) AS c
            """,
            f"JS patch count for {_js_label_noun} (Odoo {odoo_version})",
            v=odoo_version, target=target, module=module, era=era_filter,
            **_scope(profile_name),
        )
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
            f"module_inspect(name='{mod_name}', method='js', odoo_version='{odoo_version}',"
            f" start_index={start_index + cap})"
        )
        raw_rows = [r for r, _ in sub_items]

        def _fmt_js_patch(r: dict) -> str:
            base = f"{r['target']}.{r['patch_name']} : era={r.get('era') or '?'}"
            if r.get("file_path"):
                # ADR-0037: repo-relative path. Anchor on module (r['repo'] is now
                # the portable git URL via coalesce, not a path-prefix anchor).
                pp = _portable_path(r["file_path"], module=r.get("module"))
                base += f" | {pp}"
            return base

        rendered = _render_capped(
            raw_rows,
            _fmt_js_patch,
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
    rows = session.run(f"""
        MATCH (mth:Method {{name: $method, model: $model, odoo_version: $v}})
        WHERE {_scope_pred("mth")}
        RETURN mth.decorators AS decorators,
               mth.convention_kind AS ck,
               mth.super_safety AS ss,
               coalesce(mth.has_super_call, false) AS has_super,
               mth.signature AS signature
        ORDER BY mth.module
    """, method=method, model=model, v=version, **_scope()).data()
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
    _NULL_HINT = "(signature not available for this version)"
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
    # ADR-0034 WI-4 (R-09 fix): apply tenant boundary filter even though
    # find_override_point has no profile_name param — use None so admin is
    # unrestricted and tenant boundary is still enforced via _effective_allowed.
    with driver.session() as session:
        records = session.run("""
            MATCH (mth:Method {name: $method, model: $model, odoo_version: $v})
            WHERE ($own IS NULL OR (size(mth.profile) > 0
                   AND all(__p IN mth.profile WHERE __p IN $own OR __p IN $shared)))
            OPTIONAL MATCH (mod:Module {name: mth.module, odoo_version: $v})
            RETURN mth.module AS module, mth.convention_kind AS ck,
                   mth.super_safety AS ss, mth.return_required AS rr,
                   coalesce(mth.has_super_call, false) AS has_super,
                   coalesce(mod.repo_url, mod.repo) AS repo, mod.edition AS edition
            ORDER BY mth.module
        """, method=method, model=model, v=v, **_scope(None)).data()

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
async def suggest_pattern(
    intent: str,
    odoo_version: RequiredOdooVersion,
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
        language: 'python' | 'xml' | 'js' | 'all'. Default 'python'.
        limit: Max patterns to return (default 5).

    Returns:
        Tree list of patterns ranked by relevance score, each with snippet (first
        5 lines), file ref, and gotchas. Empty index → instruction to seed.

    Example:
        suggest_pattern("override write to read old value", "17.0")
        → suggest_pattern('override write to read old value', 17.0, ...) — 1 matches
          └─ #1 · score 0.81 · write-read-before-super
              ├─ Language: python (min v17.0)
              └─ Gotchas:
                   • Reading old values AFTER super().write() returns new value
    """
    # #227: guard cheaply (empty/invalid → sync impl returns the error string),
    # then embed async + offload the blocking body to a worker thread.
    if not intent.strip() or language not in _VALID_PATTERN_LANGUAGES:
        return _suggest_pattern(intent, odoo_version, language, limit)
    from src.embedding.instructions import INSTRUCT_NL_TO_CODE
    try:
        embedder = _get_embedder()
    except Exception:
        logger.warning("suggest_pattern: embedder unavailable", exc_info=True)
        return (
            "suggest_pattern: embedder unavailable.\n"
            "Hint: check Ollama is running (default: http://localhost:11434)."
        )
    try:
        instruct = getattr(embedder, "query_instruction", INSTRUCT_NL_TO_CODE)
        intent_vec = await _embed_query(embedder, instruct, intent)
    except EmbedOverloaded as e:
        return f"suggest_pattern: {e}"
    except Exception:
        logger.warning("suggest_pattern: embedding query failed", exc_info=True)
        return (
            "suggest_pattern: embedding query failed — try again shortly, "
            "or verify the embedder service is reachable."
        )
    return await asyncio.to_thread(
        _suggest_pattern,
        intent, odoo_version, language, limit,
        _embedder=embedder, _query_vec=intent_vec,
    )


@mcp.tool(**READONLY_TOOL_KWARGS)
@offload
def check_module_exists(
    name: str,
    odoo_version: RequiredOdooVersion,
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
        profile_name: Optional inheritance-resolved profile filter. When set,
            narrows the check to modules visible in this profile (including
            parent profiles via the ancestor chain). Default None checks all.

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
@offload
def find_override_point(
    model: str, method: str, odoo_version: RequiredOdooVersion, to_version: str = "",
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
        odoo_version: From-version when in diff mode (see field schema for
            the required-version contract).
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


# Health endpoint — registered as custom route on MCP app
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request):
    from src.mcp.health import health_handler
    return await health_handler(request)


# Readiness endpoint (WI-D) — cached DB-count readiness probe. Distinct from
# /health liveness: /ready reports whether the index is populated and both DBs
# are reachable, reading from the shared TTL cache so it never scans on the hot
# path. Registered as an HTTP custom route (NOT an MCP tool — tool count is 25 after WI-4).
@mcp.custom_route("/ready", methods=["GET"])
async def ready_check(request: Request):
    from src.mcp.health import ready_handler
    return await ready_handler(request)


# Prometheus metrics endpoint — no auth (mirroring /health bypass in middleware.py).
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
    ``session_idle_timeout <= 0`` (it has no "0 = disable" affordance — that is
    expressed as ``None``, which Option B never passes). A misconfigured ``<= 0``
    would therefore crash startup AND, if it didn't, silently disable reaping —
    re-opening the #279 leak. We are NOT offering an intentional opt-out here, so
    clamp any ``<= 0`` back to the 3600s default and log a warning.

    ``_resolve_orm_float`` parses ``SESSION_IDLE_TIMEOUT=nan`` / ``=inf`` to a
    float without raising (Python ``float()`` accepts both), and ``nan <= 0`` is
    ``False`` — so a bare ``<= 0`` guard would let a non-finite value through to
    the SDK. ``nan`` yields a deadline that never compares true, ``inf`` a
    never-expiring one: either silently disables reaping and re-opens #279. We
    therefore reject any non-finite value the same way as ``<= 0``.
    """
    resolved = _resolve_orm_float("SESSION_IDLE_TIMEOUT", 3600.0)
    if not math.isfinite(resolved) or resolved <= 0:
        logging.getLogger(__name__).warning(
            "SESSION_IDLE_TIMEOUT=%s is non-finite or <= 0 — that would disable "
            "streamable-http session reaping (re-opening the #279 leak) and is "
            "rejected by the MCP SDK. Falling back to the 3600s (1h) default.",
            resolved,
        )
        return 3600.0
    return resolved


def _build_streamable_http_app(*, idle_timeout: float, middleware, mcp_server=None):
    """Build the Option B streamable-http app core (#279 item 1, ADR-0049).

    Single source of truth for the manual ``create_streamable_http_app()``
    reproduction — both ``main()`` and ``tests/test_session_idle_timeout.py``
    call this so the construction can never drift out of lockstep (FIX 3).

    Returns ``(app, session_manager)``. The caller (``main()``) is responsible
    for the steps that are NOT part of the Option B core: wrapping the router
    lifespan with ``_lifespan_with_pg``, mounting ``/install`` + the feedback
    sub-app, and running uvicorn.

    FastMCP's ``mcp.http_app()`` / ``create_streamable_http_app()`` do NOT forward
    ``session_idle_timeout`` to ``StreamableHTTPSessionManager`` (still
    ``DON'T MERGE`` upstream while we pin fastmcp<3.0), so we reproduce that
    factory here and add the one kwarg. The MCP SDK's manager DOES accept it.

    FRAGILE — depends on FastMCP private internals (``mcp._mcp_server``,
    ``mcp._lifespan_manager()``, ``mcp._get_additional_http_routes()``,
    ``mcp._deprecated_settings``) plus the public-but-undocumented
    ``StreamableHTTPASGIApp`` / ``create_base_app``. The smoke test guards drift.
    """
    from contextlib import asynccontextmanager as _asynccontextmanager

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
    # prod is unaffected). Read from the same source FastMCP.http_app() reads.
    _settings = _mcp._deprecated_settings
    _json_response = bool(_settings.json_response)
    _stateless = bool(_settings.stateless_http)
    # FIX C: forward debug the same way FastMCP.http_app() does
    # (debug=self._deprecated_settings.debug). getattr fallback keeps us safe if
    # a future fastmcp drops the attr — create_base_app(debug=) is a public kwarg.
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
        # _lifespan_with_pg). Starts/stops the session manager — mirrors the
        # lifespan FastMCP's create_streamable_http_app() would have built.
        async with _mcp._lifespan_manager(), session_manager.run():
            yield

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
):
    sys.modules.pop(_tool_mod, None)
del _tool_mod

from src.mcp.tools import inspect_tools as _inspect_tools  # noqa: E402,F401
from src.mcp.tools import orm_tools as _orm_tools  # noqa: E402,F401
from src.mcp.tools import session_tools as _session_tools  # noqa: E402,F401
from src.mcp.tools import spec as _spec_tools  # noqa: E402,F401
from src.mcp.tools import stylesheet as _stylesheet_tools  # noqa: E402,F401

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
    _cli_help,
    _compile_lint_pattern,
    _find_deprecated_usage,
    _format_deprecated_usage,
    _lint_check,
    _lint_check_xml,
    _lint_match_kind,
    _lookup_core_api,
    _match_lint_rule_lines,
    api_version_diff,
    cli_help,
    find_deprecated_usage,
    lint_check,
    lookup_core_api,
)

# Phase 2 re-exports: the two public stylesheet tools, plus the three impl
# helpers. _resolve_stylesheet / _find_style_override are imported by tests via
# src.mcp.server and resolved through srv. by the async wrapper at call time (so
# monkeypatch.setattr(srv, "_find_style_override", ...) keeps working).
# _literal_style_lookup must be re-exported too: _find_examples (still in this
# hub, moved in Phase 5) calls it by bare name in its style literal-first path.
from src.mcp.tools.stylesheet import (  # noqa: E402,F401
    _find_style_override,
    _literal_style_lookup,
    _resolve_stylesheet,
    find_style_override,
    resolve_stylesheet,
)

if __name__ == "__main__":
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
    # helper (single source of truth — tests/test_session_idle_timeout.py calls
    # the SAME helper so the two can never drift). main() owns the wrapping:
    # lifespan compose with _lifespan_with_pg, /install + feedback mounts, uvicorn.
    # ADR-0049 records the 3 triggers to revert to the http_app() kwarg once
    # upstream forwards session_idle_timeout. SESSION_IDLE_TIMEOUT (default 3600s
    # = 1h, value-guarded) reaps abandoned streamable-http sessions (#279).
    from pathlib import Path as _Path

    import uvicorn as _uvicorn
    from starlette.middleware import Middleware as _Middleware
    from starlette.staticfiles import StaticFiles as _StaticFiles

    from src.mcp.middleware import AuthMiddleware

    _session_idle_timeout = _resolve_session_idle_timeout()
    _app, _session_manager = _build_streamable_http_app(
        idle_timeout=_session_idle_timeout,
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

        # Bootstrap admin settings catalogue (idempotent, best-effort).
        # Runs after PG pool init attempt. Swallows errors so a missing
        # app_settings table (m13_010 not yet applied) never blocks startup.
        try:
            from src.settings_registry import bootstrap_settings_safe as _bootstrap
            await _asyncio.to_thread(_bootstrap)
        except Exception:  # noqa: BLE001
            pass  # non-fatal — logged inside bootstrap_settings_safe

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

    from src.web_ui.routes import deploy_key as _deploy_key
    from src.web_ui.routes import feedback as _feedback

    _feedback_app = _FastAPI()
    _feedback_app.include_router(_feedback.router)
    # Mount tenant self-service deploy-key endpoint (ADR-0034 D7, WI-I).
    # GET /api/tenant/deploy-key — tenant_id resolved from X-API-Key auth state,
    # never from a user-supplied path parameter (cross-tenant leakage impossible).
    _feedback_app.include_router(_deploy_key.router)
    _app.mount("", _feedback_app)

    # #227 backpressure: cap the number of concurrent connections uvicorn will
    # service. Beyond this, uvicorn returns HTTP 503 immediately instead of
    # letting the accept-backlog grow unbounded (which turns overload into
    # latency + OOM). There are now THREE independent inner bounds — the embed
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
