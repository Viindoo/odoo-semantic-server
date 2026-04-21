"""Provision a new tenant schema and apply the migration sequence to it.

Creates the Postgres schema (quoted identifier) and delegates to
``scripts.migrate.main`` so the tenant gets the same DDL as ``public``.
Idempotent: re-running with an existing schema is a no-op once migrations
are current.

Usage:

    python scripts/create_tenant.py viindoo
    python scripts/create_tenant.py cust_acme --database-url postgresql://...

Env vars:

    DATABASE_URL   Postgres connection string. Overridden by --database-url.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

import psycopg

from scripts.migrate import main as migrate_main

# WHY: validate tenant name as a safe identifier before quoting. Even with
# double-quoting, accepting arbitrary bytes here risks confusable/zero-width
# characters in schema names. Pattern matches Postgres-safe lowercase idents
# and stays well under the 63-byte NAMEDATALEN limit.
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,62}$")

_RESERVED = frozenset({"public", "information_schema"})


def _validate_name(name: str) -> None:
    if not _NAME_PATTERN.match(name):
        raise SystemExit(
            f"error: invalid tenant name {name!r}; "
            "must match ^[a-z][a-z0-9_]{1,62}$"
        )
    if name in _RESERVED or name.startswith("pg_"):
        raise SystemExit(f"error: reserved schema name {name!r} is not allowed")


def _create_schema(database_url: str, name: str) -> None:
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{name}"')
        conn.commit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="Tenant schema name (e.g. viindoo, cust_acme).")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="Postgres connection string (defaults to $DATABASE_URL).",
    )
    args = parser.parse_args(argv)

    _validate_name(args.name)

    if not args.database_url:
        print("error: DATABASE_URL not set and --database-url not given", file=sys.stderr)
        return 2

    _create_schema(args.database_url, args.name)
    print(f"[create_tenant] schema {args.name} ready")

    migrate_argv = ["--schema", args.name, "--database-url", args.database_url]
    return migrate_main(migrate_argv)


if __name__ == "__main__":
    raise SystemExit(main())
