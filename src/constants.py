# SPDX-License-Identifier: AGPL-3.0-or-later
# src/constants.py — Single source of truth for all magic numbers and string constants.
#
# Rules:
#   - NO imports from src.indexer.* or src.mcp.* (prevents circular imports)
#   - All callers: `from src.constants import XYZ`

import os

# ---------------------------------------------------------------------------
# Odoo version era boundaries
# ---------------------------------------------------------------------------

# v8/v9: __openerp__.py manifest, _columns dict, Python 2 AST syntax
# v10+: __manifest__.py, class-level fields, modern AST
LEGACY_ERA_MAX_MAJOR: int = 9

# openerp/ namespace (v8/v9) vs odoo/ namespace (v10+)
ODOO_NAMESPACE_LEGACY_MAX_MAJOR: int = 9

# test_lint addon available starting from this major version
LINT_RULES_MIN_MAJOR: int = 17

# ---------------------------------------------------------------------------
# Neo4j relationship type strings
# ---------------------------------------------------------------------------

REL_INHERITS: str = "INHERITS"
REL_INHERITS_VIEW: str = "INHERITS_VIEW"
REL_DEFINED_IN: str = "DEFINED_IN"
REL_DEPENDS_ON: str = "DEPENDS_ON"
REL_REPLACED_BY: str = "REPLACED_BY"
REL_CHECKS: str = "CHECKS"
REL_USES_CORE_SYMBOL: str = "USES_CORE_SYMBOL"
REL_OF_COMMAND: str = "OF_COMMAND"
REL_TARGETS_MODEL: str = "TARGETS_MODEL"
REL_IMPORTS: str = "IMPORTS"
REL_HAS_VIOLATION: str = "HAS_VIOLATION"
# A2d — method-to-field provenance edges
REL_USES_FIELD: str = "USES_FIELD"
REL_DEPENDS_ON_FIELD: str = "DEPENDS_ON_FIELD"

# ---------------------------------------------------------------------------
# Edition metadata
# ---------------------------------------------------------------------------

# Priority order used in Cypher CASE ranking (lower = higher priority in results)
EDITION_PRIORITY: dict[str, int] = {
    "community": 0,
    "enterprise": 1,
    "viindoo": 2,
    "oca": 3,
}
EDITION_PRIORITY_ELSE: int = 4
VALID_EDITIONS: frozenset[str] = frozenset(EDITION_PRIORITY) | {"custom"}

# ---------------------------------------------------------------------------
# Global embedding sentinel
# ---------------------------------------------------------------------------

# Sentinel profile_name for global (cross-tenant) rows in the embeddings table.
# MUST equal the value in migrations/m13_021_embeddings_global_sentinel.sql
# (backfill, RLS policy branch, sentinel CHECK). Drift here silently breaks
# suggest_pattern / global visibility.
GLOBAL_PROFILE: str = "__global__"

# ---------------------------------------------------------------------------
# pgvector chunk types
# ---------------------------------------------------------------------------

VALID_CHUNK_TYPES: frozenset[str] = frozenset({
    "method", "field", "view", "qweb", "js_era1", "js_era2", "js_era3",
    "css", "scss", "less",
})

# ---------------------------------------------------------------------------
# Pagination & search limits
# ---------------------------------------------------------------------------

FIND_EXAMPLES_ANN_LIMIT: int = 20       # hard cap on pgvector ANN query rows
FIND_EXAMPLES_DEFAULT_LIMIT: int = 5    # user-facing default when limit unspecified

# --- Issue #255 ---
# HNSW post-filter recall mitigation for filtered semantic queries (pgvector >=0.8).
# 'relaxed_order' lets HNSW keep scanning until LIMIT is met *after* the post-filter.
# Env-gated kill-switch: set HNSW_ITERATIVE_SCAN='' (empty) to disable at runtime
# without a code change (falls back to the server default 'off'). See ADR-0047.
HNSW_ITERATIVE_SCAN: str = os.getenv("HNSW_ITERATIVE_SCAN", "relaxed_order")

# Chunk types that participate in literal-first style lookup (issue #255).
# SSOT for the ('css', 'scss', 'less') triple — replaces the hard-coded tuple in
# server.py _find_style_override and _find_examples css/scss/less path.
STYLE_CHUNK_TYPES: frozenset[str] = frozenset({"css", "scss", "less"})
SNIPPET_PREVIEW_MAX_LINES: int = 5
ERROR_MSG_MAX_CHARS: int = 100
CODE_PREVIEW_MAX_CHARS: int = 60

