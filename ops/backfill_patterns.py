# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backfill src/data/patterns.json -> patterns DB table.

Idempotent: ON CONFLICT (pattern_id) DO UPDATE SET ... only when source-controlled
fields differ. Safe to run multiple times; second run produces 0 inserted, 0 updated
when the DB already matches the JSON.

Usage:
    ~/.venv/odoo-semantic-mcp/bin/python ops/backfill_patterns.py
    ~/.venv/odoo-semantic-mcp/bin/python ops/backfill_patterns.py --force

Run after `python -m src.db.migrate` to populate a fresh DB.

Column mapping (JSON field -> DB column):
    pattern_id          -> pattern_id       (PRIMARY KEY)
    intent_keywords     -> intent_keywords  (TEXT[])
    file_ref            -> file_ref         (TEXT)
    snippet_text        -> snippet_text     (TEXT)
    gotchas             -> gotchas          (JSONB, list of strings)
    odoo_version_min    -> odoo_version_min (TEXT)
    odoo_version_max    -> odoo_version_max (TEXT, nullable)
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


def backfill(conn, *, patterns_path: Path = PATTERNS_JSON) -> tuple[int, int]:
    """Backfill patterns from *patterns_path* into patterns table via *conn*.

    Args:
        conn: open psycopg2 connection (caller owns lifecycle + commit/rollback).
        patterns_path: path to patterns.json (default: src/data/patterns.json).

    Returns:
        (inserted_count, updated_count) tuple.
        Rows with no diff are counted neither as inserted nor updated.

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

    with conn.cursor() as cur:
        for p in raw:
            pid = p["pattern_id"]
            intent_keywords = p.get("intent_keywords", [])
            file_ref = p["file_ref"]
            snippet_text = p["snippet_text"]
            gotchas = json.dumps(p.get("gotchas", []))
            odoo_version_min = p["odoo_version_min"]
            odoo_version_max = p.get("odoo_version_max")  # nullable
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
                    gotchas, odoo_version_min, odoo_version_max,
                    language, core_symbol_names
                ) VALUES (
                    %s, %s, %s, %s,
                    %s::jsonb, %s, %s,
                    %s, %s
                )
                ON CONFLICT (pattern_id) DO UPDATE SET
                    intent_keywords  = EXCLUDED.intent_keywords,
                    file_ref         = EXCLUDED.file_ref,
                    snippet_text     = EXCLUDED.snippet_text,
                    gotchas          = EXCLUDED.gotchas,
                    odoo_version_min = EXCLUDED.odoo_version_min,
                    odoo_version_max = EXCLUDED.odoo_version_max,
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

    return inserted, updated


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
    args = parser.parse_args(argv)

    patterns_path = Path(args.patterns_file)
    if not patterns_path.exists():
        log.error("patterns.json not found: %s", patterns_path)
        return 2

    conn = _build_conn()
    try:
        ins, upd = backfill(conn, patterns_path=patterns_path)
        conn.commit()
        log.info("Backfill complete: %d inserted, %d updated (no-diff skipped).", ins, upd)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
