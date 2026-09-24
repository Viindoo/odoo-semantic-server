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

The repo-removal helper (``_remove_repos_through_ledger``, ADR-0056 ledger
first, then graph, then Postgres) and ``_get_neo4j_writer`` stay defined HERE
(not in a sibling module) so ``mock.patch("...repos._get_neo4j_writer")`` is
effective across the whole chain; the endpoint modules call them via
``repos._*`` (namespace lookup at call time) so the test patch surface
``src.web_ui.routes.repos._*`` works for every endpoint.

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
from collections.abc import Callable
from pathlib import Path

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


def _attach_lifecycle(repos: list[dict], *, is_admin: bool) -> list[dict]:
    """Add the module lifecycle view (ADR-0056) to repo rows, in place.

    Every row gets ``presence_head_sha`` (the HEAD the ledger last reflects;
    differs from ``head_sha`` while the ledger lags the graph),
    ``lifecycle_attention`` / ``lifecycle_attention_at`` (why the last index
    run or reconcile needs an operator; NULL when clean) and
    ``lifecycle_counts`` ``{present, excluded, retired, retire_pending,
    needs_rewrite}`` from the ledger (None when the ledger is unreadable).

    The attention text can name repos of other tenants (an undecidable
    retirement lists the unsynced repos that block it), so a non-admin gets
    ``lifecycle_attention`` None and only the timestamp says attention is
    needed (ADR-0034 fail-closed, same rule as ``clone_error_msg``).
    """
    counts: dict[int, dict[str, int]] | None
    try:
        from src.db.module_presence import ModulePresenceStore
        from src.db.pg import get_pool

        counts = ModulePresenceStore(get_pool()).lifecycle_counts(r["id"] for r in repos)
    except Exception as e:  # noqa: BLE001 - the listing must not fail on the ledger
        _logger.warning("Repo lifecycle counts unavailable: %s", e)
        counts = None
    for repo in repos:
        repo["presence_head_sha"] = repo.get("presence_head_sha")
        repo["lifecycle_attention_at"] = repo.get("lifecycle_attention_at")
        repo["lifecycle_attention"] = repo.get("lifecycle_attention") if is_admin else None
        repo["lifecycle_counts"] = counts.get(repo["id"]) if counts is not None else None
    return repos


class RemovalBusy(Exception):
    """A lock the removal must hold is taken; nothing was changed (HTTP 409)."""


def _remove_repos_through_ledger[T](
    repos: list[dict], *, profile_name: str, pg_delete: Callable[[], T],
) -> tuple[T, dict]:
    """Remove *repos* (rows of ``repo_store().get_repo_by_id`` shape) ledger-first.

    Order (review H3): every lock first, then the ledger, then the graph, and
    the Postgres delete last, so the ledger still names each row's repo when
    ownership is decided and the ``repo_removed`` history survives the delete
    (``module_presence.repo_id`` becomes NULL, ADR-0056):

    1. On one dedicated connection (never a pool connection pinned for the
       wait): the profile's indexer lock (try once: an index run of the
       profile is in progress -> busy), each repo's git lock (ADR-0035) and the
       ledger lock ``retire:<v>`` of every version the repos have rows at,
       each waited for up to ``WEBUI_LIFECYCLE_LOCK_WAIT_SECONDS``. A lock not
       obtained raises :class:`RemovalBusy` before anything is written.
    2. ``mark_repo_removed`` flags every non-retired row ``retire_pending``.
    3. ``reconcile_removed_repos`` per version: a module another repo still
       ships keeps its node, with that repo as the only owner; a module nobody
       else ships is retired with its subtree and the departing profiles'
       embeddings; residue the ledger never recorded is retired when no
       unsynced repo can claim it. Skipped (rows stay pending for the next
       index run's reconcile) when Neo4j is not configured or unreachable.
    4. *pg_delete* runs while the locks are still held, so no index run can
       re-observe the repo between its reconcile and its delete.

    Returns ``(pg_delete(), summary)``; *summary* is JSON-ready (see
    :func:`_removal_summary`).
    """
    from contextlib import ExitStack

    import psycopg2

    from src.constants import WEBUI_LIFECYCLE_LOCK_WAIT_SECONDS
    from src.db.exceptions import LifecycleLockTimeout
    from src.db.module_presence import ModulePresenceStore
    from src.db.pg import advisory_lock, get_pool
    from src.indexer.pipeline import _profile_lock_id, _repo_lock_id

    wait = WEBUI_LIFECYCLE_LOCK_WAIT_SECONDS
    pool = get_pool()
    store = ModulePresenceStore(pool, lock_wait_seconds=wait)
    conn = psycopg2.connect(pool.dsn)
    conn.autocommit = True
    try:
        with ExitStack() as locks:
            if not locks.enter_context(advisory_lock(conn, _profile_lock_id(profile_name))):
                raise RemovalBusy(f"Cannot delete: indexer running for profile {profile_name}")
            for repo in sorted(repos, key=lambda r: r["id"]):
                if not locks.enter_context(
                    advisory_lock(conn, _repo_lock_id(repo["id"]), wait_seconds=wait),
                ):
                    raise RemovalBusy(
                        f"Cannot delete: a git operation is running for repo id={repo['id']} "
                        f"(waited {wait:g}s); retry when it finished"
                    )
            versions = {r["odoo_version"] for r in repos if r.get("odoo_version")}
            for repo in repos:
                versions |= {
                    row["odoo_version"] for row in store.rows_for_repo(repo["id"], conn=conn)
                }
            try:
                for version in sorted(versions):
                    locks.enter_context(store.version_lock(version, conn=conn))
            except LifecycleLockTimeout as exc:
                raise RemovalBusy(
                    f"Cannot delete: a module lifecycle reconcile is running ({exc}); "
                    "retry when the index run finished"
                ) from exc

            flagged: dict[str, set[str]] = {v: set() for v in versions}
            for repo in repos:
                for row in store.mark_repo_removed(repo["id"], conn=conn):
                    flagged.setdefault(row["odoo_version"], set()).add(row["name"])

            reports, deferred = _reconcile_removed(
                repos, store, conn, flagged, profile_name=profile_name,
            )
            repo_ids = {r["id"] for r in repos}
            still_pending = {
                v: sorted({
                    row["name"] for row in store.pending_retirements(v, conn=conn)
                    if row["repo_id"] in repo_ids
                })
                for v in sorted(flagged)
            }
            result = pg_delete()
        return result, _removal_summary(flagged, reports, deferred, still_pending)
    finally:
        conn.close()