# List-tool preview caps — see ADR-0023 §3. Default applies to list_* tools and
# any unbounded sublist (Extended by, deprecated usage hits, etc.). Per-tool
# overrides exist because field-heavy models and verbose JS patches have
# different readability sweet spots.
LIST_PREVIEW_MAX_ITEMS: int = 20    # Default cap for list_* and sublists (ADR-0023 §3)
LIST_PREVIEW_FIELDS_MAX: int = 50   # account.move has ~150 fields; 20 too restrictive
LIST_PREVIEW_PATCHES_MAX: int = 10  # JS patches are verbose; 10 sufficient for overview
IMPACT_MODULES_MAX: int = 30        # impact_analysis dependent-modules preview cap (G1)

# ---------------------------------------------------------------------------
# Impact analysis risk thresholds
# Validated 2026-05-11 against 25-case curated incident set, macro-F1 = 1.0000
# ---------------------------------------------------------------------------

IMPACT_RISK_HIGH_THRESHOLD: int = 10
IMPACT_RISK_MED_THRESHOLD: int = 4

# ---------------------------------------------------------------------------
# Batch sizes
# ---------------------------------------------------------------------------

# NEO4J_WRITE_BATCH_SIZE: rows per Neo4j transaction.
# Override via NEO4J_WRITE_BATCH_SIZE env var.
NEO4J_WRITE_BATCH_SIZE: int = int(os.getenv("NEO4J_WRITE_BATCH_SIZE", "500"))

# NEO4J_DELETE_BATCH_ROWS: rows per inner transaction for CALL {} IN TRANSACTIONS
# DELETE batches (delete_modules_scoped + cleanup scripts).
# Intentionally separate from NEO4J_WRITE_BATCH_SIZE: delete batches should be
# much larger (each row is a single DELETE, not a multi-property MERGE) and are
# auto-commit transactions that live outside the normal write batch semantics.
# The cleanup script ops/cleanup_same_name_inherits_mesh.cypher uses the same
# default (10000) — if you change this constant, update the script comment too.
NEO4J_DELETE_BATCH_ROWS: int = int(os.getenv("NEO4J_DELETE_BATCH_ROWS", "10000"))

# EMBEDDER_MAX_BATCH: texts per Ollama /api/embed call.
# Empirical (production, qwen3-embedding-q5km behind Ollama): a 50-text batch
# takes ~10-56s depending on text density and concurrent profile-worker load.
# Keep at 50 to stay clear of any reverse-proxy proxy_read_timeout on a slow box.
# Override via EMBEDDER_MAX_BATCH env var for tuning on faster hardware.
EMBEDDER_MAX_BATCH: int = int(os.getenv("EMBEDDER_MAX_BATCH", "50"))

# ---------------------------------------------------------------------------
# Timeouts (seconds)
# ---------------------------------------------------------------------------

# TIMEOUT_GIT_CLONE: subprocess timeout for full git clone (no --depth=1).
# v17+ odoo/odoo has 1M+ commits; fresh clone on a slow link takes 30+ min.
# Default 3600s (1h). Override via TIMEOUT_GIT_CLONE env var.
TIMEOUT_GIT_CLONE: int = int(os.getenv("TIMEOUT_GIT_CLONE", "3600"))

# TIMEOUT_GIT_DIFF: subprocess timeout for git diff/rev-parse commands.
# These are lightweight read-only ops, but a huge repo or slow disk can stall.
# Default 30s (was 10s). Override via TIMEOUT_GIT_DIFF env var.
TIMEOUT_GIT_DIFF: int = int(os.getenv("TIMEOUT_GIT_DIFF", "30"))

# TIMEOUT_GIT_SCAN: subprocess timeout for git rev-parse HEAD during scan.
# Default 30s (was 10s). Override via TIMEOUT_GIT_SCAN env var.
TIMEOUT_GIT_SCAN: int = int(os.getenv("TIMEOUT_GIT_SCAN", "30"))

