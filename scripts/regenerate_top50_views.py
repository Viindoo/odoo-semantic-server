"""Regenerate ``tests/accept/top50_views.json`` from a live MCP index.

Queries the ``views`` table in the active tenant (or ``public``) schema to
find the 50 primary views with the most extensions, orders by extension
count descending, and rewrites ``tests/accept/top50_views.json`` with the
result + a timestamp.

Idempotent: re-running on an unchanged DB produces bit-identical output.
Preserves seed-only entries (``seed: true``) in two cases:

- Query returns no rows at all (empty schema): exits with code 1, file
  unchanged.
- Query returns fewer than 50 rows: seed entries missing from the DB
  result are merged in (DB rows take precedence for any xmlid that
  appears in both sets).

Run from repo root against a Postgres with a full CE index populated:

    DATABASE_URL=postgresql:///osm_live?user=soncrits \\
        OSM_TENANT=public \\
        uv run python scripts/regenerate_top50_views.py

Must run on ``osm-dev`` (where the full CE corpus is indexed). Not expected
to succeed on a laptop with only the fixture subset.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

REPO = Path(__file__).resolve().parent.parent
TARGET = REPO / "tests" / "accept" / "top50_views.json"

_QUERY = sql.SQL(
    """
    SELECT v.xmlid AS root_xmlid,
           COUNT(ext.id) AS ext_count
      FROM {schema}.views v
      LEFT JOIN {schema}.views ext ON ext.inherit_id = v.id
     WHERE v.mode = 'primary'
     GROUP BY v.xmlid
     ORDER BY ext_count DESC, v.xmlid ASC
     LIMIT 50
    """
)


def _load_seeds() -> list[dict[str, Any]]:
    if not TARGET.is_file():
        return []
    try:
        blob = json.loads(TARGET.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    views = blob.get("views") or []
    return [v for v in views if isinstance(v, dict) and v.get("seed")]


def main() -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("error: DATABASE_URL not set", file=sys.stderr)
        return 2
    schema = os.environ.get("OSM_TENANT", "public")

    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(_QUERY.format(schema=sql.Identifier(schema)))
        rows = cur.fetchall()

    if not rows:
        # Preserve seed entries untouched — live index is empty or the
        # schema has no primary view rows. Exiting with code 1 signals
        # the regen was a no-op so the caller can decide what to do.
        print(
            f"warning: no primary view rows found in schema {schema!r}; "
            "preserving existing seed entries",
            file=sys.stderr,
        )
        return 1

    entries: list[dict[str, Any]] = [
        {"xmlid": xmlid, "ext_count": int(count), "seed": False}
        for xmlid, count in rows
    ]

    if len(entries) < 50:
        # Fewer than 50 DB rows — merge must-include seeds so the top-50
        # fixture stays populated even against a fresh / partial CE index.
        # DB rows win on any xmlid collision (they carry real usage stats).
        db_xmlids = {e["xmlid"] for e in entries}
        for seed in _load_seeds():
            if seed.get("xmlid") not in db_xmlids:
                entries.append(seed)

    blob = {
        "_regenerated_at": _dt.datetime.now(tz=_dt.UTC).isoformat(
            timespec="seconds"
        ),
        "_regenerated_by": (
            "scripts/regenerate_top50_views.py "
            f"(schema={schema}, rows={len(entries)})"
        ),
        "_notes": (
            "Query: top-50 primary views ranked by extension count. "
            "Re-run after an Odoo pin bump or addons-path change."
        ),
        "views": entries,
    }
    TARGET.write_text(
        json.dumps(blob, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {TARGET} ({len(entries)} entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
