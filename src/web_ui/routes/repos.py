# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/routes/repos.py
"""Profiles & Repos management routes (M8 W1 — pure JSON API).

B3 split: the ~15 endpoints that used to live here are now grouped into three
sibling sub-routers, each mounted under this module's ``/api/repos`` prefix:

- ``repos_profiles`` — profile CRUD (list/create/set-parent/update/delete).
- ``repos_crud``     — repo CRUD + ssh-keys-list + core-symbol-counts.
- ``repos_indexing`` — clone + index triggers (clone-all/clone-status/index/
  reset-embed/index-all).

Path strings, status codes, dependencies and behaviour are byte-identical to
the pre-split routes. ``app.include_router(repos.router)`` is unchanged: this
module still exposes a single ``router`` with ``prefix="/api/repos"`` whose
``router.routes`` is the union of all three sub-routers.

The Neo4j + pgvector cleanup helpers stay defined HERE (not in a sibling
module) for two reasons: (1) they call each other (``_collect_module_names_*``
and ``_delete_neo4j_*`` both call ``_get_neo4j_writer``), so co-location keeps
``mock.patch("...repos._get_neo4j_writer")`` effective across the whole chain;
(2) the existing test patch surface is ``src.web_ui.routes.repos._*``. The
endpoint modules call them via ``repos._delete_*`` (namespace lookup at call
time) so that patch surface is preserved unchanged.

``import subprocess`` is also kept here because tests patch
``src.web_ui.routes.repos.subprocess.Popen``; the ``subprocess`` module is a
process-wide singleton, so patching ``Popen`` on it is honoured by the spawn
sites in ``repos_crud`` / ``repos_indexing`` too.

Note: job status/reset routes were moved to src/web_ui/routes/jobs.py
(Phase 8 review) so that clients polling /api/jobs/{id}/status resolve
correctly. The original prefix "/api/repos" caused 404s for those paths.
"""
import logging
import subprocess  # noqa: F401  (kept: tests patch repos.subprocess.Popen — shared singleton)

from fastapi import APIRouter

from src.web_ui.routes import repos_crud, repos_indexing, repos_profiles

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/repos")


def _get_neo4j_writer():
    """Build a Neo4jWriter from config, or None if password is missing."""
    from src import config
    from src.indexer.writer_neo4j import Neo4jWriter

    uri = config.from_env_or_ini(
        "NEO4J_URI", "database", "neo4j_uri",
        fallback="bolt://localhost:7687",
    )
    user = config.from_env_or_ini(
        "NEO4J_USER", "database", "neo4j_user", fallback="neo4j",
    )
    password = config.from_env_or_ini(
        "NEO4J_PASSWORD", "database", "neo4j_password", fallback=None,
    )
    if not password:
        return None
    return Neo4jWriter(uri=uri, user=user, password=password)


def _delete_neo4j_for_repos(repo_cleanup_pairs: list[dict]) -> tuple[int, int]:
    """Delete Neo4j Module nodes + children for each (basename, version) pair.

    Returns (total_modules_deleted, total_children_deleted).
    """
    total_modules = 0
    total_children = 0
    for pair in repo_cleanup_pairs:
        basename = pair["basename"]
        version = pair["version"]
        try:
            writer = _get_neo4j_writer()
            if writer is None:
                continue
            try:
                counts = writer.delete_modules_scoped(basename, version)
                total_modules += counts.get("modules", 0)
                total_children += counts.get("children", 0)
            finally:
                writer.close()
        except Exception as e:
            _logger.warning(
                "Neo4j cleanup failed for repo %s version %s: %s", basename, version, e
            )
    return total_modules, total_children


def _collect_module_names_for_repos(
    repo_cleanup_pairs: list[dict],
) -> dict[str, list[str]]:
    """Query Neo4j for Odoo module names belonging to each (basename, version) pair.

    Returns a dict mapping version → list of module names.
    Must be called BEFORE _delete_neo4j_for_repos so the Module nodes still exist.
    """
    by_version: dict[str, list[str]] = {}
    for pair in repo_cleanup_pairs:
        version = pair["version"]
        basename = pair["basename"]
        try:
            writer = _get_neo4j_writer()
            if writer is None:
                _logger.warning(
                    "Neo4j unavailable — cannot resolve module names for repo %s v%s",
                    basename,
                    version,
                )
                continue
            try:
                with writer.driver.session() as session:
                    result = session.run(
                        "MATCH (m:Module {repo: $repo, odoo_version: $v}) "
                        "RETURN m.name AS module_name",
                        repo=basename,
                        v=version,
                    )
                    names = [row["module_name"] for row in result]
            finally:
                writer.close()
            by_version.setdefault(version, []).extend(names)
        except Exception as e:
            _logger.warning(
                "Failed to collect module names for repo %s v%s: %s", basename, version, e
            )
    return by_version


def _delete_embeddings_for_repos(
    repo_cleanup_pairs: list[dict],
    module_names_by_version: dict[str, list[str]] | None = None,
) -> int:
    """Delete pgvector embeddings for each (basename, version) repo pair.

    Resolves the correct Odoo module names from ``module_names_by_version`` (a dict
    produced by ``_collect_module_names_for_repos`` called BEFORE the Neo4j delete).
    The embeddings table stores Odoo module names (e.g. ``sale``, ``account``), NOT
    repo basenames — using basenames was a production bug that made every DELETE a
    no-op.

    If ``module_names_by_version`` is None or empty for a version, the DELETE is a
    correct no-op (repo was never indexed → no embeddings to clean).

    Returns total embeddings rows deleted.
    """
    if module_names_by_version is None:
        module_names_by_version = {}

    total = 0

    # Collect all versions we need to clean (deduplicated)
    versions_seen: set[str] = {pair["version"] for pair in repo_cleanup_pairs}
    if not any(module_names_by_version.get(v) for v in versions_seen):
        return 0  # nothing to delete

    try:
        from src.db.pg import get_pool

        for version in versions_seen:
            module_list = module_names_by_version.get(version, [])
            if not module_list:
                continue  # repo never indexed → no embeddings to delete
            try:
                with get_pool().checkout() as conn:
                    rowcount = get_pool().execute(
                        conn,
                        "DELETE FROM embeddings "
                        "WHERE odoo_version = %s AND module = ANY(%s)",
                        (version, module_list),
                    )
                    total += rowcount
            except Exception as e:
                _logger.warning(
                    "pgvector cleanup failed for version %s modules %s: %s",
                    version,
                    module_list,
                    e,
                )
    except Exception as e:
        _logger.warning("PG connection unavailable — skipping embeddings cleanup: %s", e)

    return total


# Mount the three sub-routers onto this module's prefixed router. Order matches
# the original endpoint declaration order (profiles → repo CRUD → clone/index)
# so route ordering in router.routes is preserved.
router.include_router(repos_profiles.router)
router.include_router(repos_crud.router)
router.include_router(repos_indexing.router)


# Job status and reset routes have been moved to src/web_ui/routes/jobs.py
# (prefix="/api/jobs") per Phase 8 review — see that module for job_status
# and reset_stuck_job handlers.