# TIMEOUT_EMBEDDER_CONNECT: TCP connect timeout for Ollama /api/embed.
# Ollama is local or on LAN — 10s is generous. Override via EMBEDDER_TIMEOUT_CONNECT.
TIMEOUT_EMBEDDER_CONNECT: int = int(os.getenv("EMBEDDER_TIMEOUT_CONNECT", "10"))

# TIMEOUT_EMBEDDER_READ: between-chunks read timeout (httpx.ReadTimeout).
# For Ollama-style non-streaming /api/embed, this is effectively the full
# response wait. A 50-text batch on qwen3-embedding-q5km takes ~22s on fast
# hardware but can exceed 90s on CPU-only servers. Parallel profile workers
# queue behind a single Ollama instance, so headroom must be generous.
# 1200s (20 min). Override via EMBEDDER_TIMEOUT_READ or legacy EMBEDDER_TIMEOUT.
_embedder_timeout_default = os.getenv("EMBEDDER_TIMEOUT", "1200")
TIMEOUT_EMBEDDER_READ: int = int(os.getenv("EMBEDDER_TIMEOUT_READ", _embedder_timeout_default))

# TIMEOUT_EMBEDDER_WRITE: timeout for sending the request body to Ollama.
# Payload is a JSON list of texts — 50 texts is small, 30s is very generous.
# Override via EMBEDDER_TIMEOUT_WRITE.
TIMEOUT_EMBEDDER_WRITE: int = int(os.getenv("EMBEDDER_TIMEOUT_WRITE", "30"))

# TIMEOUT_EMBEDDER_READ_QUERY: read timeout for a single *query* embed (the
# online MCP/search path), kept deliberately short and separate from the
# 1200s batch-indexing read timeout. A query embeds one short text; if the
# embedder can't answer in 30s the caller should fail fast rather than block a
# user request for 20 minutes. Override via EMBEDDER_TIMEOUT_READ_QUERY.
TIMEOUT_EMBEDDER_READ_QUERY: int = int(os.getenv("EMBEDDER_TIMEOUT_READ_QUERY", "30"))

# Backward-compat alias — callers that imported TIMEOUT_EMBEDDER_REQUEST continue to work.
TIMEOUT_EMBEDDER_REQUEST: int = TIMEOUT_EMBEDDER_READ

# EMBEDDER_RETRY_BACKOFF_BASE: base delay (seconds) for exponential retry backoff
# in Qwen3Embedder._embed_one. Delay between attempt i and i+1 is
# min(base * 2**i, max). Default 2.0s. Override via EMBEDDER_RETRY_BACKOFF_BASE.
EMBEDDER_RETRY_BACKOFF_BASE: float = float(os.getenv("EMBEDDER_RETRY_BACKOFF_BASE", "2.0"))

# EMBEDDER_RETRY_BACKOFF_MAX: cap (seconds) on a single retry sleep so a slow
# Ollama box doesn't stall the indexer for minutes between attempts.
# Default 30.0s. Override via EMBEDDER_RETRY_BACKOFF_MAX.
EMBEDDER_RETRY_BACKOFF_MAX: float = float(os.getenv("EMBEDDER_RETRY_BACKOFF_MAX", "30.0"))

# ---------------------------------------------------------------------------
# Embedding defaults
# ---------------------------------------------------------------------------

DEFAULT_EMBEDDER_MODEL: str = "qwen3-embedding-q5km"
DEFAULT_EMBEDDER_DIM: int = 1024


def normalize_embedder_model_name(name: str | None) -> str | None:
    """Strip an optional Ollama ``:latest`` tag so model-name comparisons use
    the bare name on BOTH the configured side and the DB-stored side.

    Ollama treats ``foo`` and ``foo:latest`` as the same model, so the dim/model
    guard (``src/db/embedding_guard.py``) must not treat them as a model switch.
    Applied symmetrically: the embedder normalizes the name it stamps onto each
    vector row, and the guard normalizes both operands before comparing — so a
    ``:latest`` value on either side can never falsely trip ``EmbedderModelMismatch``.
    """
    if name is None:
        return None
    return name.removesuffix(":latest")


# EMBEDDER_BACKEND: which embedder provider make_embedder() constructs.
#   ollama -> Qwen3Embedder (Ollama /api/embed, Qwen INSTRUCT prefix on queries)
#   openai / tei -> OpenAICompatEmbedder (/v1/embeddings, OpenAI/Voyage/TEI/vLLM/LiteLLM)
#   fake -> FakeEmbedder (deterministic, no network — CI/tests)
# Override via EMBEDDER_BACKEND env var.
EMBEDDER_BACKEND: str = os.getenv("EMBEDDER_BACKEND", "ollama")

