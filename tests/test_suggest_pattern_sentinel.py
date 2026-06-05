# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression guard: suggest_pattern must NOT silently return 0 results under FORCE RLS.

FUFU-2 root-cause: _suggest_pattern (server.py) has no GUC wiring and relies on
the RLS policy to pass pattern rows through. Under the old NULL-as-global design
the 'profile_name IS NULL' branch rescued it. After the sentinel migration
(m13_021) the 'profile_name = __global__' branch replaces IS NULL — but
_suggest_pattern's WHERE clause must also be updated to filter on '__global__'
explicitly, otherwise the SELECT returns 0 rows regardless of RLS (the RLS branch
fires, but no rows match because the query itself never fetches '__global__' rows).

These tests call the REAL _suggest_pattern function from src.mcp.server (not a
reimplemented SELECT), so they fail whenever the actual WHERE clause regresses.
An earlier version of this test reimplemented the SELECT locally with a spurious
`AND odoo_version = %s` condition — it would have PASSED even if the
`AND profile_name = '__global__'` clause were removed, making it useless as a
regression guard.

Per FastMCP convention (CLAUDE.md), the underscore-prefixed _suggest_pattern is
imported directly (FastMCP wraps the public name into a non-callable FunctionTool;
the underscore form is the real callable used in all integration tests).

Marker: pytest.mark.postgres — requires a real PostgreSQL + pgvector DB.
PROD-SAFETY: NEVER run against the default localhost DSN on this box — it points
at the prod database.  Run in CI or against an isolated throwaway container only.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.postgres

# Version sentinel dedicated to this test module — avoids row collisions.
_TV = "97.0"

# A zero-vector literal for pgvector (1024-dim).
_ZERO_VEC = "[" + ",".join(["0.0"] * 1024) + "]"


@pytest.fixture
def pg_with_global_patterns(clean_pg_embeddings):
    """Insert '__global__' pattern_example rows for both language paths, yield the connection.

    Uses the raw pg connection (owner, RLS bypassed for writes) so that the
    INSERT succeeds even when FORCE RLS is active.
    Rows: 'python__override_write' (language=python) and 'xml__view_inherit' (language=xml).
    """
    pg = clean_pg_embeddings

    rows = [
        (
            "pattern_example", "__patterns__", _TV,
            "python__override_write", None,
            "/__patterns__/override_write.py", 0,
            "sentinel regression guard pattern body python",
            _ZERO_VEC, "__global__",
        ),
        (
            "pattern_example", "__patterns__", _TV,
            "xml__view_inherit", None,
            "/__patterns__/view_inherit.xml", 0,
            "sentinel regression guard pattern body xml",
            _ZERO_VEC, "__global__",
        ),
    ]

    with pg.cursor() as cur:
        for row in rows:
            cur.execute(
                """
                INSERT INTO embeddings
                    (chunk_type, module, odoo_version, entity_name, model_name,
                     file_path, chunk_idx, content, vec, profile_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)
                ON CONFLICT DO NOTHING
                """,
                row,
            )
    pg.commit()

    yield pg

    # Cleanup: remove the test rows for this version.
    with pg.cursor() as cur:
        cur.execute(
            "DELETE FROM embeddings "
            "WHERE odoo_version = %s AND module = '__patterns__'",
            (_TV,),
        )
    pg.commit()


def test_suggest_pattern_language_all_returns_global_row(
    pg_with_global_patterns,
):
    """_suggest_pattern (language='all') must return the '__global__' pattern row without GUC.

    This is the silent-0 regression guard for FUFU-2 (issue #267):
    - Setup: '__global__' pattern rows in embeddings.
    - Action: call the REAL _suggest_pattern with GUC UNSET (language='all' path).
    - Assert: ≥1 result returned.

    A result of 0 means the WHERE clause is missing `AND profile_name = '__global__'`
    (or the GLOBAL_PROFILE constant drifted from the actual value used in the query).
    """
    from src.indexer.embedder import FakeEmbedder
    from src.mcp.server import _suggest_pattern

    pg = pg_with_global_patterns

    # Intentionally do NOT set app.allowed_profiles GUC — mimics an unwrapped
    # connection (no _rls_read_tx wrapper, as in the production path).
    result = _suggest_pattern(
        "override write method",
        odoo_version=_TV,
        language="all",
        _driver=None,  # no Neo4j needed — test only the pgvector SELECT path
        _pg_conn=pg,
        _embedder=FakeEmbedder(dim=1024),
    )

    # Must NOT return the "no curated patterns" message — at least 1 row must match.
    assert "No curated patterns available" not in result, (
        "REGRESSION: _suggest_pattern (language='all') returned 0 rows for a "
        "'__global__' pattern row WITHOUT GUC set. "
        "The WHERE clause must include `AND profile_name = GLOBAL_PROFILE` to match "
        "the sentinel migration (m13_021). Without this fix, suggest_pattern silently "
        "returns 'no curated patterns' for all callers post-migration."
    )


def test_suggest_pattern_language_filter_returns_global_row(
    pg_with_global_patterns,
):
    """_suggest_pattern (language='python') must return the '__global__' pattern without GUC.

    Covers the language-filter branch (entity_name LIKE 'python__%').
    """
    from src.indexer.embedder import FakeEmbedder
    from src.mcp.server import _suggest_pattern

    pg = pg_with_global_patterns

    result = _suggest_pattern(
        "override write method",
        odoo_version=_TV,
        language="python",
        _driver=None,
        _pg_conn=pg,
        _embedder=FakeEmbedder(dim=1024),
    )

    assert "No curated patterns available" not in result, (
        "REGRESSION: _suggest_pattern (language='python') returned 0 rows for a "
        "'python__override_write' '__global__' pattern row WITHOUT GUC set. "
        "The WHERE clause (language-filter branch) must include "
        "`AND profile_name = GLOBAL_PROFILE`."
    )
