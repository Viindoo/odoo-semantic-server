"""Run the indexer over one or more Odoo addon paths into a target tenant schema.

Usage:

    python scripts/index.py --addons ./odoo/addons --addons ./tvtmaaddons \
        --tenant viindoo --git-sha $(git rev-parse HEAD)

    python scripts/index.py --addons ./tests/fixtures/odoo_ce_subset \
        --addons ./tests/fixtures/custom_addons --tenant public --git-sha fixture0

The tenant schema must already exist. Provision one first via:

    python scripts/create_tenant.py <name>

Env vars:

    DATABASE_URL   Postgres connection string. Overridden by --database-url.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import psycopg

from osm.indexer.driver import IndexStats, index

_TENANT_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,62}$|^public$")


def _validate_tenant(name: str) -> None:
    if not _TENANT_PATTERN.match(name):
        raise SystemExit(
            f"error: invalid tenant schema {name!r}; "
            "must match ^[a-z][a-z0-9_]{1,62}$ or be 'public'"
        )


def _format_stats(stats: IndexStats) -> str:
    lines = [
        f"modules_scanned   = {stats.modules_scanned}",
        f"modules_upserted  = {stats.modules_upserted}",
        f"files_reparsed    = {stats.files_reparsed}",
        f"files_skipped     = {stats.files_skipped}",
        f"models   ins={stats.models_inserted}  upd={stats.models_updated}",
        f"fields   ins={stats.fields_inserted}  upd={stats.fields_updated}",
        f"methods  ins={stats.methods_inserted}  upd={stats.methods_updated}",
        f"rows_deleted      = {stats.rows_deleted}",
        f"override_links    = {stats.override_links_written}",
        f"cache_rows_touched= {stats.cache_rows_touched}",
        f"warnings          = {len(stats.warnings)}",
    ]
    for w in stats.warnings:
        lines.append(f"  ! {w}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--addons",
        action="append",
        required=True,
        help="Addon root directory; repeat flag for multiple roots.",
    )
    parser.add_argument(
        "--tenant",
        default="public",
        help="Target tenant schema (default: public).",
    )
    parser.add_argument(
        "--git-sha",
        default=os.environ.get("OSM_GIT_SHA", "unknown"),
        help="Git SHA stamped onto every re-indexed row.",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="Postgres connection string (defaults to $DATABASE_URL).",
    )
    args = parser.parse_args(argv)

    _validate_tenant(args.tenant)
    if not args.database_url:
        print("error: DATABASE_URL not set and --database-url not given", file=sys.stderr)
        return 2

    roots = [Path(p) for p in args.addons]
    for root in roots:
        if not root.is_dir():
            print(f"error: addon root not found: {root}", file=sys.stderr)
            return 2

    with psycopg.connect(args.database_url) as conn:
        stats = index(
            addon_roots=roots,
            conn=conn,
            tenant=args.tenant,
            git_sha=args.git_sha,
        )
        conn.commit()

    print(_format_stats(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