# EMBEDDER_NUM_CTX: the embedder model's context window in tokens. Used by the
# choke-point truncation safety-net so no single text is sent past what the
# model can encode. Mirror your Ollama Modelfile `num_ctx`. Override via
# EMBEDDER_NUM_CTX.
EMBEDDER_NUM_CTX: int = int(os.getenv("EMBEDDER_NUM_CTX", "4096"))

# EMBEDDER_TOKEN_BUDGET: target per-chunk token budget kept as a margin *below*
# num_ctx, so chunking (WI-B) leaves headroom for any instruction prefix and
# tokenizer drift. Override via EMBEDDER_TOKEN_BUDGET.
EMBEDDER_TOKEN_BUDGET: int = int(os.getenv("EMBEDDER_TOKEN_BUDGET", "3500"))

# EMBEDDER_CHARS_PER_TOKEN: conservative chars-per-token ratio for the cheap
# heuristic token estimate (no real tokenizer dependency). Deliberately LOW:
# a low ratio over-estimates token count, which over-truncates / over-splits —
# the safe direction. Code and XML pack more tokens per char than prose, so 3.0
# leaves margin. Override via EMBEDDER_CHARS_PER_TOKEN.
EMBEDDER_CHARS_PER_TOKEN: float = float(os.getenv("EMBEDDER_CHARS_PER_TOKEN", "3.0"))

# EMBEDDER_TRUNCATE_CHARS_PER_TOKEN: worst-case chars-per-token ratio used
# EXCLUSIVELY as a safety-net floor in _truncate_to_ctx().  Code with dense
# tokens (identifiers, operators, short words) can approach 1–2 chars/token.
# Using 2.0 here ensures that after truncation the estimated token count is
# ≤ num_ctx even for token-dense code, regardless of the (higher) estimation
# ratio used elsewhere.  The main chunking cap (WI-B) runs before this; this
# is a last-resort guard and intentionally conservative.
# Override via EMBEDDER_TRUNCATE_CHARS_PER_TOKEN.
EMBEDDER_TRUNCATE_CHARS_PER_TOKEN: float = float(
    os.getenv("EMBEDDER_TRUNCATE_CHARS_PER_TOKEN", "2.0")
)

# EMBEDDER_MAX_CONCURRENCY: ceiling on concurrent in-flight embed requests for
# async callers that fan out (e.g. an asyncio.Semaphore around embed_async).
# Override via EMBEDDER_MAX_CONCURRENCY.
EMBEDDER_MAX_CONCURRENCY: int = int(os.getenv("EMBEDDER_MAX_CONCURRENCY", "4"))

# ---------------------------------------------------------------------------
# Neo4j query execution timeout
# ---------------------------------------------------------------------------

# NEO4J_QUERY_TIMEOUT_SECONDS: per-query server-side transaction timeout passed
# via neo4j.Query(text, timeout=...) on the ORM-tool read path. Bounds the
# variable-length / dense-inheritance traversals so a runaway query becomes a
# fast, actionable error instead of an indefinite hang (issue #273: 11 zombie
# transactions ran 19-24h on prod because db.transaction.timeout was 0s and no
# driver/session/query timeout was set anywhere). The rewritten per-hop
# name-dedup queries run ~0.5-1.0s on the worst prod models, so 30s is a very
# generous ceiling that only ever fires on genuine pathology. Override via
# NEO4J_QUERY_TIMEOUT_SECONDS.
NEO4J_QUERY_TIMEOUT_SECONDS: int = int(os.getenv("NEO4J_QUERY_TIMEOUT_SECONDS", "30"))

# ORM_QUERY_MAX_CONCURRENCY caps in-flight ORM-validation tool queries (the 4
# offload_bounded tools). A fan-out burst of dense ORM traversals must not drain
# the shared asyncio.to_thread ThreadPoolExecutor that every @offload tool uses,
# nor exhaust the Neo4j connection pool. Mirrors EMBEDDER_MAX_CONCURRENCY for the
# embed path (ADR-0046). Default 8 sits comfortably below the ~24-connection
# Neo4j pool, leaving headroom for non-ORM reads + the brief fast-reject window.
ORM_QUERY_MAX_CONCURRENCY: int = int(os.getenv("ORM_QUERY_MAX_CONCURRENCY", "8"))

