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
# pgvector chunk types
# ---------------------------------------------------------------------------

VALID_CHUNK_TYPES: frozenset[str] = frozenset({
    "method", "field", "view", "qweb", "js_era1", "js_era2", "js_era3",
    "css", "scss",
})

# ---------------------------------------------------------------------------
# Pagination & search limits
# ---------------------------------------------------------------------------

FIND_EXAMPLES_ANN_LIMIT: int = 20       # hard cap on pgvector ANN query rows
FIND_EXAMPLES_DEFAULT_LIMIT: int = 5    # user-facing default when limit unspecified
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

# EMBEDDER_MAX_BATCH: texts per Ollama /api/embed call.
# Empirical: ~22s per 100 texts on qwen3-embedding-q5km.
# Keep at 50 to stay well under any reverse-proxy proxy_read_timeout (120s).
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
