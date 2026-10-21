# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backfill Module.auto_install_triggers from the manifests on disk (#380).

The install-set resolver (``module_inspect(method='dependencies')``,
``impact_analysis``) reads ``Module.auto_install_triggers``. A normal index run
writes it, but an incremental run skips every module whose repo HEAD did not
move, and ``--full`` takes about 20 hours. This script reads ONLY the
manifests (no model / view / JS parse, no git log per module) and sets
``auto_install_triggers`` and ``auto_install`` on the Module nodes that already
exist, with the same resolver the indexer uses
(``src.indexer.registry.auto_install_triggers``).

- Checkouts come from the repos registry (``repos.local_path``, joined to
  ``profiles.odoo_version``); the version key follows the indexer's version
  rule (``resolve_repo_version``: a standard branch name wins over the profile
  version).
- Only existing, profile-owned Module nodes are touched (MATCH, never MERGE):
  a module on disk that was never indexed is reported, not created; a
  dependency stub is never stamped.
- Idempotent: a node whose values already match is not written; a second run
  reports 0 changes.
- ``--dry-run`` reports what would change and writes nothing; ``--version``
  (repeatable) limits the run to those Odoo versions; ``--repo-id``
  (repeatable) to those repos.
- When several repos ship the same module name at one version they share one
  Module node (MERGE key ``(name, odoo_version)``); the repo with the lowest id
  wins here. That value stays until a run re-parses the module: an incremental
  run of another repo shipping the name rewrites it only when that module's
  directory changed (or the repo is re-indexed with ``--full``), so where the
  copies' manifests disagree, re-index the repo whose value should win with
  ``--full`` (or ``--repo-id`` this script to it alone).

Owner DSN required (reads ``repos``; writes Neo4j). Not for ``osm_reader``.

Usage:
    ~/.venv/odoo-semantic-mcp/bin/python ops/backfill_auto_install.py --dry-run
    ~/.venv/odoo-semantic-mcp/bin/python ops/backfill_auto_install.py --version 17.0
    ~/.venv/odoo-semantic-mcp/bin/python ops/backfill_auto_install.py
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src import config  # noqa: E402 (path setup must come first)
from src.indexer.registry import (  # noqa: E402
    _place_version,
    auto_install_triggers,
    get_manifest_finder,
    parse_manifest,
    resolve_repo_version,
)

log = logging.getLogger("backfill_auto_install")


@dataclass(frozen=True)
class ModuleTriggers:
    name: str
    odoo_version: str
    auto_install: bool
    triggers: list[str] | None


# ---------------------------------------------------------------------------
# Manifest side (no database)
# ---------------------------------------------------------------------------


def collect_repo_triggers(
    local_path: str,
    profile_version: str | None,
    branch: str | None,
) -> list[ModuleTriggers]:
    """Resolve the auto_install triggers of every installable module of a checkout.

    Same manifest dispatch, version rule and installable gate as
    ``build_registry_scan``; a name found twice keeps the shallowest manifest
    (the registry's tie-break after its version-format preference).
    """
    repo_ver = resolve_repo_version(
        local_path, branch=branch, profile_version=profile_version,
    )
    dispatch_version = repo_ver.odoo_version or profile_version or ""
    root = Path(local_path)
    manifests = sorted(
        get_manifest_finder(dispatch_version).find(local_path),
        key=lambda p: (len(Path(p).relative_to(root).parts), p),
    )
    seen: set[tuple[str, str]] = set()
    out: list[ModuleTriggers] = []
    for manifest_path in manifests:
        manifest = parse_manifest(manifest_path)
        if not manifest or not manifest.get("installable", True):
            continue
        name = Path(manifest_path).parent.name
        version = _place_version(
            str(manifest.get("version", "") or ""), repo_ver.odoo_version,
        )
        if version == "unknown" or (name, version) in seen:
            continue
        seen.add((name, version))
        triggers = auto_install_triggers(
            manifest, manifest.get("depends", []), version, name,
        )
        out.append(ModuleTriggers(name, version, triggers is not None, triggers))
    return out


# ---------------------------------------------------------------------------
# Graph side
# ---------------------------------------------------------------------------

_DIFF_FILTER = """
    UNWIND $rows AS r
    MATCH (m:Module {name: r.name, odoo_version: $v})
    WHERE size(coalesce(m.profile, [])) > 0
    WITH m, r,
         (coalesce(m.auto_install, false) <> r.auto_install
          OR (m.auto_install_triggers IS NULL) <> (r.triggers IS NULL)
          OR coalesce(m.auto_install_triggers, []) <> coalesce(r.triggers, [])) AS differs
"""


