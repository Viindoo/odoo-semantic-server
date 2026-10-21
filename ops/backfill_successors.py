# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backfill the successor of retired ledger rows that have none (issue #380 item 3).

Rows retired before the N:1 merge oracle existed (viin_ai_rag, viin_ai_agent,
viin_ai_skill, viin_ai_search, viin_ai_memory at 17.0 -> viin_ai) carry their
removing commit but no successor. This re-runs the git evidence
(``src.indexer.lifecycle.removal_evidence``: rename, then merge) on exactly
those rows and records what git proves. Nothing else is touched.

* Dry run by default: prints what it WOULD record; ``--apply`` writes.
* Bounded: ``--limit`` rows per run (default 200), in ledger row-id order;
  narrow with ``--version`` / ``--profile``. A row git has no evidence for
  keeps no successor, so the run ends with the cursor of the next page
  (``--after-id <id>``) whenever the page was full: re-run with it to reach
  the rows after it (without it, the same first rows would be read again).
* Idempotent: only rows ``state='retired' AND successor_names IS NULL AND
  removing_commit_sha IS NOT NULL`` whose repo is still registered are read,
  and the write never overwrites a recorded successor
  (``ModulePresenceStore.record_successor``). A re-run records nothing new.
* Safe: a row is updated only when git's newest deleting commit of its
  manifest is the one the ledger recorded (a resurrected-and-removed-again
  module is skipped), and the successor is a name some repo's ledger row
  still ships at that version. Read-only git (``ls-tree`` / ``diff``), no
  fetch, no Neo4j, no re-index. Needs migration 0006 for merge successors
  (without it they are reported and skipped).

Usage (owner DSN, like the indexer - module_presence writes need it):
    ~/.venv/odoo-semantic-mcp/bin/python ops/backfill_successors.py
    ~/.venv/odoo-semantic-mcp/bin/python ops/backfill_successors.py --version 17.0 --apply

Exit 0 (also when nothing is found), 2 on a configuration error.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src import config  # noqa: E402
from src.db.module_presence import ModulePresenceStore  # noqa: E402
from src.db.module_presence import Successor as LedgerSuccessor  # noqa: E402
from src.indexer.lifecycle import removal_evidence  # noqa: E402

log = logging.getLogger("backfill_successors")

_MANIFEST_CANDIDATES = ("__manifest__.py", "__openerp__.py")
DEFAULT_LIMIT = 200


def backfill(
    store: ModulePresenceStore,
    conn,
    *,
    apply: bool,
    odoo_version: str | None = None,
    profile_name: str | None = None,
    limit: int = DEFAULT_LIMIT,
    after_id: int | None = None,
    evidence_fn=removal_evidence,
    out=None,
) -> dict[str, int]:
    """Re-evaluate retired rows without a successor; record git's answer when *apply*.

    Reads the page of at most *limit* rows with an id above *after_id*.
    Returns counts: candidates, found, recorded, no_evidence, commit_mismatch,
    not_recorded, and ``next_after_id`` (the cursor of the next page; 0 when
    the page was not full, i.e. nothing is left after it).
    """
    out = out or sys.stdout
    counts = dict.fromkeys(
        ("candidates", "found", "recorded", "no_evidence", "commit_mismatch",
         "not_recorded"), 0,
    )
    rows = store.retired_without_successor(
        odoo_version=odoo_version, profile_name=profile_name, limit=limit,
        after_id=after_id, conn=conn,
    )
    shipped: dict[str, set[str]] = {}
    for row in rows:
        counts["candidates"] += 1
        version = row["odoo_version"]
        if version not in shipped:
            shipped[version] = store.shipped_names(version, conn=conn)
        manifests = [row["manifest_file"]] if row.get("manifest_file") else []
        manifests += [m for m in _MANIFEST_CANDIDATES if m not in manifests]
        evidence, successor = evidence_fn(
            str(row["local_path"]), row["path"] or row["name"], manifests, shipped[version],
        )
        label = f"{row['name']}@{version} ({row['profile_name']}, row {row['id']})"
        if evidence is None or successor is None:
            counts["no_evidence"] += 1
            continue
        if evidence.sha != row["removing_commit_sha"]:
            counts["commit_mismatch"] += 1
            print(f"skip {label}: newest deleting commit {evidence.sha[:10]} is not the "
                  f"recorded {str(row['removing_commit_sha'])[:10]}", file=out)
            continue
        counts["found"] += 1
        verb = "merged into" if successor.source == "git_merge" else "renamed to"
        if not apply:
            print(f"would record {label}: {verb} {', '.join(successor.names)} "
                  f"({successor.source}, commit {evidence.sha[:10]})", file=out)
            continue
        if store.record_successor(
            row["id"], LedgerSuccessor(successor.names, successor.source), conn=conn,
        ):
            counts["recorded"] += 1
            print(f"recorded {label}: {verb} {', '.join(successor.names)} "
                  f"({successor.source})", file=out)
        else:
            counts["not_recorded"] += 1
            print(f"not recorded {label}: row changed meanwhile or migration 0006 "
                  "missing (see WARNING)", file=out)
    mode = "applied" if apply else "dry run"
    print(f"{mode}: " + ", ".join(f"{k} {v}" for k, v in counts.items()), file=out)
    full_page = limit > 0 and len(rows) >= limit
    counts["next_after_id"] = int(rows[-1]["id"]) if full_page else 0
    if full_page:
        print(f"more candidate rows may follow: re-run with --after-id "
              f"{counts['next_after_id']}", file=out)
    return counts


def _build_conn():
    import psycopg2

    dsn = config.from_env_or_ini("PG_DSN", "database", "pg_dsn", fallback=None)
    if not dsn:
        raise RuntimeError(
            "PG_DSN not set. Export PG_DSN=postgresql://... or configure "
            "[database] pg_dsn in odoo-semantic.conf."
        )
    conn = psycopg2.connect(dsn)
    conn.autocommit = True  # each record_successor is its own locked transaction
    return conn


def main(argv: list[str] | None = None) -> int:
    config.init_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--version", help="only this Odoo version (e.g. 17.0)")
    parser.add_argument("--profile", help="only rows of this profile")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"rows per run (default {DEFAULT_LIMIT})")
    parser.add_argument("--after-id", type=int, default=None,
                        help="only rows with a ledger id above this (the cursor a "
                             "previous run printed)")
    args = parser.parse_args(argv)
    try:
        conn = _build_conn()
    except RuntimeError as exc:
        log.error("%s", exc)
        return 2
    try:
        backfill(
            ModulePresenceStore(None), conn,  # type: ignore[arg-type]
            apply=args.apply, odoo_version=args.version, profile_name=args.profile,
            limit=args.limit, after_id=args.after_id,
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
