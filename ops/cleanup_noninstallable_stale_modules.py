# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cleanup stale Module nodes that are now `installable=False`.

WHY THIS EXISTS
---------------
A module that was `installable=True` in a PRIOR index has Module + Field/Method/
View/QWebTmpl/JSPatch/OWLComp nodes in Neo4j. If a later manifest flips it to
`installable=False`, `build_registry` (src/indexer/registry.py) silently drops it
from fresh scans — but its *path still exists on disk*, so `gc_stale_modules`
(src/indexer/writer_neo4j.py) does NOT remove it (GC only deletes modules whose
path vanished). The old nodes linger as stale graph entries (~25 observed on v18).

This script closes that specific gap. It is CONSERVATIVE: it only deletes a
module when BOTH conditions hold:
  (a) the module currently exists on disk with `installable=False`, AND
  (b) it has Module node(s) in the graph for that (module, odoo_version).
Modules ABSENT from disk are left untouched — that is `gc_stale_modules`' job.

It is DRY-RUN by default. Pass --apply to actually DETACH DELETE.

OWNERSHIP / RBAC
----------------
Owner-run only (BypassRLS). NEVER run as osm_reader — deletions require the owner
DSN. Connects via the same env/config the other ops scripts use (src.config:
PG_DSN + NEO4J_* env / odoo-semantic.conf), and reuses the registry's manifest
parsing (get_manifest_finder + parse_manifest + the `installable` check) — it does
NOT duplicate the parse.

Usage:
    ~/.venv/odoo-semantic-mcp/bin/python ops/cleanup_noninstallable_stale_modules.py
    ~/.venv/odoo-semantic-mcp/bin/python ops/cleanup_noninstallable_stale_modules.py --apply
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow running directly from repo root or from inside ops/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src import config  # noqa: E402 (path setup must come first)
from src.indexer.registry import (  # noqa: E402
    get_manifest_finder,
    parse_manifest,
)

log = logging.getLogger("cleanup_noninstallable_stale_modules")


# ---------------------------------------------------------------------------
# Pure, testable classification helper (no DB, no Neo4j)
# ---------------------------------------------------------------------------


def noninstallable_module_names(local_path: str, odoo_version: str) -> set[str]:
    """Return the set of module names on disk that are currently `installable=False`.

    Reuses the SAME logic the registry uses: the version-dispatched
    ``get_manifest_finder`` to locate manifests, ``parse_manifest`` to read them,
    and the identical ``installable`` semantics as
    ``build_registry`` — a module is non-installable iff its manifest parses to a
    truthy dict AND ``manifest.get('installable', True)`` is falsey. Manifests
    that fail to parse (empty dict) are NOT classified as non-installable (they
    are simply unparseable — the registry skips them for a different reason and
    they never produced graph nodes via this path).

    Args:
        local_path:   Absolute checkout path of the repo (``repos.local_path``).
        odoo_version: Odoo version label used to dispatch the manifest finder.

    Returns:
        Set of module directory names (Module.name keys) marked non-installable.
    """
    finder = get_manifest_finder(odoo_version)
    result: set[str] = set()
    for manifest_path in finder.find(local_path):
        manifest = parse_manifest(manifest_path)
        if not manifest:
            # Unparseable — registry skips as "unparseable", not "not-installable".
            continue
        if not manifest.get("installable", True):
            result.add(Path(manifest_path).parent.name)
    return result


# ---------------------------------------------------------------------------
# DB access — repos joined to profiles for (local_path, odoo_version)
# ---------------------------------------------------------------------------