# ORM_SLOT_ACQUIRE_TIMEOUT: max wait (seconds) for an ORM concurrency slot before
# fast-reject. Must stay well below NEO4J_QUERY_TIMEOUT_SECONDS so an overloaded
# server rejects quickly rather than holding a slot for the full traversal
# window. Enforced at server startup (see server.py _validate_orm_env).
ORM_SLOT_ACQUIRE_TIMEOUT: float = float(os.getenv("ORM_SLOT_ACQUIRE_TIMEOUT", "5"))

# NONORM_READ_MAX_CONCURRENCY caps in-flight NON-ORM heavy reads that are wrapped
# in offload_bounded_nonorm (currently impact_analysis — a 6-query fan-out over
# TARGETS_MODEL / DEPENDS_ON / BOUND_TO / PATCHES that can run long on a dense
# graph). Kept as a SEPARATE pool from ORM_QUERY_MAX_CONCURRENCY (issue #276 G6):
# a fan-out burst of one class must never starve the other. Default 8 mirrors the
# ORM cap and sits below the ~24-connection Neo4j pool. Override via
# NONORM_READ_MAX_CONCURRENCY.
NONORM_READ_MAX_CONCURRENCY: int = int(os.getenv("NONORM_READ_MAX_CONCURRENCY", "8"))

# NONORM_SLOT_ACQUIRE_TIMEOUT: max wait (seconds) for a non-ORM read slot before
# fast-reject. Same fast-reject contract as ORM_SLOT_ACQUIRE_TIMEOUT — must stay
# strictly below NEO4J_QUERY_TIMEOUT_SECONDS so an overloaded server rejects
# quickly instead of pinning a worker-thread slot for the full query window.
# Enforced at server startup (see server.py _validate_orm_env).
NONORM_SLOT_ACQUIRE_TIMEOUT: float = float(
    os.getenv("NONORM_SLOT_ACQUIRE_TIMEOUT", "5")
)

# EMBEDDER_SLOT_ACQUIRE_TIMEOUT: max wait (seconds) for a query-embed concurrency
# slot before fast-reject (EmbedOverloaded). Must stay strictly below the query
# read timeout (TIMEOUT_EMBEDDER_READ_QUERY) so an overloaded embedder rejects
# fast rather than pinning a worker thread for the whole embed window (issue #276
# G7). Enforced at server startup (see server.py _validate_orm_env).
EMBEDDER_SLOT_ACQUIRE_TIMEOUT: float = float(
    os.getenv("EMBEDDER_SLOT_ACQUIRE_TIMEOUT", "5")
)

# ---------------------------------------------------------------------------
# PostgreSQL connection pool
# ---------------------------------------------------------------------------

PG_POOL_MIN_CONN: int = 1
PG_POOL_MAX_CONN: int = 10
# Bound psycopg2.connect() so a dead/unreachable PG fails fast (TCP RST or
# the timeout) instead of hanging the caller. Used by every init_pool() call.
PG_CONNECT_TIMEOUT_SECONDS: int = 5
# Background reconnect cadence for the MCP lifespan handler when the pool
# failed to initialise at startup. Also drives the `Retry-After` header
# returned by AuthMiddleware in degraded mode — clients re-poll on this
# cadence and the next attempt has a high chance of seeing pool ready.
PG_BG_RETRY_INTERVAL_SECONDS: int = 30

# ---------------------------------------------------------------------------
# CLI diagnostics
# ---------------------------------------------------------------------------

# urllib timeout for `python -m src.cli diagnose` probing the MCP /health
# endpoint. Distinct from PG timeout so HTTP probe semantics stay decoupled.
MCP_HEALTH_PROBE_TIMEOUT_SECONDS: int = 5

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

DEFAULT_RATE_LIMIT_RPM: int = 120

# ---------------------------------------------------------------------------
# Magic fields — ORM auto-injects these at runtime; not declared in source.
# Value: (ttype, comodel_name | None). Synthetic at query-time only.
# ---------------------------------------------------------------------------

