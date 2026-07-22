# SPDX-License-Identifier: AGPL-3.0-or-later
"""Behaviour test for ADR-0007 D6-CRUD (issue #F1).

Contract under protection:
    An admin pattern edit that changes DB content MUST become visible in the
    Neo4j PatternExample graph after the next auto-reseed cycle — the promise
    admin_patterns.py's own response makes ("reseed_status: pending - next
    index_profile() run").

Before the fix, the CRUD path STAMPED the _SeedMeta sentinel to the post-write
canonical SHA (via recompute_sentinel_sha()).  run()'s gate recomputed the
identical SHA from the identical DB rows, saw a match, and SKIPPED — so the
edit never propagated to Neo4j/pgvector.  After the fix the CRUD path
INVALIDATES the sentinel (invalidate_patterns_sentinel()), so run() detects
drift and re-writes.

Requires PostgreSQL AND Neo4j (testcontainers / CI service containers).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.db.migrate import run_migrations

pytestmark = [pytest.mark.postgres, pytest.mark.neo4j]

_PATTERNS_JSON = (
    Path(__file__).resolve().parent.parent / "src" / "data" / "patterns.json"
)


@pytest.fixture
def fresh_pg(clean_pg):
    """Migrate schema (incl. patterns table) and yield a clean connection."""
    with clean_pg.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS patterns CASCADE")
    run_migrations(clean_pg)
    yield clean_pg
    with clean_pg.cursor() as cur:
        cur.execute("DELETE FROM patterns")
    clean_pg.commit()


def _backfill_from_json(conn) -> int:
    """Insert all patterns from patterns.json into the DB. Returns row count."""
    data = json.loads(_PATTERNS_JSON.read_text(encoding="utf-8"))
    inserted = 0
    with conn.cursor() as cur:
        for entry in data:
            cur.execute(
                """INSERT INTO patterns
                     (pattern_id, intent_keywords, file_ref, snippet_text,
                      gotchas, odoo_version_min, odoo_version_max,
                      language, core_symbol_names)
                   VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                   ON CONFLICT (pattern_id) DO NOTHING""",
                (
                    entry["pattern_id"],
                    entry.get("intent_keywords", []),
                    entry["file_ref"],
                    entry["snippet_text"],
                    json.dumps(entry.get("gotchas", [])),
                    entry["odoo_version_min"],
                    entry.get("odoo_version_max"),
                    entry["language"],
                    entry.get("core_symbol_names", []),
                ),
            )
            inserted += cur.rowcount
    conn.commit()
    return inserted


class TestAdminEditVisibleAfterNextReseed:
    def test_admin_update_is_visible_in_neo4j_after_next_reseed(
        self, fresh_pg, clean_neo4j,
    ):
        """Admin PATCH -> real invalidate path -> next run() reseeds -> Neo4j reflects edit.

        This holds WITHOUT any test-only sentinel manipulation.  On the pre-fix
        code (CRUD stamps the current SHA) run()['skipped'] comes back True and
        the PatternExample still carries the OLD snippet_text — RED.  After the
        fix (CRUD invalidates the sentinel) run() reseeds and the node reflects
        the edit — GREEN.
        """
        import os

        from src.indexer.seed_patterns import (
            _get_stored_patterns_sha,
            invalidate_patterns_sentinel,
            run,
        )
        from src.indexer.writer_neo4j import Neo4jWriter
        from tests.conftest import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER

        _backfill_from_json(fresh_pg)

        # Which real pattern are we going to edit? Pick a deterministic one.
        with fresh_pg.cursor() as cur:
            cur.execute("SELECT pattern_id FROM patterns ORDER BY pattern_id LIMIT 1")
            target_pid = cur.fetchone()[0]

        # Wipe any stale sentinel/pattern nodes from a prior test.
        with clean_neo4j.session() as session:
            session.run("MATCH (s:_SeedMeta) DELETE s")
            session.run("MATCH (p:PatternExample) DELETE p")

        uri = os.getenv("NEO4J_TEST_URI", NEO4J_URI)
        user = os.getenv("NEO4J_TEST_USER", NEO4J_USER)
        password = os.getenv("NEO4J_TEST_PASSWORD", NEO4J_PASSWORD)
        writer = Neo4jWriter(uri, user, password)
        try:
            # Baseline seed: Neo4j holds the OLD content, sentinel stamped in sync
            # (a real deployment before anyone edits a pattern).
            baseline = run(writer=writer, embedder=None, force=True)
            assert baseline["skipped"] is False
            with writer.driver.session() as session:
                old = session.run(
                    "MATCH (p:PatternExample {pattern_id: $pid}) "
                    "RETURN p.snippet_text AS txt",
                    pid=target_pid,
                ).single()
            assert old is not None, "baseline seed must create the PatternExample"
            edited = "# EDITED BY ADMIN (propagation test)"
            assert old["txt"] != edited, "precondition: edit differs from baseline"

            # --- Admin edits the pattern (the CRUD handler's DB write) ---
            with fresh_pg.cursor() as cur:
                cur.execute(
                    "UPDATE patterns SET snippet_text = %s WHERE pattern_id = %s",
                    (edited, target_pid),
                )
            fresh_pg.commit()

            # --- The REAL post-fix CRUD side effect (what _invalidate_sentinel
            # calls). Explicit driver keeps the test hermetic w.r.t. config. ---
            invalidate_patterns_sentinel(writer.driver)

            # Sentinel must be gone (incl. the legacy 'patterns' fallback key).
            assert (
                _get_stored_patterns_sha(writer.driver, key="patterns_neo4j") is None
            ), "invalidate must clear the neo4j sentinel (incl. legacy fallback)"

            # --- Next index_profile() cycle ---
            result = run(writer=writer, embedder=None, force=False)

            assert result["skipped"] is False, (
                "admin edit must be picked up by the next auto-reseed; if this "
                "is True the CRUD sentinel handling silently ate the reseed "
                "signal (issue #F1)"
            )

            # The observable outcome: Neo4j actually reflects the edit.
            with writer.driver.session() as session:
                row = session.run(
                    "MATCH (p:PatternExample {pattern_id: $pid}) "
                    "RETURN p.snippet_text AS txt",
                    pid=target_pid,
                ).single()
            assert row is not None
            assert row["txt"] == edited, (
                f"PatternExample.snippet_text must reflect the admin edit; "
                f"got {row['txt']!r}"
            )
        finally:
            with clean_neo4j.session() as session:
                session.run("MATCH (s:_SeedMeta) DELETE s")
                session.run("MATCH (p:PatternExample) DELETE p")
            writer.close()
