"""One-shot: re-label the curated resolve_view golden entries from live handler output.

Runs the indexer over the test fixture corpus into a throwaway tenant
schema, invokes ``resolve_view`` for every non-TODO entry in
``tests/fixtures/golden/resolve_view.json``, and writes the normalised response
back as the new golden. TODO skeletons and entries marked ``skip_handler`` are
preserved untouched. Mirrors ``regenerate_golden.py``.

Run from the repo root with a live Postgres:

    DATABASE_URL=postgresql:///osm_dev?user=osm \\
        uv run python tests/accept/regenerate_golden_views.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

import psycopg

from osm.indexer.driver import index
from osm.server.errors import NotFoundError
from osm.server.handlers.resolve_view import resolve_view
from osm.server.tenancy import context_from_tenant
from scripts.create_tenant import main as create_tenant_main
from scripts.migrate import main as migrate_main

REPO = Path(__file__).resolve().parent.parent
GOLDEN = REPO / "tests" / "fixtures" / "golden"
CE_SUBSET = REPO / "tests" / "fixtures" / "odoo_ce_subset"
CUSTOM = REPO / "tests" / "fixtures" / "custom_addons"


def _entry_label(entry: dict[str, Any]) -> str:
    label = entry.get("label")
    xmlid = entry.get("xmlid", "<no-xmlid>")
    return f"{xmlid}#{label}" if label else xmlid


def main() -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("error: DATABASE_URL not set", file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="regen_golden_views_"))
    shutil.copytree(CE_SUBSET, tmp / "odoo_ce_subset")
    shutil.copytree(CUSTOM, tmp / "custom_addons")

    migrate_main(["--schema", "public", "--database-url", db_url])
    tenant = f"osm_regen_{uuid.uuid4().hex[:8]}"
    create_tenant_main([tenant, "--database-url", db_url])

    try:
        with psycopg.connect(db_url) as conn:
            index(
                addon_roots=[tmp / "odoo_ce_subset", tmp / "custom_addons"],
                conn=conn,
                tenant=tenant,
                git_sha="golden-regen",
            )
            conn.commit()

        ctx = context_from_tenant(tenant)

        with psycopg.connect(db_url) as conn, conn.cursor() as cur:
            skipped: list[str] = []

            path = GOLDEN / "resolve_view.json"
            data = json.loads(path.read_text())
            updated: list[dict[str, Any]] = []
            for entry in data:
                if "TODO" in entry or "skip_handler" in entry:
                    updated.append(entry)
                    continue
                label = entry.get("label")
                try:
                    env = resolve_view(
                        cur,
                        ctx,
                        entry["xmlid"],
                        include_final_xml=entry.get("include_final_xml", True),
                        include_patch_log=entry.get("include_patch_log", True),
                    )
                except NotFoundError as exc:
                    skipped.append(f"resolve_view {_entry_label(entry)}: {exc}")
                    updated.append(entry)
                    continue
                out: dict[str, Any] = {
                    "xmlid": entry["xmlid"],
                    "include_final_xml": entry.get("include_final_xml", True),
                    "include_patch_log": entry.get("include_patch_log", True),
                    "result": env["result"],
                    "warnings": env["warnings"],
                }
                if label is not None:
                    out["label"] = label
                updated.append(out)
            path.write_text(json.dumps(updated, indent=2, sort_keys=True) + "\n")

            if skipped:
                print("skipped (entry preserved as-is):")
                for s in skipped:
                    print(" -", s)

    finally:
        with psycopg.connect(db_url) as conn, conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{tenant}" CASCADE')
            conn.commit()
        shutil.rmtree(tmp, ignore_errors=True)

    print("regenerated resolve_view golden at", GOLDEN / "resolve_view.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