def _fetch_registered_repos(pg_conn) -> list[dict]:
    """Return [{repo_id, local_path, odoo_version}] for every registered repo.

    Joins ``repos`` to ``profiles`` for the Odoo version (profiles is the SSOT
    for odoo_version). Owner DSN required — this is a plain read but the script
    as a whole performs owner-only deletes.
    """
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.local_path, p.odoo_version
            FROM repos r
            JOIN profiles p ON p.id = r.profile_id
            ORDER BY r.id
            """
        )
        rows = cur.fetchall()
    return [
        {"repo_id": r[0], "local_path": r[1], "odoo_version": r[2]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Neo4j — which of these (module, version) have nodes, and delete them
# ---------------------------------------------------------------------------


def _existing_module_nodes(
    driver, module_names: list[str], odoo_version: str,
) -> list[str]:
    """Return the subset of *module_names* that have a :Module node for *odoo_version*."""
    if not module_names:
        return []
    with driver.session() as session:
        row = session.run(
            """
            MATCH (m:Module {odoo_version: $version})
            WHERE m.name IN $names
            RETURN collect(DISTINCT m.name) AS names
            """,
            names=module_names,
            version=odoo_version,
        ).single()
    return list(row["names"]) if row is not None else []


def _delete_module_and_children(
    driver, module_name: str, odoo_version: str,
) -> dict:
    """DETACH DELETE one Module node + its owned child nodes for *odoo_version*.

    Child ownership mirrors ``Neo4jWriter.delete_modules_scoped``: child nodes
    (Model/Field/Method/View/QWebTmpl/JSPatch/OWLComp) carry ``module`` +
    ``odoo_version`` properties and are deleted only when scoped to this exact
    (module, version) pair, so other repos sharing the version are untouched.

    Returns {"modules": N, "children": M}.
    """
    with driver.session() as session:
        children_row = session.run(
            """
            MATCH (child)
            WHERE child.module = $name AND child.odoo_version = $version
              AND (child:Model OR child:Field OR child:Method OR child:View
                   OR child:QWebTmpl OR child:JSPatch OR child:OWLComp)
            WITH collect(child) AS children
            UNWIND children AS c
            DETACH DELETE c
            RETURN count(c) AS cc
            """,
            name=module_name,
            version=odoo_version,
        ).single()
        children = children_row["cc"] if children_row is not None else 0

        modules_row = session.run(
            """
            MATCH (m:Module {name: $name, odoo_version: $version})
            WITH collect(m) AS mods
            UNWIND mods AS m
            DETACH DELETE m
            RETURN count(m) AS mc
            """,
            name=module_name,
            version=odoo_version,
        ).single()
        modules = modules_row["mc"] if modules_row is not None else 0

    return {"modules": modules, "children": children}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(pg_conn, driver, *, apply: bool) -> dict:
    """Scan every registered repo, find stale non-installable Module nodes, report/delete.

    Args:
        pg_conn: open psycopg2 connection (owner DSN).
        driver:  open neo4j driver (owner credentials).
        apply:   when True, DETACH DELETE the targeted nodes; otherwise dry-run.

    Returns a summary dict with totals + per-repo breakdown.
    """
    repos = _fetch_registered_repos(pg_conn)
    summary: dict = {
        "repos_scanned": len(repos),
        "modules_targeted": 0,
        "modules_deleted": 0,
        "children_deleted": 0,
        "per_repo": [],
    }

    for repo in repos:
        local_path = repo["local_path"]
        version = repo["odoo_version"]
        repo_id = repo["repo_id"]

        if not local_path or not Path(local_path).is_dir():
            log.warning(
                "repo id=%s local_path %r missing on disk — skipping (cannot "
                "classify installable state; not this script's job to delete "
                "absent modules — see gc_stale_modules).",
                repo_id, local_path,
            )
            summary["per_repo"].append(
                {
                    "repo_id": repo_id,
                    "local_path": local_path,
                    "odoo_version": version,
                    "skipped": "local_path missing",
                }
            )
            continue

        non_installable = sorted(noninstallable_module_names(local_path, version))
        # Only target those that actually have nodes in the graph (condition b).
        with_nodes = sorted(
            _existing_module_nodes(driver, non_installable, version)
        )

        repo_entry = {
            "repo_id": repo_id,
            "local_path": local_path,
            "odoo_version": version,
            "noninstallable_on_disk": len(non_installable),
            "targeted_with_nodes": with_nodes,
            "deleted_modules": 0,
            "deleted_children": 0,
        }

        for module_name in with_nodes:
            summary["modules_targeted"] += 1
            if apply:
                counts = _delete_module_and_children(driver, module_name, version)
                repo_entry["deleted_modules"] += counts["modules"]
                repo_entry["deleted_children"] += counts["children"]
                summary["modules_deleted"] += counts["modules"]
                summary["children_deleted"] += counts["children"]

        summary["per_repo"].append(repo_entry)

        if with_nodes:
            verb = "DELETED" if apply else "WOULD DELETE"
            log.info(
                "repo id=%s (v%s) %s %d stale non-installable module(s): %s",
                repo_id, version, verb, len(with_nodes), ", ".join(with_nodes),
            )

    return summary


def _print_summary(summary: dict, *, apply: bool) -> None:
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"\n=== cleanup_noninstallable_stale_modules [{mode}] ===")
    print(f"repos scanned:      {summary['repos_scanned']}")
    print(f"modules targeted:   {summary['modules_targeted']}")
    if apply:
        print(f"modules deleted:    {summary['modules_deleted']}")
        print(f"children deleted:   {summary['children_deleted']}")
    else:
        print("(dry-run — nothing deleted; re-run with --apply to delete)")
    print("per-repo:")
    for entry in summary["per_repo"]:
        if "skipped" in entry:
            print(
                f"  - repo {entry['repo_id']} (v{entry['odoo_version']}): "
                f"SKIPPED ({entry['skipped']})"
            )
            continue
        targeted = entry["targeted_with_nodes"]
        print(
            f"  - repo {entry['repo_id']} (v{entry['odoo_version']}): "
            f"{entry['noninstallable_on_disk']} non-installable on disk, "
            f"{len(targeted)} with graph nodes"
            + (f" → {', '.join(targeted)}" if targeted else "")
        )


# ---------------------------------------------------------------------------
# Connection builders (mirror ops/backfill_patterns.py + pipeline helpers)
# ---------------------------------------------------------------------------


def _build_pg_conn():
    import psycopg2

    dsn = config.from_env_or_ini("PG_DSN", "database", "pg_dsn", fallback=None)
    if not dsn:
        raise RuntimeError(
            "PG_DSN not set. Export PG_DSN=postgresql://... or configure "
            "[database] pg_dsn in odoo-semantic.conf. (Owner DSN required — "
            "never osm_reader.)"
        )
    return psycopg2.connect(dsn)


def _build_neo4j_driver():
    from neo4j import GraphDatabase

    uri = config.from_env_or_ini(
        "NEO4J_URI", "database", "neo4j_uri", fallback="bolt://localhost:7687",
    )
    user = config.from_env_or_ini(
        "NEO4J_USER", "database", "neo4j_user", fallback="neo4j",
    )
    password = config.from_env_or_ini(
        "NEO4J_PASSWORD", "database", "neo4j_password", fallback=None,
    )
    if not password:
        raise RuntimeError(
            "Neo4j password missing. Set NEO4J_PASSWORD env var OR "
            "neo4j_password in [database] section of odoo-semantic.conf."
        )
    return GraphDatabase.driver(uri, auth=(user, password))


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    # ADR-0031: load .env at the CLI entry point (idempotent, main()-only) so
    # PG_DSN / NEO4J_PASSWORD resolve on a fresh prod box without manual sourcing.
    config.init_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually DETACH DELETE the stale nodes (default: dry-run).",
    )
    args = parser.parse_args(argv)

    pg_conn = _build_pg_conn()
    driver = _build_neo4j_driver()
    try:
        summary = run(pg_conn, driver, apply=args.apply)
        _print_summary(summary, apply=args.apply)
    finally:
        driver.close()
        pg_conn.close()

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
