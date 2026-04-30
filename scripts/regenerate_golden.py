"""One-shot: re-label the curated golden entries from live handler output.

Runs the indexer over the test fixture corpus into a throwaway tenant
schema, invokes each handler for every non-TODO entry in
`tests/fixtures/golden/*.json`, and writes the normalised response back as
the new golden. TODO skeletons are preserved untouched.

Run from the repo root with a live Postgres:

    DATABASE_URL=postgresql:///osm_dev?user=osm \\
        uv run python scripts/regenerate_golden.py
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
from osm.server.handlers.resolve_field import resolve_field
from osm.server.handlers.resolve_method import resolve_method
from osm.server.handlers.resolve_model import resolve_model
from osm.server.tenancy import context_from_tenant
from scripts.create_tenant import main as create_tenant_main
from scripts.migrate import main as migrate_main

REPO = Path(__file__).resolve().parent.parent
GOLDEN = REPO / "tests" / "fixtures" / "golden"
CE_SUBSET = REPO / "tests" / "fixtures" / "odoo_ce_subset"
CUSTOM = REPO / "tests" / "fixtures" / "custom_addons"


def _normalise_file(path: str) -> str:
    for m in ("odoo_ce_subset/", "custom_addons/"):
        idx = path.find(m)
        if idx != -1:
            return "tests/fixtures/" + path[idx:]
    return path


def _normalise_entry(entry: dict[str, Any]) -> dict[str, Any]:
    out = dict(entry)
    if "file" in out and isinstance(out["file"], str):
        out["file"] = _normalise_file(out["file"])
    out.pop("kind", None)  # stay in step with handler-test normalisation
    return out


def main() -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("error: DATABASE_URL not set", file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="regen_golden_"))
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

            # models
            path = GOLDEN / "resolve_model.json"
            data = json.loads(path.read_text())
            updated: list[dict[str, Any]] = []
            for entry in data:
                if "TODO" in entry:
                    updated.append(entry)
                    continue
                try:
                    env = resolve_model(cur, ctx, entry["model_name"])
                except NotFoundError as exc:
                    skipped.append(f"resolve_model {entry['model_name']}: {exc}")
                    updated.append(entry)
                    continue
                r = env["result"]
                chain = [_normalise_entry(c) for c in r["chain"]]
                updated.append({
                    "model_name": r["model_name"],
                    "abstract": r["abstract"],
                    "transient": r["transient"],
                    "inherits": r["inherits"],
                    "chain": chain,
                    "warnings": r["warnings"],
                })
            path.write_text(json.dumps(updated, indent=2, sort_keys=True) + "\n")

            # fields
            path = GOLDEN / "resolve_field.json"
            data = json.loads(path.read_text())
            updated = []
            for entry in data:
                if "TODO" in entry:
                    updated.append(entry)
                    continue
                try:
                    env = resolve_field(
                        cur, ctx, entry["model_name"], entry["field_name"]
                    )
                except NotFoundError as exc:
                    skipped.append(
                        f"resolve_field {entry['model_name']}.{entry['field_name']}: {exc}"
                    )
                    updated.append(entry)
                    continue
                r = env["result"]
                chain = [_normalise_entry(c) for c in r["chain"]]
                updated.append({
                    "model_name": r["model_name"],
                    "field_name": r["field_name"],
                    "chain": chain,
                    "effective": r["effective"],
                    "warnings": r["warnings"],
                })
            path.write_text(json.dumps(updated, indent=2, sort_keys=True) + "\n")

            # methods
            path = GOLDEN / "resolve_method.json"
            data = json.loads(path.read_text())
            updated = []
            for entry in data:
                if "TODO" in entry:
                    updated.append(entry)
                    continue
                try:
                    env = resolve_method(
                        cur, ctx, entry["model_name"], entry["method_name"]
                    )
                except NotFoundError as exc:
                    skipped.append(
                        f"resolve_method {entry['model_name']}.{entry['method_name']}: {exc}"
                    )
                    updated.append(entry)
                    continue
                r = env["result"]
                chain = [_normalise_entry(c) for c in r["chain"]]
                updated.append({
                    "model_name": r["model_name"],
                    "method_name": r["method_name"],
                    "chain": chain,
                    "chain_is_broken": r["chain_is_broken"],
                    "warnings": r["warnings"],
                })
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

    print("regenerated goldens at", GOLDEN)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