def _reconcile_removed(
    repos: list[dict], store, conn, flagged: dict[str, set[str]], *, profile_name: str,
) -> tuple[dict, str | None]:
    """Run ``reconcile_removed_repos`` per version; returns ``(reports, deferred_reason)``."""
    from src.indexer.reconcile import reconcile_removed_repos

    writer = _get_neo4j_writer()
    if writer is None:
        _logger.warning(
            "Repo removal: Neo4j not configured; %d ledger row(s) left pending for "
            "the next index run's reconcile",
            sum(len(n) for n in flagged.values()),
        )
        return {}, "neo4j not configured"
    basenames = {Path(r["local_path"]).name for r in repos if r.get("local_path")}
    repo_ids = {r["id"] for r in repos}
    profiles = {profile_name} | {r["profile_name"] for r in repos if r.get("profile_name")}
    reports: dict = {}
    try:
        run_started_at = writer.server_now()
        for version in sorted(flagged):
            reports[version] = reconcile_removed_repos(
                version, writer=writer, store=store, conn=conn,
                run_started_at=run_started_at, names=flagged[version],
                basenames=basenames, removed_repo_ids=repo_ids,
                removed_profiles=profiles,
            )
    except Exception as exc:  # noqa: BLE001 - rows stay pending; the delete proceeds
        _logger.warning(
            "Repo removal: reconcile incomplete (%s: %s); pending ledger rows are "
            "decided by the next index run's reconcile", type(exc).__name__, exc,
        )
        return reports, f"{type(exc).__name__}: {exc}"[:300]
    finally:
        writer.close()
    return reports, None


def _removal_summary(
    flagged: dict[str, set[str]],
    reports: dict,
    deferred: str | None,
    still_pending: dict[str, list[str]],
) -> dict:
    """JSON-ready summary of a ledger-first removal.

    ``neo4j_modules`` / ``neo4j_children`` / ``embeddings`` keep the meaning of
    the pre-ledger response (what was deleted). Per version: ``retired`` (gone
    with subtree + embeddings), ``owner_dropped`` (kept: another repo ships
    them), ``residue_retired`` / ``residue_kept`` (nodes the ledger never
    recorded), ``pending`` (ledger rows left for the next index run's
    reconcile, with ``undecidable`` / ``errors`` saying why).
    """
    per_version = {}
    for version in sorted(flagged):
        rep = reports.get(version)
        per_version[version] = {
            "flagged": len(flagged[version]),
            "retired": list(rep.retired) if rep else [],
            "owner_dropped": list(rep.owner_dropped) if rep else [],
            "residue_retired": list(rep.orphans_swept) if rep else [],
            "residue_kept": sorted(rep.orphans_deferred) if rep else [],
            "pending": still_pending.get(version, []),
            "undecidable": dict(rep.undecidable) if rep else {},
            "errors": dict(rep.errors) if rep else {},
        }
    reps = list(reports.values())
    return {
        "neo4j_modules": sum(r.modules_deleted for r in reps),
        "neo4j_children": sum(r.children_deleted for r in reps),
        "embeddings": sum(r.embeddings_deleted for r in reps),
        "lifecycle": {
            "reconciled": deferred is None,
            "deferred_reason": deferred,
            "versions": per_version,
        },
    }


# Mount the three sub-routers onto this module's prefixed router. Order matches
# the original endpoint declaration order (profiles → repo CRUD → clone/index)
# so route ordering in router.routes is preserved.
router.include_router(repos_profiles.router)
router.include_router(repos_crud.router)
router.include_router(repos_indexing.router)


# Job status and reset routes have been moved to src/web_ui/routes/jobs.py
# (prefix="/api/jobs") per Phase 8 review — see that module for job_status
# and reset_stuck_job handlers.
