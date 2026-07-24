# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backfill src/data/patterns.json -> patterns DB table.

Idempotent: ON CONFLICT (pattern_id) DO UPDATE SET ... only when source-controlled
fields differ. Safe to run multiple times; second run produces 0 inserted, 0 updated
when the DB already matches the JSON.

The backfill IS the SSOT sync from the curated patterns.json into the Postgres
`patterns` table (which is in turn the reseed source-of-truth for Neo4j
PatternExample + pgvector, read by src.indexer.seed_patterns). Because the DB
table - not patterns.json - is what the reseed reads, a curated pattern_id that
is RENAMED or REMOVED from patterns.json (e.g. #362 renamed
`odoo-module-owl2-component-v15` -> `odoo-module-owl1-component-v15`) would
otherwise survive in the DB forever and keep re-propagating to Neo4j/pgvector on
every reseed - the same orphan-on-rename class fixed one layer up in Neo4j (R1,
writer_neo4j.write_pattern_examples prune) and in the version-scoped spec
writers (F2). The upsert-only backfill was the remaining gap at the Postgres
layer, and the R1 Neo4j prune cannot heal it because the DB still lists the
stale id as live (WHERE soft_deleted = FALSE).

This module therefore SOFT-DELETES (soft_deleted = TRUE) curated rows whose
pattern_id left patterns.json, so the next reseed excludes them (R1 then drops
the PatternExample; pgvector clean-replaces) while the row stays recoverable and
auditable. The prune is SCOPED to `updated_by IS NULL` so it only ever touches
backfill/curated-owned rows - admin-created or admin-edited rows
(src/web_ui/routes/admin_patterns.py sets updated_by to the admin user id) are
NEVER pruned. See :func:`_prune_curated_removed`.

Usage:
    ~/.venv/odoo-semantic-mcp/bin/python ops/backfill_patterns.py
    ~/.venv/odoo-semantic-mcp/bin/python ops/backfill_patterns.py --no-prune

Run after `python -m src.db.migrate` to populate a fresh DB.

Column mapping (JSON field -> DB column):
    pattern_id          -> pattern_id       (PRIMARY KEY)
    intent_keywords     -> intent_keywords  (TEXT[])
    file_ref            -> file_ref         (TEXT)
    snippet_text        -> snippet_text     (TEXT)
    gotchas             -> gotchas          (JSONB, list of strings)
    odoo_version_min    -> odoo_version_min (TEXT)
    odoo_version_max    -> odoo_version_max (TEXT, nullable)
    category            -> category         (TEXT, nullable, enum test/production)
    language            -> language         (TEXT, enum python/xml/js)
    core_symbol_names   -> core_symbol_names (TEXT[], default [])
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Allow running directly from repo root (python ops/backfill_patterns.py)
# or from inside ops/ (python backfill_patterns.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src import config  # noqa: E402 (path setup must come first)

log = logging.getLogger("backfill_patterns")

PATTERNS_JSON = _REPO_ROOT / "src" / "data" / "patterns.json"


# ---------------------------------------------------------------------------
# Core backfill logic (testable without __main__)
# ---------------------------------------------------------------------------


