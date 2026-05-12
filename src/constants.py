# src/constants.py — Single source of truth for all magic numbers and string constants.
#
# Rules:
#   - NO imports from src.indexer.* or src.mcp.* (prevents circular imports)
#   - All callers: `from src.constants import XYZ`

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
})

# ---------------------------------------------------------------------------
# Pagination & search limits
# ---------------------------------------------------------------------------

FIND_EXAMPLES_ANN_LIMIT: int = 20       # hard cap on pgvector ANN query rows
FIND_EXAMPLES_DEFAULT_LIMIT: int = 5    # user-facing default when limit unspecified
SNIPPET_PREVIEW_MAX_LINES: int = 5
ERROR_MSG_MAX_CHARS: int = 100
CODE_PREVIEW_MAX_CHARS: int = 60

# ---------------------------------------------------------------------------
# Impact analysis risk thresholds
# Validated 2026-05-11 against 25-case curated incident set, macro-F1 = 1.0000
# ---------------------------------------------------------------------------

IMPACT_RISK_HIGH_THRESHOLD: int = 10
IMPACT_RISK_MED_THRESHOLD: int = 4

# ---------------------------------------------------------------------------
# Batch sizes
# ---------------------------------------------------------------------------

NEO4J_WRITE_BATCH_SIZE: int = 500
EMBEDDER_MAX_BATCH: int = 50    # empirical: ~22s per 100 texts on qwen3-embedding-q5km

# ---------------------------------------------------------------------------
# Timeouts (seconds)
# ---------------------------------------------------------------------------

TIMEOUT_GIT_CLONE: int = 600
TIMEOUT_GIT_DIFF: int = 10
TIMEOUT_GIT_SCAN: int = 10
TIMEOUT_EMBEDDER_REQUEST: int = 600    # Ollama runs ~60s; 600s covers large core modules

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

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

DEFAULT_RATE_LIMIT_RPM: int = 120