MAGIC_FIELDS: dict[str, tuple[str, str | None]] = {
    "id": ("integer", None),
    "display_name": ("char", None),
    "create_uid": ("many2one", "res.users"),
    "create_date": ("datetime", None),
    "write_uid": ("many2one", "res.users"),
    "write_date": ("datetime", None),
}

# ---------------------------------------------------------------------------
# Domain operators — VERSION-AWARE (M10.5 P2, used by validate_domain).
# Cross-version survey v8→v19 (12 Haiku agents): the term-operator set is NOT
# constant across Odoo majors. `parent_of` arrived in v9; `any`/`not any` in
# v17; v19 added access-rights-bypass variants (`any!`/`not any!`) + explicit
# `not =like`/`not =ilike`. The `=like`/`=ilike` family persists v8→v19.
# Relational set ttypes are lowercase as stored in Neo4j Field.ttype.
# ---------------------------------------------------------------------------

RELATIONAL_TTYPES: frozenset[str] = frozenset({"many2one", "one2many", "many2many"})

# Term operators available in every supported Odoo major (v8+).
_DOMAIN_OPERATORS_BASE: frozenset[str] = frozenset({
    "=", "!=", "<", "<=", ">", ">=", "=?",
    "=like", "=ilike", "like", "not like", "ilike", "not ilike",
    "in", "not in", "child_of",
})


def valid_domain_operators(odoo_version: str) -> frozenset[str]:
    """Return the set of valid domain term-operators for an Odoo version.

    Unknown / unparseable versions return a permissive superset (all operators)
    so validate_domain never flags a false positive on an unrecognised version.
    """
    try:
        major = int(str(odoo_version).split(".")[0])
    except (ValueError, IndexError, AttributeError):
        major = 99  # unknown → permissive
    ops = set(_DOMAIN_OPERATORS_BASE)
    if major >= 9:
        ops.add("parent_of")
    if major >= 17:
        ops |= {"any", "not any"}
    if major >= 19:
        ops |= {"any!", "not any!", "not =like", "not =ilike"}
    return frozenset(ops)


# ---------------------------------------------------------------------------
# License policy engine (ADR-0036)
# ---------------------------------------------------------------------------

# Config-driven map: license class → action ∈ {serve, ingest_flagged, skip}.
# 'ingest_flagged' is a valid third action — assign it here to any license class
# to host-but-not-serve (e.g. to stage OEEL-1 content ahead of a written
# permission from Odoo S.A. without exposing it to AI clients).
# Changing any action here is sufficient to change posture — no code change.
LICENSE_POLICY: dict[str, str] = {
    "LGPL-3":   "serve",
    "AGPL-3":   "serve",
    "GPL-3":    "serve",
    "OPL-1":    "serve",
    "unknown":  "serve",
    # OEEL-1: Odoo S.A. Enterprise — Viindoo's own Partnership obligation (ADR-0036 D4).
    # Default skip (no derivation until written permission obtained).
    # To enable: flip to 'serve' or 'ingest_flagged' — config change only, no code change.
    "OEEL-1":   "skip",
}


def default_license_for_missing(major: int) -> str:
    """Return the implied license for a module with no explicit 'license' key.

    v8 repo base is AGPL-3; v9+ base is LGPL-3.  Both resolve to 'serve' in
    LICENSE_POLICY.  Recording the accurate value (rather than 'unknown') is
    important so that future policy changes are data-driven, not code-driven.
    """
    return "AGPL-3" if major <= 8 else "LGPL-3"


def license_policy_action(license_value: str) -> str:
    """Return the LICENSE_POLICY action for a license string.

    Falls back to 'serve' for unmapped license strings — submitter bears
    responsibility per ADR-0036 D5 (Terms of Service representation).
    """
    return LICENSE_POLICY.get(license_value, "serve")


# ---------------------------------------------------------------------------
# Resource body limits
# ---------------------------------------------------------------------------

# Maximum bytes served for odoo://stylesheet/{...} resources.
# Large compiled CSS/Bootstrap bundles can exceed MCP response budget; per-file
# SCSS sources are typically 2–20 KB so 128 KB is generous for real stylesheet
# files while blocking accidental huge-file reads (ADR-0030 stylesheet resource;
# output-gap G5).
STYLESHEET_RESOURCE_MAX_BYTES: int = 131_072  # 128 KB