def apply_version(driver, odoo_version: str, rows: list[ModuleTriggers], *,
                  dry_run: bool) -> dict:
    """Write (or, dry-run, count) the changed nodes of one version.

    Returns {"matched": N, "changed": [names], "not_indexed": [names]}.
    """
    payload = [
        {"name": r.name, "auto_install": r.auto_install, "triggers": r.triggers}
        for r in rows
    ]
    with driver.session() as session:
        matched = session.run(
            _DIFF_FILTER + " RETURN m.name AS name, differs ORDER BY name ASC",
            rows=payload, v=odoo_version,
        ).data()
        changed = [row["name"] for row in matched if row["differs"]]
        if changed and not dry_run:
            session.run(
                _DIFF_FILTER + """
                WITH m, r WHERE differs
                SET m.auto_install = r.auto_install,
                    m.auto_install_triggers = r.triggers
                """,
                rows=payload, v=odoo_version,
            ).consume()
    names = {row["name"] for row in matched}
    return {
        "matched": len(matched),
        "changed": changed,
        "not_indexed": sorted(r.name for r in rows if r.name not in names),
    }


def _fetch_repos(pg_conn, repo_ids: list[int] | None) -> list[dict]:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.local_path, r.branch, p.odoo_version
            FROM repos r
            JOIN profiles p ON p.id = r.profile_id
            ORDER BY r.id
            """
        )
        rows = cur.fetchall()
    repos = [
        {"repo_id": r[0], "local_path": r[1], "branch": r[2], "odoo_version": r[3]}
        for r in rows
    ]
    if repo_ids:
        repos = [r for r in repos if r["repo_id"] in set(repo_ids)]
    return repos


def run(pg_conn, driver, *, versions: list[str] | None, repo_ids: list[int] | None,
        dry_run: bool) -> dict:
    by_version: dict[str, dict[str, ModuleTriggers]] = {}
    unreadable: list[str] = []
    for repo in _fetch_repos(pg_conn, repo_ids):
        path = repo["local_path"]
        if not path or not Path(path).is_dir():
            unreadable.append(f"repo {repo['repo_id']}: {path!r} is not a directory")
            continue
        for mt in collect_repo_triggers(path, repo["odoo_version"], repo["branch"]):
            if versions and mt.odoo_version not in versions:
                continue
            # Lowest repo id wins a shared name (repos are ordered by id).
            by_version.setdefault(mt.odoo_version, {}).setdefault(mt.name, mt)

    summary: dict = {"versions": {}, "unreadable": unreadable}
    for version in sorted(by_version, key=lambda v: (float(v) if _is_num(v) else 0, v)):
        rows = sorted(by_version[version].values(), key=lambda r: r.name)
        summary["versions"][version] = apply_version(driver, version, rows,
                                                     dry_run=dry_run)
    return summary


def _is_num(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def _print_summary(summary: dict, *, dry_run: bool) -> None:
    verb = "would change" if dry_run else "changed"
    for version, res in summary["versions"].items():
        log.info(
            "Odoo %s: %d indexed module(s) matched, %d %s, %d on disk not indexed",
            version, res["matched"], len(res["changed"]), verb, len(res["not_indexed"]),
        )
        if res["changed"]:
            log.info("  %s: %s", verb, ", ".join(res["changed"][:20])
                     + (" ..." if len(res["changed"]) > 20 else ""))
    for line in summary["unreadable"]:
        log.warning("skipped %s", line)


def main(argv: list[str] | None = None) -> int:
    config.init_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would change; write nothing.")
    parser.add_argument("--version", action="append", dest="versions",
                        help="Odoo version to backfill, e.g. 17.0 (repeatable).")
    parser.add_argument("--repo-id", action="append", type=int, dest="repo_ids",
                        help="repos.id to read (repeatable). Default: every repo.")
    args = parser.parse_args(argv)

    import psycopg2

    from src.indexer.pipeline import open_production_neo4j, production_pg_dsn

    pg_conn = psycopg2.connect(production_pg_dsn())
    driver = open_production_neo4j()
    try:
        summary = run(pg_conn, driver, versions=args.versions,
                      repo_ids=args.repo_ids, dry_run=args.dry_run)
        _print_summary(summary, dry_run=args.dry_run)
    finally:
        driver.close()
        pg_conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
