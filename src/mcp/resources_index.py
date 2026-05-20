# SPDX-License-Identifier: AGPL-3.0-or-later
# src/mcp/resources_index.py
"""Discovery index for MCP Resources — top-100 popular models per indexed version.

Pattern 8 (MCP Resources) — Wave F WI-F2.

`list_resources_index()` returns a flat list of resource descriptors suitable
for use as the `resources/list` response payload. Each entry carries:

  - ``uri``         — stable ``odoo://`` URI for the resource
  - ``mimeType``    — always ``"text/markdown"`` (model resources are markdown)
  - ``name``        — dotted model name, e.g. ``"sale.order"``
  - ``description`` — one-line human-readable label

Popularity metric: count of inbound DEPENDS_ON edges on Module nodes that
define the model (`m.is_definition = true`), breaking ties by model name ASC.
This mirrors the T3 ranking tier used in `_resolve_model` (server.py).

The query is capped at LIMIT 100 per version to keep the `resources/list`
response within MCP protocol budget (~700 entries for 7 indexed versions).

Requires: Neo4j driver initialised via `_get_driver()` in server.py.  This
module intentionally does NOT import from server.py to avoid circular deps —
it calls `_get_driver` lazily from the shared singleton in server.py via an
explicit import at call time.
"""

from __future__ import annotations

from src.constants import REL_DEPENDS_ON

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_PER_VERSION: int = 100
"""Maximum number of resource entries returned per indexed Odoo version."""

_MIME_TYPE: str = "text/markdown"
"""MIME type for all model resources — content is rendered as markdown."""

_URI_SCHEME: str = "odoo"
"""URI scheme for the odoo:// resource namespace (ADR-0030)."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_resources_index() -> list[dict[str, str]]:
    """Return up to top-100 most-depended-on models per indexed Odoo version.

    For each version present in the index, runs a Cypher query that:

      1. Matches all Model nodes for that version where ``is_definition = true``
         (the canonical defining module — mirrors ADR-0013 T1 tier).
      2. Counts inbound DEPENDS_ON edges on the defining Module as popularity
         score (same as T3 tier in ``_resolve_model``).
      3. Orders by dep_count DESC, model name ASC (deterministic tiebreak
         per CLAUDE.md Neo4j 5.x gotcha).
      4. Limits to 100 entries per version.

    Returns a list of dicts with keys: ``uri``, ``mimeType``, ``name``,
    ``description``. All values are strings. Empty list when the index is empty.

    The list is ordered: versions descending (newest first), then by
    dep_count DESC, name ASC within each version. Callers (e.g. FastMCP's
    ``resources/list`` handler) may re-sort as needed.

    Thread-safe: each call opens a fresh Neo4j session and reads only; no
    shared mutable state in this module.
    """
    # Lazy import from server to avoid circular dependency at module load time.
    # server.py owns the _driver singleton and _get_driver() factory.
    from src.mcp.server import _get_driver  # type: ignore[import]

    driver = _get_driver()
    entries: list[dict[str, str]] = []

    with driver.session() as session:
        versions = _fetch_indexed_versions(session)
        for version in versions:
            version_entries = _fetch_top_models(session, version)
            entries.extend(version_entries)

    return entries


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _fetch_indexed_versions(session) -> list[str]:
    """Return all indexed Odoo versions, newest first (numeric sort).

    Excludes ``'unknown'`` and any non-semver strings (same filter as
    ``_latest_version()`` in server.py).
    """
    records = session.run("""
        MATCH (m:Module)
        WITH DISTINCT m.odoo_version AS v
        WHERE v <> 'unknown' AND v =~ '\\d+\\.\\d+'
        RETURN v
        ORDER BY toInteger(split(v, '.')[0]) DESC,
                 toInteger(split(v, '.')[1]) DESC
    """).data()
    return [r["v"] for r in records]


def _fetch_top_models(session, version: str) -> list[dict[str, str]]:
    """Return up to _MAX_PER_VERSION resource descriptors for *version*.

    The Cypher query:
      - Joins Model to its defining Module via DEFINED_IN (is_definition).
      - Counts inbound DEPENDS_ON on the Module as dep_count.
      - Orders by dep_count DESC, m.name ASC.
      - Limits to _MAX_PER_VERSION.

    Falls back gracefully: if no model has is_definition=true for a version
    (e.g. partial index), includes all models ordered by dep_count + name.
    """
    records = session.run(
        f"""
        MATCH (m:Model {{odoo_version: $v}})-[:DEFINED_IN]->(mod:Module)
        WHERE coalesce(m.is_definition, false) = true
        WITH m, mod,
             COUNT {{ ()-[:{REL_DEPENDS_ON}]->(mod) }} AS dep_count
        RETURN m.name AS model_name,
               dep_count
        ORDER BY dep_count DESC, m.name ASC
        LIMIT {_MAX_PER_VERSION}
        """,
        v=version,
    ).data()

    # Fallback: if no model has is_definition=true yet (pre-reindex state),
    # run without the is_definition filter so the index is still useful.
    if not records:
        records = session.run(
            f"""
            MATCH (m:Model {{odoo_version: $v}})-[:DEFINED_IN]->(mod:Module)
            WITH m, mod,
                 COUNT {{ ()-[:{REL_DEPENDS_ON}]->(mod) }} AS dep_count
            RETURN m.name AS model_name,
                   dep_count
            ORDER BY dep_count DESC, m.name ASC
            LIMIT {_MAX_PER_VERSION}
            """,
            v=version,
        ).data()

    return [_make_entry(version, r["model_name"]) for r in records]


def _make_entry(version: str, model_name: str) -> dict[str, str]:
    """Build a single resource descriptor dict for *model_name* at *version*."""
    return {
        "uri": _build_uri(version, model_name),
        "mimeType": _MIME_TYPE,
        "name": model_name,
        "description": f"Odoo model {model_name} (v{version})",
    }


def _build_uri(version: str, model_name: str) -> str:
    """Build canonical odoo:// URI for a model resource.

    Format: ``odoo://<version>/model/<model.name>``
    Example: ``odoo://17.0/model/sale.order``

    ADR-0030 governs the URI scheme grammar.
    """
    return f"{_URI_SCHEME}://{version}/model/{model_name}"