def _prune_curated_removed(cur, json_ids: list[str]) -> tuple[int, list[str]]:
    """Soft-delete curated rows whose pattern_id left patterns.json.

    Runs after the upsert loop as part of the SSOT sync. Soft-delete (not
    hard-delete) because the `patterns` table has a `soft_deleted` column and the
    reseed reads `WHERE soft_deleted = FALSE` (src.indexer.seed_patterns.
    _load_patterns_from_db), so flipping the flag excludes the row from the next
    reseed - Neo4j R1 prune then removes its PatternExample and pgvector
    clean-replaces - while the row stays recoverable and auditable.

    SCOPE - `updated_by IS NULL` (mandatory admin-safety): backfill never writes
    `updated_by` (its INSERT omits the column and its ON CONFLICT DO UPDATE does
    not set it), so backfill/curated-owned rows have `updated_by IS NULL`. The
    admin CRUD (src/web_ui/routes/admin_patterns.py create/update/soft_delete)
    always sets `updated_by` to the admin user id (NOT NULL in production). A
    naive unscoped `pattern_id NOT IN (json ids)` prune would soft-delete
    admin-CREATED patterns (net-new ids that never appear in patterns.json) =
    data loss, so the predicate is scoped to `updated_by IS NULL` and never
    touches an admin-created or admin-edited row.

    EMPTY-GUARD (mandatory): when *json_ids* is empty (a failed / empty
    patterns.json load) prune NOTHING and return 0. `pattern_id <> ALL(ARRAY[])`
    is vacuously TRUE for every row, so without this guard an empty id set would
    soft-delete the WHOLE curated catalogue. Mirrors the R1
    (write_pattern_examples) and F2 (_prune_versioned_spec_nodes) empty-guards.

    Returns ``(pruned_count, sorted_pruned_ids)`` - the safety-log payload so an
    operator sees exactly which curated patterns were retired.
    """
    if not json_ids:
        return 0, []
    cur.execute(
        """
        UPDATE patterns
           SET soft_deleted = TRUE,
               updated_at   = now()
         WHERE updated_by IS NULL
           AND soft_deleted = FALSE
           AND pattern_id <> ALL(%(json_ids)s)
        RETURNING pattern_id
        """,
        {"json_ids": json_ids},
    )
    pruned_ids = sorted(r[0] for r in cur.fetchall())
    return len(pruned_ids), pruned_ids


def backfill(
    conn, *, patterns_path: Path = PATTERNS_JSON, prune: bool = True,
) -> tuple[int, int, int]:
    """Backfill patterns from *patterns_path* into patterns table via *conn*.

    Args:
        conn: open psycopg2 connection (caller owns lifecycle + commit/rollback).
        patterns_path: path to patterns.json (default: src/data/patterns.json).
        prune: when True (default - the backfill IS the SSOT sync), soft-delete
            curated (``updated_by IS NULL``) rows whose pattern_id is no longer in
            patterns.json. The ``--no-prune`` CLI flag sets this False as an escape
            hatch for a partial / hand-edited patterns.json. See
            :func:`_prune_curated_removed` for the admin-safety + empty-guard
            contract.

    Returns:
        (inserted_count, updated_count, pruned_count) tuple.
        Rows with no diff are counted neither as inserted nor updated.
        ``pruned_count`` is the number of curated rows soft-deleted (0 when
        ``prune=False`` or the id set is empty).

    Raises:
        AssertionError: if patterns_path content is not a JSON array.
        FileNotFoundError: if patterns_path does not exist.
    """
    raw = json.loads(patterns_path.read_text(encoding="utf-8"))
    assert isinstance(raw, list), (
        f"patterns.json must be a JSON array, got {type(raw).__name__}"
    )

    inserted = 0
    updated = 0
    pruned = 0
    json_ids: list[str] = []

    with conn.cursor() as cur:
        for p in raw:
            pid = p["pattern_id"]
            json_ids.append(pid)
            intent_keywords = p.get("intent_keywords", [])
            file_ref = p["file_ref"]
            snippet_text = p["snippet_text"]
            gotchas = json.dumps(p.get("gotchas", []))
            odoo_version_min = p["odoo_version_min"]
            odoo_version_max = p.get("odoo_version_max")  # nullable
            category = p.get("category")  # nullable
            language = p["language"]
            core_symbol_names = p.get("core_symbol_names", [])

            # ON CONFLICT: update only when at least one field differs.
            # The WHERE clause on the DO UPDATE avoids bumping updated_at and
            # counting rows that already match the JSON source exactly.
            # xmax = 0 means the row was freshly INSERTed (not UPDATEd).
            cur.execute(
                """
                INSERT INTO patterns (
                    pattern_id, intent_keywords, file_ref, snippet_text,
                    gotchas, odoo_version_min, odoo_version_max, category,
                    language, core_symbol_names
                ) VALUES (
                    %s, %s, %s, %s,
                    %s::jsonb, %s, %s, %s,
                    %s, %s
                )
                ON CONFLICT (pattern_id) DO UPDATE SET
                    intent_keywords  = EXCLUDED.intent_keywords,
                    file_ref         = EXCLUDED.file_ref,
                    snippet_text     = EXCLUDED.snippet_text,
                    gotchas          = EXCLUDED.gotchas,
                    odoo_version_min = EXCLUDED.odoo_version_min,
                    odoo_version_max = EXCLUDED.odoo_version_max,
                    category         = EXCLUDED.category,
                    language         = EXCLUDED.language,
                    core_symbol_names = EXCLUDED.core_symbol_names,
                    updated_at       = now()
                WHERE
                    patterns.intent_keywords   IS DISTINCT FROM EXCLUDED.intent_keywords
                    OR patterns.file_ref       IS DISTINCT FROM EXCLUDED.file_ref
                    OR patterns.snippet_text   IS DISTINCT FROM EXCLUDED.snippet_text
                    OR patterns.gotchas        IS DISTINCT FROM EXCLUDED.gotchas
                    OR patterns.odoo_version_min IS DISTINCT FROM EXCLUDED.odoo_version_min
                    OR patterns.odoo_version_max IS DISTINCT FROM EXCLUDED.odoo_version_max
                    OR patterns.category       IS DISTINCT FROM EXCLUDED.category
                    OR patterns.language       IS DISTINCT FROM EXCLUDED.language
                    OR patterns.core_symbol_names IS DISTINCT FROM EXCLUDED.core_symbol_names
                RETURNING xmax
                """,
                (
                    pid,
                    intent_keywords,
                    file_ref,
                    snippet_text,
                    gotchas,
                    odoo_version_min,
                    odoo_version_max,
                    category,
                    language,
                    core_symbol_names,
                ),
            )
            row = cur.fetchone()
            if row is None:
                # ON CONFLICT WHERE condition evaluated to FALSE — no diff, skip
                pass
            elif int(row[0]) == 0:
                # xmax = 0 means the row was freshly INSERTed (no prior version).
                # psycopg2 returns xmax as a string from RETURNING xmax.
                inserted += 1
            else:
                # xmax != 0 means the row was UPDATEd (existing transaction ID).
                updated += 1

        # SSOT sync: retire curated rows that left patterns.json (scoped to
        # updated_by IS NULL so admin rows are never touched; empty-guard inside).
        if prune:
            pruned, pruned_ids = _prune_curated_removed(cur, json_ids)
            if pruned:
                # SAFETY LOG: name every soft-deleted curated pattern so an
                # operator sees exactly what the SSOT sync retired this run.
                log.info(
                    "Backfill prune: soft-deleted %d curated pattern(s) removed "
                    "from patterns.json (updated_by IS NULL scope): %s",
                    pruned,
                    ", ".join(pruned_ids),
                )

    return inserted, updated, pruned


