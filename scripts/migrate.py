"""Apply SQL migrations in order to a target schema.

Reads ``migrations/*.sql`` alphabetically, tracks applied versions in
``<schema>.schema_migrations``, and skips anything already recorded. Designed
for per-schema fan-out: ``public`` holds the shared Odoo CE index; each
tenant lives in its own schema and shares the same migration sequence.

Usage:

    python scripts/migrate.py --schema public
    python scripts/migrate.py --schema viindoo --database-url postgresql://...

Env vars:

    DATABASE_URL   Postgres connection string. Overridden by --database-url.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

import psycopg

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "migrations"


def _list_migrations() -> list[pathlib.Path]:
    if not MIGRATIONS_DIR.is_dir():
        raise SystemExit(f"migrations directory not found: {MIGRATIONS_DIR}")
    return sorted(p for p in MIGRATIONS_DIR.glob("*.sql") if p.is_file())


def _ensure_tracking_table(conn: psycopg.Connection, schema: str) -> None:
    with conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        cur.execute(
            f'CREATE TABLE IF NOT EXISTS "{schema}".schema_migrations ('
            "version text PRIMARY KEY,"
            "applied_at timestamptz NOT NULL DEFAULT now()"
            ")"
        )
    conn.commit()


def _applied_versions(conn: psycopg.Connection, schema: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(f'SELECT version FROM "{schema}".schema_migrations')
        return {row[0] for row in cur.fetchall()}


def _apply_one(conn: psycopg.Connection, schema: str, path: pathlib.Path) -> None:
    version = path.stem
    sql = path.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        # Route unqualified references to the target schema for the duration
        # of this migration. Migration files should still qualify DDL where
        # ambiguity matters.
        cur.execute(f'SET LOCAL search_path TO "{schema}", public')
        cur.execute(sql)
        cur.execute(
            f'INSERT INTO "{schema}".schema_migrations (version) VALUES (%s)',
            (version,),
        )
    conn.commit()
    print(f"[migrate] applied {version} to schema {schema}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema",
        default="public",
        help="Target schema (default: public). Created if missing.",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="Postgres connection string (defaults to $DATABASE_URL).",
    )
    args = parser.parse_args(argv)

    if not args.database_url:
        print("error: DATABASE_URL not set and --database-url not given", file=sys.stderr)
        return 2

    migrations = _list_migrations()
    if not migrations:
        print("[migrate] no migration files found; nothing to do")
        return 0

    with psycopg.connect(args.database_url) as conn:
        _ensure_tracking_table(conn, args.schema)
        applied = _applied_versions(conn, args.schema)
        pending = [m for m in migrations if m.stem not in applied]
        if not pending:
            print(f"[migrate] schema {args.schema} is up to date ({len(applied)} applied)")
            return 0
        for path in pending:
            _apply_one(conn, args.schema, path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