def _build_conn():
    """Build a psycopg2 connection from config (DSN env var or odoo-semantic.conf)."""
    import psycopg2

    dsn = config.from_env_or_ini("PG_DSN", "database", "pg_dsn", fallback=None)
    if not dsn:
        raise RuntimeError(
            "PG_DSN not set. Export PG_DSN=postgresql://... or configure "
            "[database] pg_dsn in odoo-semantic.conf."
        )
    return psycopg2.connect(dsn)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    # ADR-0031: load `.env` at the CLI entry point so PG_DSN (with its password)
    # resolves on a fresh prod box without the operator manually sourcing .env.
    # Idempotent + main()-only (never at import) so pytest is unaffected; mirrors
    # src/db/migrate.py::main().  This was the PRIMARY cause of the ADR-0042
    # backfill prod auth failure (DSN was unresolved -> connect as wrong user).
    config.init_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--patterns-file",
        default=str(PATTERNS_JSON),
        help=f"Path to patterns.json (default: {PATTERNS_JSON})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="(reserved for future use — backfill is always idempotent)",
    )
    parser.add_argument(
        "--no-prune",
        action="store_true",
        help=(
            "Do NOT soft-delete curated rows removed from patterns.json. Prune is "
            "ON by default because the backfill IS the SSOT sync; use this escape "
            "hatch only for a partial / hand-edited patterns.json. Only "
            "curated rows (updated_by IS NULL) are ever pruned - admin-owned "
            "rows are always preserved."
        ),
    )
    args = parser.parse_args(argv)

    patterns_path = Path(args.patterns_file)
    if not patterns_path.exists():
        log.error("patterns.json not found: %s", patterns_path)
        return 2

    conn = _build_conn()
    try:
        ins, upd, pruned = backfill(
            conn, patterns_path=patterns_path, prune=not args.no_prune,
        )
        conn.commit()
        log.info(
            "Backfill complete: %d inserted, %d updated, %d pruned "
            "(no-diff skipped).",
            ins, upd, pruned,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
