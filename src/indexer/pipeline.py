# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/pipeline.py
"""Orchestrator: scan repos → parse → write to Neo4j.

Pipeline stages (per CLAUDE.md pipeline convention):
    scanner → registry → resolver → parser → writer

Public API:
    index_profile(pg_conn, *, profile_name) -> summary dict
    index_all(pg_conn) -> aggregate summary dict
    open_production_neo4j() -> neo4j.Driver   (external callers / health check)
    open_production_pg() -> psycopg2.connection (used by __main__.py)
"""
import concurrent.futures
import hashlib
import logging
import re
import time
from contextlib import contextmanager
from pathlib import Path

from neo4j import GraphDatabase, NotificationMinimumSeverity

from src import clone_ops as _clone_ops
from src import config
from src.constants import EMBEDDER_MAX_BATCH
from src.db.pg import repo_store

# --- Collaborators kept on THIS module namespace for the moved _index_repo ---
# After the B6 split _index_repo lives in pipeline_repo.py but resolves these
# back through ``src.indexer.pipeline`` at call time (``_pipeline.build_registry``
# / ``topological_sort``; ``parser_*`` / ``_incremental`` by module identity).
# The test suite ALSO patches them on this namespace
# (``patch("src.indexer.pipeline.build_registry")``, ``...parser_python.parse_module``,
# ``..._incremental.get_repo_head``, ...). So they must stay imported here even
# though nothing in THIS file's body references them — hence the noqa: F401.
from src.indexer import incremental as _incremental  # noqa: F401
from src.indexer import (  # noqa: F401
    parser_js,
    parser_python,
    parser_qweb,
    parser_xml,
)
from src.indexer.protocols import IndexWriterProtocol
from src.indexer.registry import build_registry, build_registry_scan  # noqa: F401
from src.indexer.resolver import topological_sort  # noqa: F401
from src.indexer.stop import (
    close_unless_held,
    hold_until_exit,
    raise_if_stop_requested,
    stop_requested,
    stoppable_executor,
)
from src.indexer.writer_neo4j import Neo4jWriter

_logger = logging.getLogger(__name__)


class IndexRunError(RuntimeError):
    """An index run that finished, post-passes and lifecycle reconcile included,
    but with at least one repo or profile that failed to index.

    ``summary`` is what the run would have returned (counters of what WAS
    indexed and ``lifecycle``), so the caller still reports the lifecycle
    outcome (tripped gates, deferred presence, attention) of the healthy repos.
    """

    def __init__(self, message: str, summary: dict) -> None:
        super().__init__(message)
        self.summary = summary


def _profile_lock_id(profile_name: str) -> int:
    """Hash profile name to a 31-bit advisory lock id."""
    return int(hashlib.md5(f"odoo-semantic-{profile_name}".encode()).hexdigest(), 16) % (2**31)


# The per-repo git lock (ADR-0035 D2) lives in the leaf ``src.clone_ops``,
# shared with the cloner (issue #383 item 2); re-exported under the names
# callers and tests use. Contention raises ``RepoGitLockBusy``, a RuntimeError.
_repo_lock_id = _clone_ops.repo_lock_id
_repo_git_lock = _clone_ops.repo_git_lock
_checkout_dir_lock = _clone_ops.checkout_dir_lock


@contextmanager
def _indexer_lock(pg_conn, profile_name: str):
    """Postgres advisory lock — prevents concurrent indexer runs for a profile.

    Auto-releases on process exit/crash (unlike fcntl which is process-local).
    Cross-container safe — lock lives in PostgreSQL, not filesystem.
    Each profile gets its own lock id, so parallel indexing of different profiles
    is allowed.

    A stopped run (SIGTERM, ``src.indexer.stop``) leaves the block while its
    repo workers may still be running: the lock is then NOT released and
    *pg_conn* is held open (``stop.hold_until_exit``) until the process ends,
    so no second run of the profile can start beside a worker that still
    writes.
    """
    from src.db.pg import advisory_lock
    lock_id = _profile_lock_id(profile_name)
    with advisory_lock(pg_conn, lock_id, keep_if=stop_requested) as acquired:
        if not acquired:
            raise RuntimeError(
                f"Indexer already running for profile {profile_name!r} "
                f"(Postgres advisory lock {lock_id} held). "
                "Wait for it to finish or restart PostgreSQL to release stale lock."
            )
        try:
            yield
        finally:
            if stop_requested():
                hold_until_exit(pg_conn)


def indexer_is_running(pg_conn, profile_name: str) -> bool:
    """Non-destructive advisory lock peek — True if the indexer is currently running
    for the given profile.

    Acquire-then-release pattern: avoids pg_locks table scan, stays consistent
    with the same lock id that _indexer_lock uses. Caller's connection must be
    autocommit (Web UI _get_conn already sets autocommit=True).
    """
    from src.db.pg import advisory_lock
    lock_id = _profile_lock_id(profile_name)
    with advisory_lock(pg_conn, lock_id) as acquired:
        pass  # advisory_lock releases on exit if acquired
    return not acquired


# ---------------------------------------------------------------------------
# Production connection helpers (consumed by __main__.py)
# ---------------------------------------------------------------------------

def _neo4j_creds() -> tuple[str, str, str]:
    """Return (uri, user, password) — single source of truth for Neo4j connection.

    Priority: NEO4J_* env (Docker/CI/systemd) → [database]/neo4j_* in config
              file → hardcoded fallback (no fallback for password).

    NEO4J_TEST_* env vars are deliberately NOT consulted: those belong to
    test fixtures (testcontainers / CI service container) and must never
    influence production code paths. When tests need this helper to point
    at a test Neo4j, conftest.py exports both NEO4J_TEST_* and NEO4J_*.
    """
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
        raise RuntimeError(
            "Neo4j password missing. Set NEO4J_PASSWORD env var OR "
            "neo4j_password in [database] section of odoo-semantic.conf."
        )
    return uri, user, password


def open_production_neo4j():
    """Open a Neo4j driver using config / env vars."""
    uri, user, password = _neo4j_creds()
    # Write/indexer-path driver: filter expected INFORMATION notifications
    # server-side (e.g. IndexOrConstraintAlreadyExists from IF NOT EXISTS index
    # setup). Scoped to the write path only — the MCP READ driver keeps
    # INFORMATION hints (see src/indexer/writer_neo4j.py for the rationale).
    return GraphDatabase.driver(
        uri,
        auth=(user, password),
        notifications_min_severity=NotificationMinimumSeverity.WARNING,
    )


def production_pg_dsn() -> str:
    """The PostgreSQL DSN of the production indexer (PG_DSN env, then config)."""
    dsn = config.from_env_or_ini(
        "PG_DSN", "database", "pg_dsn", fallback=None,
    )
    if not dsn:
        raise RuntimeError(
            "PostgreSQL DSN missing. Set PG_DSN env var OR pg_dsn "
            "in [database] section of odoo-semantic.conf."
        )
    return dsn


def open_production_pg():
    """Open a psycopg2 connection + initialize centralized pool."""
    import psycopg2  # lazy import - not available in all envs at module load time

    from src.db.pg import get_pool, init_pool
    dsn = production_pg_dsn()
    try:
        get_pool()
    except RuntimeError:
        init_pool(dsn, min_conn=1, max_conn=5)
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    return conn


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------
# NOTE (B6 split): ``_owning_profiles`` and ``_index_repo`` were moved verbatim
# to ``src/indexer/pipeline_repo.py`` and are re-exported at the BOTTOM of this
# module (see "Facade re-exports"). ``index_profile`` calls ``_index_repo``
# through that re-export.


def _presence_store():
    """The lifecycle ledger store on the shared pool, or None without a pool.

    Resolved through this module by ``_index_repo`` (patchable in tests). No
    pool means no ledger: the repo is indexed but its lifecycle is not observed
    and nothing can be retired.
    """
    from src.db.exceptions import PoolNotInitializedError
    from src.db.module_presence import ModulePresenceStore
    from src.db.pg import get_pool
    try:
        return ModulePresenceStore(get_pool())
    except PoolNotInitializedError:
        _logger.warning(
            "lifecycle ledger unavailable (PostgreSQL pool not initialized): module "
            "lifecycle not observed, nothing retired"
        )
        return None


def _profiles_for_run(repos: list[dict], profile_name: str) -> list[str]:
    """Return the deduped OWNING-profile array for the framework TestHelper seeding.

    Mirrors per-repo ADR-0034 provenance (``_index_repo`` stamps each node with
    ``_owning_profiles(repo, profile_name, repo_root_name)``). Framework helpers are
    cross-repo (per-version), so we take the union of every repo's owner. Falls back
    to ``[profile_name]`` if no repo yields an owner. NEVER returns ``[]`` (the choke
    denies profile=[] nodes to all scoped tenants).
    """
    owners: list[str] = []
    seen: set[str] = set()
    for repo in repos:
        try:
            repo_root_name = Path(repo.get("local_path", "")).name
            for owner in _owning_profiles(repo, profile_name, repo_root_name):
                if owner and owner not in seen:
                    owners.append(owner)
                    seen.add(owner)
        except Exception:  # noqa: BLE001 — defensive; never break seeding on a bad repo row
            continue
    return owners or [profile_name]


def reconcile_test_surface(
    writer: IndexWriterProtocol,
    versions: list[str],
    *,
    framework_profiles: list[str],
) -> None:
    """Version-wide post-pass that builds ALL test-surface EDGES + helpers (WI-1, C1).

    The TestClass/TestMethod/JsTestSuite NODES are written per-repo in _index_repo,
    but the EDGES (INHERITS_TEST, COVERS_*) and is_helper promotion can only be built
    VERSION-WIDE after all repos of a version are written (an extender test file in
    repo A may subclass a helper in repo B; an INHERITS_TEST edge needs both nodes
    present). Mirrors reconcile_same_name_inherits. Order matters:
      1. seed framework TestHelper nodes (so INHERITS_TEST resolves framework bases
         like TransactionCase),
      2. prune stale framework TestHelper nodes (a class the CURRENT era no longer
         declares, e.g. SavepointCase once a version moves onto the v17+ era —
         issue #362 WI-4/WI-5; must run right after seeding so reconcile_test_inherits
         below never resolves an addon TestClass onto a class this era no longer has),
      3. reconcile_test_inherits (derives INHERITS_TEST edges from the declared
         bases, dependency-aware, and deletes the ones no longer derived),
      4. finalize_is_helper (counts inbound INHERITS_TEST edges - AFTER inherits),
      5. reconcile_test_coverage (COVERS_* edges to is_definition nodes; the
         ones the current TestMethod refs no longer derive are deleted).
    All passes are idempotent (derived-set reconciliation / prune-by-name-set)
    and non-fatal on error.

    ADR-0034 provenance: framework TestHelper nodes are stamped with the OWNING
    profile array of the run (mirrors every other node from the checkout). When the
    shared CE/base profile is indexed, these helpers carry the shared profile ->
    visible to ALL tenants through the choke (TransactionCase etc. are public Odoo
    source, not tenant data). A non-empty profile[] is REQUIRED: the choke denies
    profile=[] nodes to every scoped tenant (size(profile)>0 guard), so seeding with
    [] would make test_base_classes return nothing for non-admin keys.

    This function has no source root available (it runs after all repos of a
    profile are indexed, version-wide, not per-repo checkout) — ``live_names`` is
    therefore always computed from ``framework_bases(rv)`` with NO
    ``odoo_source_root`` argument. This is by design, not a missing feature: the
    curated table is built to answer "which classes exist at this version"
    correctly with no checkout at all (see ``framework_bases()`` in
    ``framework_bases.py``), and ``live_names`` must stay a pure function of the
    version alone (never of a source root or profile) so this profile's prune can
    never delete a node another profile's run still needs.

    Extracted as a module-level function so the pipeline-level e2e test can drive the
    EXACT production wiring (red-before-green: this would build zero edges on the old
    orphaned-reconcile state because nothing called it).
    """
    from src.indexer.framework_bases import framework_bases
    from src.indexer.parser_odoo_core import seed_framework_test_helpers
    for rv in versions:
        writer.write_framework_test_helpers(
            seed_framework_test_helpers(rv),
            profiles=framework_profiles,
        )
        live_names = {fact.name for fact in framework_bases(rv)}
        pruned = writer.prune_framework_test_helpers(rv, live_names)
        if pruned:
            _logger.info(
                "reconcile_test_surface: pruned %d stale '@framework' TestHelper "
                "node(s) for version %s (no longer in the current era menu)",
                pruned, rv,
            )
        writer.reconcile_test_inherits(rv)
        writer.finalize_is_helper(rv)
        writer.reconcile_test_coverage(rv)


def _empty_lifecycle() -> dict:
    return {
        "versions": [],
        "gates_tripped": [],
        "undecidable": [],
        "errors": [],
        "deferred_presence": {},
        "reports": [],
        # Registered repos with no checkout this run could create (#383 item 2):
        # "repo id=<id> (<url>): <cause>". Exit 3, no traceback.
        "unavailable_repos": [],
        # Pre-scan fetches (refresh_before_scan): how many repos were fetched,
        # the ones that failed ("repo id=<id> (<url>): <cause>", a WARNING
        # each), and the ones whose fetch also failed in the previous run
        # (``stale_repos``: exit 3 - OSM no longer follows that remote branch).
        "fetch_attempted": 0,
        "fetch_failed": [],
        "stale_repos": [],
        # Chunks sent to the embedder, the vectors it returned, and the chunks
        # written with their stored vector instead (unchanged text, no embed).
        "embed_attempted": 0,
        "embed_ok": 0,
        "embed_reused": 0,
        # Chunks sent for modules re-written for another reason than a retry
        # (changed source, full run, self-heal, ...): see _quiet_night_shape.
        "embed_attempted_fresh": 0,
        # The embedder answered the end-of-run probe (_probe_silent_embedder)
        # of a quiet-night run: it is reachable, so it refused the few retried
        # chunks for their content (each module is retried, needs_rewrite).
        "embedder_answered": False,
        # Cross-repo propagations this run could not record: the repo keeps its
        # head_sha and the next run repeats them (WARNING + lifecycle_attention,
        # no exit 3); ``stale_propagation``: the same repo's propagation was not
        # recorded in the previous run either (exit 3).
        "propagation_unrecorded": [],
        "stale_propagation": [],
        # Run-wide failures derived by _finish_lifecycle (exit 3): every fetch
        # of the run failed, or the embedder returned nothing all run.
        "outages": [],
        "needs_attention": False,
    }


def _report_unavailable_repo(lc: dict, repo: dict, exc) -> None:
    """Report a repo without a checkout (``RepoCheckoutUnavailable``) - never a
    traceback: one WARNING line, ``repos.status`` = error, the cause in
    ``repos.lifecycle_attention`` and in the run summary (``unavailable_repos``,
    which makes the CLI exit 3 so ``OnFailure=`` mails the admins: OSM cannot
    reflect a repo it cannot read). The other repos are indexed as usual; the
    next run retries the clone. A busy lock (a clone is running right now) is
    only logged."""
    repo_id = repo.get("id")
    if exc.busy:
        _logger.info(
            "Repo id=%s (%s) not indexed this run: %s", repo_id, repo.get("url"), exc.cause,
        )
        return
    _logger.warning(
        "Repo id=%s (%s) unavailable: %s; the next run retries", repo_id,
        repo.get("url"), exc.cause,
    )
    lc["unavailable_repos"].append(f"repo id={repo_id} ({repo.get('url')}): {exc.cause}")
    try:
        repo_store().update_repo_status(repo_id, "error", error_msg=str(exc)[:500])
    except Exception:  # noqa: BLE001 - reporting never fails the run
        _logger.warning("could not record the status of repo id=%s", repo_id, exc_info=True)
    store = _presence_store()
    if store is not None and repo_id is not None:
        try:
            store.set_lifecycle_attention(
                repo_id, f"checkout unavailable: {exc.cause}; the next index run retries",
            )
        except Exception:  # noqa: BLE001
            _logger.warning(
                "could not write lifecycle_attention of repo id=%s", repo_id, exc_info=True,
            )


def _absorb_repo_lifecycle(lc: dict, repo: dict, counters: dict) -> None:
    """Fold one repo's ``_index_repo`` lifecycle counters into the run summary."""
    fetch = counters.get("fetch") or {}
    if fetch.get("attempted"):
        lc["fetch_attempted"] += 1
    if fetch.get("failed"):
        item = f"repo id={repo['id']} ({repo.get('url')}): {fetch['failed']}"
        lc["fetch_failed"].append(item)
        if fetch.get("persistent"):
            lc["stale_repos"].append(item)
    lc["embed_attempted"] += counters.get("embed_attempted", 0) or 0
    lc["embed_ok"] += counters.get("embed_ok", 0) or 0
    lc["embed_reused"] += counters.get("embed_reused", 0) or 0
    # Conservative default: counters without the split count every sent chunk
    # as fresh, so they can never exonerate a silent embedder.
    lc["embed_attempted_fresh"] += counters.get(
        "embed_attempted_fresh", counters.get("embed_attempted", 0),
    ) or 0
    propagation = counters.get("propagation") or {}
    if propagation.get("unrecorded"):
        item = f"repo id={repo['id']} ({repo.get('url')}): {propagation['unrecorded']}"
        lc["propagation_unrecorded"].append(item)
        if propagation.get("persistent"):
            lc["stale_propagation"].append(item)
    repo_lc = counters.get("lifecycle")
    if not repo_lc:
        return
    version = repo_lc.get("odoo_version")
    if version and version not in lc["versions"]:
        lc["versions"].append(version)
    for gate in repo_lc.get("gates_tripped") or []:
        lc["gates_tripped"].append(f"repo id={repo['id']} ({repo.get('url')}): {gate}")
    head = repo_lc.get("presence_deferred_head")
    if head and version:
        lc["deferred_presence"][repo["id"]] = {"head": head, "odoo_version": version}


def _absorb_report(lc: dict, report) -> None:
    """Fold one ReconcileReport into the run summary."""
    v = report.odoo_version
    lc["reports"].append(report.as_dict())
    lc["gates_tripped"].extend(f"{v}: {g}" for g in report.gates_tripped)
    lc["undecidable"].extend(f"{v}: {n}" for n in sorted(report.undecidable))
    lc["errors"].extend(f"{v}: {k}: {e}" for k, e in sorted(report.errors.items()))


# A run-wide fetch outage needs at least this many fetched repos: with one
# repo, "every fetch failed" is one failure, which the two-consecutive-runs
# rule (``stale_repos``) covers instead.
FETCH_OUTAGE_MIN_REPOS = 2


def run_outages(lc: dict) -> list[str]:
    """Run-wide failures that stop OSM reflecting code (exit 3): the pre-scan
    fetch failed for every repo the run fetched (network / DNS outage, a
    revoked credential shared by all repos), or the embedder returned no
    vector for any chunk the run sent (down for the whole run). Derived from
    the counts, so a merged summary (``index_all``) is judged as a whole.

    One exception, the quiet night (:func:`_quiet_night_shape`): unchanged
    chunks are not sent (vector reuse), so a run may send only the few chunks
    a healthy embedder keeps refusing (modules re-written as an
    incomplete-embedding retry); "0 of 2 returned" would then raise this
    alarm every night. Such a run is not an outage when the embedder answered
    the end-of-run probe. Any other "sent N, got 0" stays ``embedder_down``
    whatever a probe says: an embedder that fails every batch all night (504
    model-swap thrash, a busy GPU) can still answer one short text, and the
    changed code it never embedded must not pass silently."""
    out: list[str] = []
    attempted = lc.get("fetch_attempted", 0) or 0
    failed = len(lc.get("fetch_failed") or [])
    if attempted >= FETCH_OUTAGE_MIN_REPOS and failed >= attempted:
        out.append(
            f"fetch_outage: the pre-scan fetch failed for all {attempted} repos this run "
            "fetched (network or DNS outage, or a credential they share); OSM indexed "
            "the on-disk state and does not follow the remote branches"
        )
    sent = lc.get("embed_attempted", 0) or 0
    exonerated = _quiet_night_shape(lc) and bool(lc.get("embedder_answered"))
    if sent and not (lc.get("embed_ok", 0) or 0) and not exonerated:
        out.append(
            f"embedder_down: the embedder returned no vector for any of the {sent} "
            "chunk(s) this run sent; the graph was written, the embeddings were not "
            "(existing rows kept)"
        )
    return out


# A short text the end-of-run probe embeds (any text works: only "did the
# embedder return a vector" is read).
EMBEDDER_PROBE_TEXT = "def ping(self):\n    return True"

# The most chunks a quiet-night run may send and still be exonerated by the
# probe: one embed request's worth (EMBEDDER_MAX_BATCH). A retry night sends
# the few chunks refused the night before; more than one batch of them all
# failing is no longer "one chunk the embedder dislikes" but a failing
# embedder, and is reported as such.
QUIET_NIGHT_MAX_CHUNKS = EMBEDDER_MAX_BATCH


def _quiet_night_shape(lc: dict) -> bool:
    """True when every chunk the run sent belongs to a module re-written only
    as a retry (``embed_attempted_fresh`` 0: no changed source, no full run,
    no self-heal sent anything) and they fit in one batch
    (:data:`QUIET_NIGHT_MAX_CHUNKS`). The only shape in which "sent N, got 0"
    may come from content a healthy embedder refuses."""
    sent = lc.get("embed_attempted") or 0
    fresh = lc.get("embed_attempted_fresh", sent) or 0
    return 0 < sent <= QUIET_NIGHT_MAX_CHUNKS and fresh == 0


def _probe_silent_embedder(lc: dict, embedder) -> None:
    """Ask the embedder for one vector when a quiet-night run got none.

    Only for the :func:`_quiet_night_shape` (a few retried chunks, all
    refused): sets ``lc["embedder_answered"]`` when the embedder returns a
    vector, so :func:`run_outages` raises no ``embedder_down`` for chunks the
    per-module retry (``needs_rewrite``, ``lifecycle_attention``) already
    reports. Any other run that got no vector is not probed: it is an outage
    whatever one short text would get. A failed probe leaves the flag unset.

    The probe goes through the embedder's short query client when it has one
    (``embed_short``), not the 1200s batch read timeout. With the defaults an
    embedder that accepts the connection and never answers - the realistic
    hung case - costs 3 attempts x ``EMBEDDER_TIMEOUT_READ_QUERY`` 30s plus
    the 2s and 4s backoff (``EMBEDDER_RETRY_BACKOFF_BASE``): about 96s. The
    upper bound, every attempt also spending its full connect
    (``EMBEDDER_TIMEOUT_CONNECT`` 10s) and write (``EMBEDDER_TIMEOUT_WRITE``
    30s) timeouts, is 3 x 70 + 6 = 216s (about 3.6 min). Runs once per run:
    ``index_profile`` when it ends the run, ``index_all`` for the merged run
    (it calls ``index_profile`` with ``reconcile=False``)."""
    if embedder is None or lc.get("embedder_answered"):
        return
    if (lc.get("embed_ok") or 0) or not _quiet_night_shape(lc):
        return
    short = getattr(embedder, "embed_short", None)
    try:
        vecs = (
            short([EMBEDDER_PROBE_TEXT]) if callable(short)
            else embedder.embed([EMBEDDER_PROBE_TEXT])
        )
    except Exception as exc:  # noqa: BLE001 - a failed probe is the outage itself
        _logger.warning(
            "embedder probe failed (%s): the embedder returned no vector this run", exc,
        )
        return
    if vecs:
        lc["embedder_answered"] = True
        _logger.warning(
            "embedder answered the end-of-run probe although it returned no vector for "
            "the %d retried chunk(s) this run sent: it refused them, it is not down "
            "(the modules stay flagged needs_rewrite and are retried)",
            lc.get("embed_attempted") or 0,
        )


def _finish_lifecycle(lc: dict) -> dict:
    lc["versions"] = sorted(set(lc["versions"]))
    lc["outages"] = run_outages(lc)
    lc["needs_attention"] = bool(
        lc["gates_tripped"] or lc["undecidable"] or lc["errors"]
        or lc.get("unavailable_repos") or lc.get("stale_repos") or lc["outages"]
        or lc.get("stale_propagation")
    )
    return lc


_POST_PASS_TOKEN: str | None = None
# The code that derives the post-pass edges (writers, derivation rules,
# curated framework bases): any change to it re-runs the post-pass once.
_POST_PASS_SOURCES = (
    "pipeline.py", "writer_neo4j.py", "writer_neo4j_ui.py", "writer_neo4j_orm.py",
    "framework_bases.py", "parser_odoo_core.py",
)


def _post_pass_token() -> str:
    """Code identity the version-wide post-pass state is stamped with.

    A digest of the modules that derive the post-pass edges, so a deploy that
    changes the derivation re-runs the post-pass on its first run even when
    the package metadata version was not rebuilt.
    """
    global _POST_PASS_TOKEN
    if _POST_PASS_TOKEN is None:
        digest = hashlib.sha256()
        here = Path(__file__).resolve().parent
        for name in _POST_PASS_SOURCES:
            try:
                digest.update((here / name).read_bytes())
            except OSError:
                digest.update(name.encode())
        _POST_PASS_TOKEN = f"src-{digest.hexdigest()[:16]}"
    return _POST_PASS_TOKEN


def post_pass_versions(writer: IndexWriterProtocol, versions) -> tuple[list[str], object]:
    """``(versions whose post-pass must run, start instant)`` (E2E-D4).

    The version-wide post-pass (same-name INHERITS, OWL edges, test surface)
    derives edges from the graph alone. It is needed at a version unless the
    version's ``PostPassState`` is clean and stamped with this code
    (``writer.post_pass_current``): every repo run that is not the unchanged
    skip marks its version dirty BEFORE writing, and so do the lifecycle
    deletes / re-owns and ``index-core``. A writer that cannot tell (no such
    method, or any answer but True) runs the post-pass - never skip on doubt.
    The start instant (Neo4j clock) is taken before the check, for
    :func:`record_post_pass`.
    """
    token = _post_pass_token()
    started = writer.server_now() if callable(getattr(writer, "server_now", None)) else None
    current = getattr(writer, "post_pass_current", None)
    needed: list[str] = []
    for v in sorted(set(versions)):
        try:
            clean = callable(current) and current(v, token) is True
        except Exception:  # noqa: BLE001 - the state is an optimization only
            _logger.warning("post-pass state of %s unreadable; running the post-pass", v,
                            exc_info=True)
            clean = False
        if clean:
            _logger.info(
                "post-pass %s: skipped (nothing written or deleted at this version since "
                "its last post-pass)", v,
            )
        else:
            needed.append(v)
    return needed, started


def record_post_pass(writer: IndexWriterProtocol, versions, started) -> None:
    """Stamp each version's post-pass clean; never fatal (a failure only means
    the next run re-runs it)."""
    record = getattr(writer, "record_post_pass", None)
    if not callable(record) or started is None:
        return
    token = _post_pass_token()
    for v in versions:
        try:
            record(v, token, started=started)
        except Exception:  # noqa: BLE001
            _logger.warning("post-pass state of %s not recorded", v, exc_info=True)


def run_lifecycle_reconcile(
    writer: IndexWriterProtocol,
    versions,
    *,
    run_started_at,
    lifecycle: dict,
    retire: bool = True,
    allow_mass_retire: bool = False,
    global_gc: bool | None = None,
    conn=None,
) -> None:
    """Run ``reconcile_version`` for every version, folding reports into *lifecycle*.

    The orphan sweep and version-wide GCs run for a version only when a repo
    at it was scanned this run (``lifecycle['versions']``) or under
    ``allow_mass_retire``. A version whose reconcile cannot run (ledger lock
    timeout, ledger down) is recorded in ``lifecycle['errors']`` - it never
    aborts the other versions or the run, and it makes the CLI exit 3.
    *conn*: the caller's own autocommit connection, used to hold the ledger lock
    instead of a pool connection (ignored when it is not autocommit).
    """
    from src.indexer.reconcile import reconcile_version

    if _presence_store() is None:
        return
    deferred = lifecycle.get("deferred_presence", {})
    scanned = set(lifecycle.get("versions") or [])
    for version in sorted(set(versions)):
        advance = {
            rid: d["head"] for rid, d in deferred.items() if d["odoo_version"] == version
        }
        try:
            report = reconcile_version(
                version,
                writer=writer,
                run_started_at=run_started_at,
                retire=retire,
                allow_mass_retire=allow_mass_retire,
                advance_presence=advance,
                global_gc=global_gc,
                sweep=allow_mass_retire or version in scanned,
                conn=conn if getattr(conn, "autocommit", None) is True else None,
            )
        except Exception as exc:  # noqa: BLE001 - one version never aborts the run
            _logger.exception("lifecycle reconcile failed for version %s", version)
            lifecycle["errors"].append(f"{version}: reconcile: {type(exc).__name__}: {exc}")
            continue
        _absorb_report(lifecycle, report)


def index_profile(
    pg_conn,
    *,
    profile_name: str,
    embedder=None,
    progress: bool = False,
    max_workers: int = 1,
    full_reindex: bool = False,
    gc: bool = False,
    refresh: bool = True,
    retire: bool = True,
    allow_mass_retire: bool = False,
    reconcile: bool = True,
    run_started_at=None,
    embed_reuse: bool = True,
) -> dict:
    """Index all repos belonging to *profile_name*.

    Args:
        pg_conn:       psycopg2 connection (autocommit OK).
        profile_name:  Name of the profile to index.
        embedder:      Optional EmbedderClient. When provided (and pgvector is
                       available), semantic embeddings are written to PostgreSQL.
        progress:      When True, show tqdm progress bar for module iteration.
        max_workers:   Number of parallel threads for repo scanning. Default 1
                       (sequential, unchanged behaviour). When > 1, repos are
                       indexed concurrently via ThreadPoolExecutor. Each thread
                       opens its own psycopg2 connection (psycopg2 connections
                       are NOT thread-safe). Neo4jWriter is shared across threads
                       (safe: every method uses a per-call session).
        full_reindex:  When True, bypass incremental skip-unchanged + diff filter
                       and re-parse every module of every repo. Module
                       retirement does NOT need it (it runs on every run).
        gc:            Deprecated, ignored: module retirement, the orphan sweep
                       and the version-wide GCs run on every run (ADR-0056).
        refresh:       When True (default), each repo is `git fetch`ed and
                       `reset --hard origin/<branch>` BEFORE the incremental
                       check, so upstream merges become visible to the nightly
                       cron. Fail-safe: a fetch error is logged and indexing
                       proceeds on the on-disk state. Set False (CLI --no-fetch)
                       to keep the old local-only behaviour.
        retire:        False (CLI --no-retire) scans and writes but deletes
                       nothing; no longer shipped modules stay pending.
        allow_mass_retire: emergency bypass: also delete what the git evidence
                       cannot prove gone (never set in a timer; ADR-0057 phase 2).
        reconcile:     run the per-version lifecycle reconcile, and the
                       embedder probe (``_probe_silent_embedder``), at the end
                       of the profile (default). ``index_all`` passes False and
                       runs both once, after every profile worker joined.
        run_started_at: start of the enclosing run on the Neo4j clock; defaults
                       to the start of this profile run. Nodes written after it
                       are never deleted by the reconcile.
        embed_reuse:   False (CLI --no-embed-reuse) embeds every chunk of the
                       written modules again instead of reusing the stored
                       vector of an unchanged chunk text
                       (``writer_pgvector.write_module_embeddings``).

    Returns:
        Summary dict: {modules, views, qweb, embeddings, embeddings_reused
        (rows written with their stored vector, no embed call), js_patches,
        owl_comps, lifecycle}; a profile with no repo registered returns it with
        ``no_repos`` True and nothing indexed. ``lifecycle`` = {versions,
        gates_tripped, undecidable, errors, deferred_presence, reports,
        unavailable_repos, needs_attention}; ``needs_attention``
        True means a gate tripped, a name was undecidable, the reconcile
        failed or a repo has no checkout the run could clone (CLI exit code 3).

    A repo without a checkout is cloned first when its clone never succeeded
    (``pipeline_repo._ensure_repo_checkout``); one that still has none is
    reported in ``unavailable_repos`` (WARNING line, ``lifecycle_attention``,
    no traceback) and does not make the profile fail.

    Raises :class:`IndexRunError` after the post-passes (and the reconcile)
    when any repo failed to index; its ``summary`` is the dict above for the
    repos that did index.
    """
    repos = repo_store().get_repos_for_profile(profile_name)
    if not repos:
        _logger.warning("index_profile: no repos found for profile %r", profile_name)
        return {
            "no_repos": True,
            "modules": 0,
            "views": 0,
            "qweb": 0,
            "embeddings": 0,
            "embeddings_reused": 0,
            "js_patches": 0,
            "owl_comps": 0,
            "lifecycle": _finish_lifecycle(_empty_lifecycle()),
        }

    # Build the ancestor profile name list SOLELY for the
    # "ancestor has no indexed repos" warning below. Per ADR-0034 single-owner
    # provenance, nodes are NO LONGER stamped with the ancestor chain (that is a
    # READ-time scope concern resolved at the choke) — _index_repo stamps only the
    # owning profile (F5: the dead `ancestor_profiles` param was removed from
    # _index_repo's signature). This list never reaches a node's `profile[]`.
    ancestor_profiles = repo_store().get_ancestor_profile_names(profile_name)
    if not ancestor_profiles:
        # get_ancestor_profile_names returns [] when profile not found — should
        # not happen since get_repos_for_profile succeeded, but be defensive.
        ancestor_profiles = [profile_name]

    # Warn when any ancestor profile has no indexed repos — do NOT auto-recurse.
    for anc_name in ancestor_profiles[1:]:  # skip self (index 0)
        anc_repos = repo_store().get_repos_for_profile(anc_name)
        if not any(r.get("status") == "indexed" for r in anc_repos):
            _logger.warning(
                "index_profile: ancestor profile %r has no indexed repos — "
                "query for %r may miss inherited nodes until ancestor is indexed",
                anc_name,
                profile_name,
            )

    # Resolve core_rng_root once for the entire profile (WI-E rework).
    # Scan repos to find the first one whose local_path contains the Odoo RNG
    # directory.  This covers addon-only repos that need the core's RNG for
    # version-exact RelaxNG validation without each repo re-scanning for it.
    # None → validation gracefully skipped (no false positives) if no repo
    # in this profile is an Odoo core checkout.
    core_rng_root: Path | None = None
    for _rng_repo in repos:
        _lp = Path(_rng_repo.get("local_path", ""))
        for _candidate in (
            _lp / "odoo" / "addons" / "base" / "rng",
            _lp / "openerp" / "addons" / "base" / "rng",
        ):
            if _candidate.is_dir():
                core_rng_root = _candidate
                break
        if core_rng_root is not None:
            break
    if core_rng_root is None:
        _logger.debug(
            "index_profile %r: no Odoo core RNG dir found — "
            "RelaxNG validation will be skipped for all repos in this profile",
            profile_name,
        )

    with _indexer_lock(pg_conn, profile_name):
        uri, user, password = _neo4j_creds()
        writer = Neo4jWriter(uri, user, password)

        try:
            writer.setup_indexes()
            # One run token for every repo of this profile run (ADR-0056 B14):
            # the entity prune deletes a re-parsed module's children without it.
            if run_started_at is None:
                run_started_at = writer.server_now()
            writer.begin_run(started_at=run_started_at)
            lifecycle = _empty_lifecycle()

            total_modules = 0
            total_views = 0
            total_qweb = 0
            total_embeddings = 0
            total_js_patches = 0
            total_owl_comps = 0
            # A failed repo no longer skips the post-passes: the lifecycle
            # reconcile decides per name (a failed repo only blocks the names
            # it may still ship, review H5); the failure is raised at the end.
            deferred_failure: str | None = None

            if max_workers <= 1:
                # --- Sequential path (original behaviour, unchanged) ----------
                failed_repos: list[tuple[int, str]] = []
                for repo in repos:
                    raise_if_stop_requested()
                    repo_id: int = repo["id"]
                    _t0 = time.monotonic()
                    try:
                        counters = _index_repo(
                            repo, writer, pg_conn=pg_conn, embedder=embedder,
                            progress=progress, full_reindex=full_reindex,
                            profile_name=profile_name,
                            core_rng_root=core_rng_root,
                            refresh=refresh,
                            retire=retire,
                            allow_mass_retire=allow_mass_retire,
                            embed_reuse=embed_reuse,
                        )
                        _absorb_repo_lifecycle(lifecycle, repo, counters)
                        _elapsed = time.monotonic() - _t0
                        total_modules += counters["modules"]
                        total_views += counters["views"]
                        total_qweb += counters["qweb"]
                        total_embeddings += counters.get("embeddings", 0)
                        total_js_patches += counters.get("js_patches", 0)
                        total_owl_comps += counters.get("owl_comps", 0)
                        repo_store().update_repo_status(repo_id, "indexed")
                        _logger.info(
                            "Indexed repo id=%d in %.1fs: %d modules, %d views, %d qweb, "
                            "%d embeddings, %d js_patches, %d owl_comps",
                            repo_id, _elapsed,
                            counters["modules"],
                            counters["views"],
                            counters["qweb"],
                            counters.get("embeddings", 0),
                            counters.get("js_patches", 0),
                            counters.get("owl_comps", 0),
                        )
                    except RepoCheckoutUnavailable as e:
                        _report_unavailable_repo(lifecycle, repo, e)
                    except Exception as e:
                        _elapsed = time.monotonic() - _t0
                        _logger.exception(
                            "Failed to index repo id=%d after %.1fs — continuing",
                            repo_id, _elapsed,
                        )
                        repo_store().update_repo_status(repo_id, "error", error_msg=str(e)[:500])
                        failed_repos.append((repo_id, str(e)[:200]))

                if failed_repos:
                    summary = "; ".join(f"id={rid}: {msg}" for rid, msg in failed_repos)
                    deferred_failure = f"{len(failed_repos)} repo(s) failed: {summary}"
            else:
                # --- Parallel path (ThreadPoolExecutor) ----------------------
                if progress:
                    print(
                        f"[index_profile] progress bar disabled when max_workers={max_workers} "
                        f"(parallel mode — tqdm bars would interleave)"
                    )

                def _worker(repo: dict) -> dict:
                    """Per-repo worker: own pg_conn + shared writer."""
                    raise_if_stop_requested()
                    repo_id: int = repo["id"]
                    pg_conn_local = open_production_pg()
                    _t0 = time.monotonic()
                    try:
                        counters = _index_repo(
                            repo, writer,
                            pg_conn=pg_conn_local,
                            embedder=embedder,
                            progress=False,
                            full_reindex=full_reindex,
                            profile_name=profile_name,
                            core_rng_root=core_rng_root,
                            refresh=refresh,
                            retire=retire,
                            allow_mass_retire=allow_mass_retire,
                            embed_reuse=embed_reuse,
                        )
                        _elapsed = time.monotonic() - _t0
                        repo_store().update_repo_status(repo_id, "indexed")
                        _logger.info(
                            "Indexed repo id=%d in %.1fs: %d modules, %d views, %d qweb, "
                            "%d embeddings, %d js_patches, %d owl_comps",
                            repo_id, _elapsed,
                            counters["modules"],
                            counters["views"],
                            counters["qweb"],
                            counters.get("embeddings", 0),
                            counters.get("js_patches", 0),
                            counters.get("owl_comps", 0),
                        )
                        return counters
                    except RepoCheckoutUnavailable:
                        raise  # reported by the collecting loop, no traceback
                    except Exception as e:
                        # A stopped run (SIGTERM) closes the shared writer
                        # under the running repos: not a repo failure.
                        raise_if_stop_requested()
                        _elapsed = time.monotonic() - _t0
                        _logger.exception(
                            "Failed to index repo id=%d after %.1fs — continuing",
                            repo_id, _elapsed,
                        )
                        try:
                            repo_store().update_repo_status(
                                repo_id, "error", error_msg=str(e)[:500]
                            )
                        except Exception:
                            pass
                        raise
                    finally:
                        pg_conn_local.close()

                # A SIGTERM must not wait for the running repos (stop.py).
                with stoppable_executor(max_workers) as executor:
                    futures = {executor.submit(_worker, repo): repo for repo in repos}
                    first_exc: BaseException | None = None
                    failed_repo_ids: list[tuple[int, str]] = []
                    for future in concurrent.futures.as_completed(futures):
                        repo_for_future = futures[future]
                        try:
                            counters = future.result()
                            _absorb_repo_lifecycle(lifecycle, repo_for_future, counters)
                            total_modules += counters["modules"]
                            total_views += counters["views"]
                            total_qweb += counters["qweb"]
                            total_embeddings += counters.get("embeddings", 0)
                            total_js_patches += counters.get("js_patches", 0)
                            total_owl_comps += counters.get("owl_comps", 0)
                        except RepoCheckoutUnavailable as e:
                            _report_unavailable_repo(lifecycle, repo_for_future, e)
                        except Exception as e:
                            failed_repo_ids.append((repo_for_future["id"], str(e)[:200]))
                            if first_exc is None:
                                first_exc = e
                    if first_exc is not None:
                        summary = "; ".join(
                            f"id={rid}: {msg}" for rid, msg in failed_repo_ids
                        )
                        deferred_failure = (
                            f"{len(failed_repo_ids)} repo(s) failed: {summary}"
                        )

            # === Post-pass INHERITS reconciliation (PERF: once per version, not per repo) ===
            # Fill extender-to-definition INHERITS edges missed due to cross-repo write-order
            # gaps (when an extender repo is indexed before its definition repo).  Running
            # ONCE per version here - after ALL repos of that version are written - avoids
            # R redundant full :Model label scans that would occur if called per-repo (the
            # function scans ALL :Model nodes for the version, which cannot use the composite
            # (name, odoo_version) index without a name anchor).  Calling R times per profile
            # run is pure waste; the gap it fills only materialises after the last repo writes.
            #
            # Concurrent same-version reconciles from --profile-workers can cause MERGE
            # deadlocks; warn-and-continue policy in the writer catches them but leaves a
            # silent gap.  To resolve: re-run index_profile, or accept the miss (next full
            # reindex fills it).  See IndexWriterProtocol.reconcile_same_name_inherits docstring.
            _indexed_versions: set[str] = {r["odoo_version"] for r in repos}
            # E2E-D4: the post-pass derives edges from the graph alone, so a
            # version nothing wrote or deleted since its last post-pass (with
            # this code) is skipped (post_pass_versions).
            _post_versions, _post_started = post_pass_versions(
                writer, _indexed_versions | set(lifecycle["versions"]),
            )
            for _rv in _post_versions:
                writer.reconcile_same_name_inherits(_rv)
                # OWLComp EXTENDS / BOUND_TO: the parent component or the bound
                # model may be written by any repo of the version.
                writer.reconcile_owl_edges(_rv)
            # === End post-pass reconciliation ===

            # === Post-pass test-surface reconciliation (WI-1, C1 wiring) ===
            # Extracted into reconcile_test_surface() so the pipeline-level e2e test
            # exercises the SAME production code path (not a copy) - red-before-green.
            reconcile_test_surface(
                writer,
                _post_versions,
                framework_profiles=_profiles_for_run(repos, profile_name),
            )
            record_post_pass(writer, _post_versions, _post_started)
            # === End test-surface reconciliation ===

            # === Lifecycle reconcile (ADR-0056): the only place modules retire ===
            if reconcile:
                run_lifecycle_reconcile(
                    writer,
                    set(lifecycle["versions"]) | _indexed_versions,
                    run_started_at=run_started_at,
                    lifecycle=lifecycle,
                    retire=retire,
                    allow_mass_retire=allow_mass_retire,
                    conn=pg_conn,
                )
            # === End lifecycle reconcile ===

            # Auto-reseed pattern catalogue (W2-7).
            # Hash-gated via _SeedMeta sentinel (W2-6) - cheap when patterns.json unchanged.
            # Per --no-embed semantic: if embedder is None, pattern embedding is also skipped.
            try:
                from src.indexer.seed_patterns import run as _seed_patterns_run

                seed_summary = _seed_patterns_run(
                    writer=writer,
                    embedder=embedder,
                    force=False,
                )
                if not seed_summary["skipped"]:
                    _logger.info(
                        "Auto-reseed: %d patterns + %d embeddings%s",
                        seed_summary["patterns"], seed_summary["embeddings"],
                        " (embedder=None — skipping pattern embeddings)"
                        if embedder is None else "",
                    )
                else:
                    _logger.info("Auto-reseed: patterns unchanged — skipping")
            except Exception as _seed_exc:
                _logger.warning("Auto-reseed pattern catalogue failed: %s", _seed_exc)

        finally:
            writer.close()

        if reconcile:
            # End of the run (index_all probes once for the merged run).
            _probe_silent_embedder(lifecycle, embedder)
        summary = {
            "modules": total_modules,
            "views": total_views,
            "qweb": total_qweb,
            "embeddings": total_embeddings,
            "embeddings_reused": lifecycle["embed_reused"],
            "js_patches": total_js_patches,
            "owl_comps": total_owl_comps,
            "lifecycle": _finish_lifecycle(lifecycle),
        }
        if deferred_failure is not None:
            raise IndexRunError(deferred_failure, summary)
        return summary


# ---------------------------------------------------------------------------
# Spec layer (M4.5 WI-F1): index Odoo core API symbols + lint + CLI
# ---------------------------------------------------------------------------

def _find_previous_indexed_version(
    current_version: str, writer: IndexWriterProtocol,
) -> str | None:
    """Return the latest indexed CoreSymbol version strictly less than current_version.

    Used to compute lifecycle diff (added/removed/deprecated_in properties).
    Returns None when the current_version is the first indexed version.

    Version comparison is numeric (per project convention — avoids "9.0" > "17.0").
    """
    try:
        current_major, current_minor = (int(p) for p in current_version.split(".")[:2])
    except (ValueError, AttributeError):
        return None

    with writer.driver.session() as session:
        rows = session.run(
            "MATCH (cs:CoreSymbol) RETURN DISTINCT cs.odoo_version AS v"
        ).data()

    versions = [r["v"] for r in rows if r["v"] != current_version]
    if not versions:
        return None

    def _ver_key(v: str) -> tuple[int, int]:
        try:
            parts = v.split(".")
            return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            return (0, 0)

    cur_key = (current_major, current_minor)
    candidates = [v for v in versions if _ver_key(v) < cur_key]
    if not candidates:
        return None
    return max(candidates, key=_ver_key)


def _read_spec_curate_status(
    file_prefix: str, odoo_version: str, static_data_dir: str | None,
) -> str:
    """Read `_curate_status` from a static spec JSON file, defaulting to 'pending'.

    File pattern: `<static_data_dir>/<file_prefix>_<odoo_version>.json`.
    If file missing or field absent → returns 'pending' (safe default).
    """
    import json
    from pathlib import Path as _Path

    # Import here to avoid circular imports; mirrors _load_static_* pattern.
    if static_data_dir:
        spec_dir = _Path(static_data_dir)
    else:
        spec_dir = _Path(__file__).parent / "spec_data"
    spec_path = spec_dir / f"{file_prefix}_{odoo_version}.json"
    if not spec_path.is_file():
        return "pending"
    try:
        data = json.loads(spec_path.read_text(encoding="utf-8"))
        return data.get("_curate_status", "pending")
    except (OSError, json.JSONDecodeError):
        return "pending"


def index_core(
    source_root: str,
    odoo_version: str,
    writer: IndexWriterProtocol,
    *,
    static_data_dir: str | None = None,
) -> dict:
    """Index Odoo core API symbols + lint rules + CLI commands/flags for one version.

    This is the implementation backing the `index-core` CLI subcommand. It:
    1. Parses CoreSymbol from the `_CORE_FILES` allow-list in `source_root`.
    2. Parses LintRule from pylint-odoo/ESLint/ruff + static placeholders.
    3. Parses CLICommand from `odoo/cli/*.py`.
    4. Parses CLIFlag from `odoo/tools/config.py` + static placeholders.
    5. Computes lifecycle diff vs previous indexed version → writes
       added_in/removed_in/deprecated_in properties on CoreSymbol nodes.

    Args:
        source_root:     Path to Odoo upstream checkout root.
        odoo_version:    Version label, e.g. "17.0".
        writer:          Open Neo4jWriter instance.
        static_data_dir: Override directory for static spec_data JSON files.
                         Defaults to `src/indexer/spec_data/`.

    Returns:
        Summary dict: {core_symbols, lint_rules, cli_commands, cli_flags}.
    """
    from src.indexer.diff_engine import compute_diff
    from src.indexer.framework_bases import framework_bases
    from src.indexer.parser_cli import parse_cli_commands, parse_cli_flags
    from src.indexer.parser_lint_rules import parse_lint_rules_for_version
    from src.indexer.parser_odoo_core import parse_odoo_core, seed_framework_test_helpers
    from src.indexer.parser_tools_symbols import load_tools_symbols

    _logger.info("index_core: version=%s source_root=%s", odoo_version, source_root)

    # 1. CoreSymbol (parsed from source) + curated odoo.tools.* symbols merged in.
    # Tool symbols are merged BEFORE write_core_symbols and compute_diff so they
    # participate fully in lifecycle tracking (added_in/removed_in/deprecated_in).
    # fetch_core_symbols() reads from Neo4j, so prior-run tool symbols are already
    # included in old_symbols automatically — no extra step needed.
    #
    # Dedup: parsed symbols take precedence over curated tool_symbols when their
    # qualified_name collides.  The Neo4j MERGE is last-write-wins on the composite
    # key (qualified_name, odoo_version), so placing tool_symbols AFTER parsed ones
    # would let a curated entry clobber a real parsed node (e.g. safe_eval which is
    # both parsed from odoo/tools/safe_eval.py AND listed in tools_symbols_*.json).
    # We filter tool_symbols to exclude any name already produced by parse_odoo_core
    # so the parsed node always wins — and the curated metadata (note, signature) is
    # intentionally dropped for symbols where source-truth already exists.
    #
    # issue #364 C3: plain string-equality on qualified_name misses a whole CLASS
    # of collision. Curated tools_symbols_*.json always uses the flat re-export
    # name `odoo.tools.<bare_name>` (parser_tools_symbols.py / tools_symbol.schema.json
    # — "Must start with 'odoo.tools'", never era- or submodule-qualified). A symbol
    # parsed from an `odoo/tools/<submodule>.py` file in _CORE_FILES (e.g.
    # `odoo/tools/sql.py`) instead carries the real, submodule-qualified path
    # (`odoo.tools.sql.SQL`) because parse_odoo_core's module_qname is derived
    # straight from the file's own relpath (`_extract_from_source`,
    # parser_odoo_core.py:552). `"odoo.tools.SQL" not in {"odoo.tools.sql.SQL", ...}`
    # is therefore always True, so the curated entry was never actually deduped —
    # BOTH nodes get written for v17-v19 (the first version SQL is both parsed and
    # curated), and lookup_core_api("SQL", ...) deterministically preferred the
    # thinner curated node (spec.py ranks an EXACT qualified_name match ahead of a
    # suffix match). Fixed at the class level, not just for SQL: every parsed
    # symbol whose qualified_name matches `odoo.tools.<submodule>.<bare_name>` also
    # gets indexed under its flattened `odoo.tools.<bare_name>` alias for dedup
    # purposes only (never written under that alias — only the real, submodule-
    # qualified parsed node is written), so ANY future curated flat entry that
    # collides with a submodule-qualified parsed symbol under odoo/tools/ is caught
    # by construction, not just the one instance found in current data.
    symbols = parse_odoo_core(source_root, odoo_version)
    tool_symbols = load_tools_symbols(odoo_version, static_data_dir=static_data_dir)
    parsed_qnames: set[str] = {s.qualified_name for s in symbols}
    flattened_tools_aliases: set[str] = {
        f"odoo.tools.{m.group(1)}"
        for qname in parsed_qnames
        if (m := re.match(r"^odoo\.tools\.[^.]+\.([^.]+)$", qname))
    }
    dedup_qnames = parsed_qnames | flattened_tools_aliases
    deduped_tool_symbols = [s for s in tool_symbols if s.qualified_name not in dedup_qnames]
    symbols = symbols + deduped_tool_symbols
    writer.write_core_symbols(symbols)
    _logger.info(
        "index_core: wrote %d CoreSymbol nodes (%d from odoo.tools curation, %d skipped as parsed)",
        len(symbols), len(deduped_tool_symbols), len(tool_symbols) - len(deduped_tool_symbols),
    )

    # 2. LintRule
    rules = parse_lint_rules_for_version(
        odoo_version,
        odoo_source_root=source_root,
        static_data_dir=static_data_dir,
    )
    writer.write_lint_rules(rules)
    _logger.info("index_core: wrote %d LintRule nodes", len(rules))
    # Prune-on-full-write (#364): index_core is the SOLE caller and always writes
    # the FULL LintRule set for this version, so a rule_id removed upstream (e.g.
    # #364 dropped W8140 from v14-v19) must be DETACH DELETEd - write_lint_rules
    # is MERGE-only and never deletes. Empty-guard + soft-drop gate live in the
    # writer. CoreSymbol is deliberately NOT pruned (lifecycle history). See ADR-0055.
    lint_pruned = writer.prune_lint_rules(odoo_version, {r.rule_id for r in rules})
    if lint_pruned:
        _logger.info(
            "index_core: pruned %d stale LintRule node(s) for version %s",
            lint_pruned, odoo_version,
        )
    lint_curate_status = _read_spec_curate_status(
        "lint_rules", odoo_version, static_data_dir,
    )
    writer.write_spec_metadata(
        kind="lint", odoo_version=odoo_version, curate_status=lint_curate_status,
    )

    # 3. CLICommand
    commands = parse_cli_commands(source_root, odoo_version, static_data_dir=static_data_dir)
    writer.write_cli_commands(commands)
    _logger.info("index_core: wrote %d CLICommand nodes", len(commands))
    # Prune-on-full-write (#364): same rationale as LintRule - full set per
    # version, MERGE-only writer, so removed commands must be DETACH DELETEd.
    cmd_pruned = writer.prune_cli_commands(odoo_version, {c.name for c in commands})
    if cmd_pruned:
        _logger.info(
            "index_core: pruned %d stale CLICommand node(s) for version %s",
            cmd_pruned, odoo_version,
        )

    # 4. CLIFlag
    flags = parse_cli_flags(source_root, odoo_version, static_data_dir=static_data_dir)
    writer.write_cli_flags(flags)
    _logger.info("index_core: wrote %d CLIFlag nodes", len(flags))
    # Prune-on-full-write (#364): CLIFlag identity is (flag_name, command_name,
    # odoo_version) - the same flag_name can exist under different commands, so
    # the live key is the joined "flag_name|command_name". command_name is never
    # null in the graph (Neo4j MERGE forbids a null key; parse_cli_flags defaults
    # it to "server"), so `or ''` here is defensive only. See prune_cli_flags.
    flag_live_keys = {f"{f.flag_name}|{f.command_name or ''}" for f in flags}
    flag_pruned = writer.prune_cli_flags(odoo_version, flag_live_keys)
    if flag_pruned:
        _logger.info(
            "index_core: pruned %d stale CLIFlag node(s) for version %s",
            flag_pruned, odoo_version,
        )

    # 4b. Framework TestHelper seeding (WI-1, C1 wiring; prune WI-4/WI-5): seed the
    # built-in Odoo test base classes (TransactionCase, HttpCase, ...) as TestHelper
    # nodes with module='@framework' so INHERITS_TEST edges from addon test classes
    # can resolve to them. Seeded here (the core path) AND in index_profile (so a
    # profile-only run that does not call index_core is still complete). MERGE-
    # idempotent. Unlike reconcile_test_surface, THIS path has a real checkout
    # (source_root), so it is passed through to seed_framework_test_helpers -
    # the seeded nodes are parse-enriched with real file_path/line/has_setUpClass
    # (framework_bases.py's composition rules).
    #
    # TENANT-VISIBILITY ORDERING NOTE: this call seeds with the default
    # profiles=[] (index_core has no profile concept - it is the version-wide,
    # standalone core path). Per ADR-0034, a node whose profile[] is empty is
    # denied to every SCOPED tenant at the read-side choke (size(profile)>0
    # guard) - only an admin (own=None) query sees it. These parse-enriched
    # nodes stay tenant-invisible until a PROFILE run (index_profile ->
    # reconcile_test_surface) unions a real profile array into the SAME
    # TestHelper nodes via MERGE (name/module/odoo_version composite key -
    # profile-less core-seeded and profile-stamped profile-seeded runs land on
    # one node, not two). The deploy runbook therefore orders the PROFILE pass
    # BEFORE the core pass (or re-runs the profile pass after core) - running
    # index-core alone leaves test_base_classes empty for every non-admin key
    # even though the index-core run itself reports success.
    framework_helpers = seed_framework_test_helpers(odoo_version, source_root)
    writer.write_framework_test_helpers(framework_helpers)
    # The next profile run must re-run its post-pass (it unions the profile
    # array into these helpers and re-derives INHERITS_TEST), skip night or not.
    writer.mark_post_pass_dirty(odoo_version)
    _logger.info(
        "index_core: seeded %d framework TestHelper nodes (@framework)",
        len(framework_helpers),
    )
    live_names = {fact.name for fact in framework_bases(odoo_version)}
    pruned = writer.prune_framework_test_helpers(odoo_version, live_names)
    if pruned:
        _logger.info(
            "index_core: pruned %d stale '@framework' TestHelper node(s) for "
            "version %s (no longer in the current era menu)",
            pruned, odoo_version,
        )
    cli_curate_status = _read_spec_curate_status(
        "cli_flags", odoo_version, static_data_dir,
    )
    writer.write_spec_metadata(
        kind="cli", odoo_version=odoo_version, curate_status=cli_curate_status,
    )

    # 5. Lifecycle diff vs previous indexed version
    previous_version = _find_previous_indexed_version(odoo_version, writer)
    if previous_version:
        _logger.info(
            "index_core: computing lifecycle diff %s → %s",
            previous_version, odoo_version,
        )
        # fetch_core_symbols is a convenience method we add to Neo4jWriter
        old_symbols = writer.fetch_core_symbols(previous_version)
        diff = compute_diff(old_symbols, symbols)
        writer.write_diff_edges(diff, from_version=previous_version, to_version=odoo_version)
        # Write lifecycle properties (WI-F2 extension: added_in/removed_in/deprecated_in)
        writer.write_lifecycle_properties(
            diff, from_version=previous_version, to_version=odoo_version,
        )
        _logger.info(
            "index_core: diff — +%d added, -%d removed, ~%d deprecated, %d replaced",
            len(diff.added), len(diff.removed),
            len(getattr(diff, "deprecated", [])),
            len(diff.replaced),
        )

    return {
        "core_symbols": len(symbols),
        "lint_rules": len(rules),
        "cli_commands": len(commands),
        "cli_flags": len(flags),
    }


def index_all(
    pg_conn,
    embedder=None,
    progress: bool = False,
    max_workers: int = 1,
    *,
    full_reindex: bool = False,
    profile_workers: int = 1,
    gc: bool = False,
    refresh: bool = True,
    retire: bool = True,
    allow_mass_retire: bool = False,
    embed_reuse: bool = True,
) -> dict:
    """Index every profile registered in PostgreSQL.

    Continues after per-profile failures - failed profiles are listed in
    the summary under 'profiles_failed'. The healthy repos of a failed
    profile still take part in the lifecycle reconcile (their presence heads
    and tripped gates are absorbed from its :class:`IndexRunError`). With
    ``profile_workers > 1`` the summary is carried by an :class:`IndexRunError`
    raised after the reconcile instead of being returned.

    Args:
        pg_conn:         psycopg2 connection (autocommit OK).
        embedder:        Optional EmbedderClient for pgvector embeddings.
        progress:        When True, show tqdm progress bar for module iteration.
                         Automatically disabled per-profile when profile_workers > 1
                         (tqdm bars would interleave).
        max_workers:     Passed through to index_profile() for intra-profile
                         parallel repo scanning.
        full_reindex:    When True, bypass incremental skip-unchanged + diff filter
                         (W2-4). Forwarded to each index_profile() call.
        profile_workers: Number of profiles to index in parallel. Default 1
                         (sequential, unchanged behaviour). When > 1, profiles
                         are indexed concurrently via ThreadPoolExecutor. Each
                         worker opens its own psycopg2 connection (psycopg2
                         connections are NOT thread-safe). Per-profile advisory
                         lock (Wave 1 P1) ensures no collision across workers.
        gc:              Deprecated, ignored (see index_profile).
        refresh:         When True (default), `git fetch` + `reset --hard
                         origin/<branch>` each repo before the incremental check
                         so upstream merges are visible to the nightly cron.
                         Fail-safe (fetch error -> index on-disk state). Set False
                         (CLI --no-fetch) for the old local-only behaviour.
                         Forwarded to each index_profile() call.
        retire / allow_mass_retire / embed_reuse: see index_profile.

    Module lifecycle: each profile run observes its repos (ledger + pending
    names) with its own reconcile turned off; after every profile worker
    joined, ``reconcile_version`` runs ONCE per version (retirement, orphan
    sweep, version-wide GCs including the dep-stub GC).

    Returns aggregate summary: {profiles_ok, profiles_failed, profiles_empty,
    modules, views, qweb, embeddings, js_patches, owl_comps, lifecycle}
    (``lifecycle`` as in index_profile, merged across profiles plus the
    post-pass reports). ``profiles_empty`` (sorted names) are the profiles
    with no repo registered (e.g. seeded roots): nothing was indexed for
    them, so they are neither ok nor failed.
    """
    profiles = repo_store().list_profiles()
    lifecycle = _empty_lifecycle()
    uri, user, password = _neo4j_creds()
    _clock_writer = Neo4jWriter(uri, user, password)
    try:
        run_started_at = _clock_writer.server_now()
    finally:
        _clock_writer.close()

    counts = dict.fromkeys(
        ("modules", "views", "qweb", "embeddings", "embeddings_reused", "js_patches",
         "owl_comps"), 0,
    )
    profiles_ok = 0
    profiles_failed: list[str] = []
    profiles_empty: list[str] = []
    failures: list[tuple[str, Exception]] = []

    def _absorb_profile(summary: dict) -> None:
        plc = summary.get("lifecycle") or {}
        lifecycle["versions"].extend(plc.get("versions") or [])
        lifecycle["gates_tripped"].extend(plc.get("gates_tripped") or [])
        lifecycle["deferred_presence"].update(plc.get("deferred_presence") or {})
        lifecycle["unavailable_repos"].extend(plc.get("unavailable_repos") or [])
        for key in ("fetch_failed", "stale_repos"):
            lifecycle[key].extend(plc.get(key) or [])
        for key in ("fetch_attempted", "embed_attempted", "embed_ok", "embed_reused"):
            lifecycle[key] += plc.get(key, 0) or 0
        lifecycle["embed_attempted_fresh"] += plc.get(
            "embed_attempted_fresh", plc.get("embed_attempted", 0),
        ) or 0
        for key in ("propagation_unrecorded", "stale_propagation"):
            lifecycle[key].extend(plc.get(key) or [])
        lifecycle["embedder_answered"] = bool(
            lifecycle["embedder_answered"] or plc.get("embedder_answered")
        )
        for key in counts:
            counts[key] += summary.get(key, 0)

    def _absorb_failure(name: str, exc: Exception) -> None:
        # The healthy repos of a profile with a failed repo were indexed:
        # their presence heads, gates and counters still reach the reconcile.
        profiles_failed.append(name)
        failures.append((name, exc))
        if isinstance(exc, IndexRunError):
            _absorb_profile(exc.summary)

    if profile_workers <= 1:
        # --- Sequential path (original behaviour) ----------------------------
        for profile in profiles:
            name = profile["name"]
            try:
                summary = index_profile(
                    pg_conn,
                    profile_name=name,
                    embedder=embedder,
                    progress=progress,
                    max_workers=max_workers,
                    full_reindex=full_reindex,
                    refresh=refresh,
                    retire=retire,
                    allow_mass_retire=allow_mass_retire,
                    reconcile=False,
                    run_started_at=run_started_at,
                    embed_reuse=embed_reuse,
                )
                _absorb_profile(summary)
                if summary.get("no_repos"):
                    profiles_empty.append(name)
                else:
                    profiles_ok += 1
            except Exception as exc:
                _logger.exception("index_all: profile %r failed — skipping", name)
                _absorb_failure(name, exc)
    else:
        # --- Parallel path (ThreadPoolExecutor across profiles) --------------
        if progress:
            print(
                f"[index_all] progress bar disabled when profile_workers={profile_workers} "
                f"(parallel mode — tqdm bars would interleave)"
            )

        # Pre-create Neo4j indexes once to avoid EquivalentSchemaRuleAlreadyExists
        # race when parallel workers simultaneously call setup_indexes() in their
        # sessions. The CREATE INDEX IF NOT EXISTS guards are not enough to prevent
        # concurrent creation races; pre-running setup_indexes() once is the correct
        # workaround (W1-4, re-applied M7 C1).
        uri, user, password = _neo4j_creds()
        _pre_writer = Neo4jWriter(uri, user, password)
        try:
            _pre_writer.setup_indexes()
        finally:
            _pre_writer.close()

        profile_names = [p["name"] for p in profiles]

        def _run_one_profile(profile_name: str) -> dict:
            """Per-profile worker: own pg_conn, own advisory lock."""
            raise_if_stop_requested()
            pg_conn_thread = open_production_pg()
            try:
                return index_profile(
                    pg_conn_thread,
                    profile_name=profile_name,
                    embedder=embedder,
                    progress=False,  # avoid tqdm collision
                    max_workers=max_workers,
                    full_reindex=full_reindex,
                    refresh=refresh,
                    retire=retire,
                    allow_mass_retire=allow_mass_retire,
                    reconcile=False,
                    run_started_at=run_started_at,
                    embed_reuse=embed_reuse,
                )
            finally:
                # A stopped run keeps it open: it holds the profile lock.
                close_unless_held(pg_conn_thread)

        # A SIGTERM must not wait for the running profiles: a profile is hours
        # of work and an embed request can take many minutes (stop.py).
        with stoppable_executor(profile_workers) as executor:
            future_to_name = {
                executor.submit(_run_one_profile, name): name
                for name in profile_names
            }
            for future in concurrent.futures.as_completed(future_to_name):
                name = future_to_name[future]
                exc = future.exception()
                if exc is not None:
                    _logger.exception(
                        "index_all: profile %r failed — will re-raise after all complete",
                        name,
                        exc_info=exc,
                    )
                    _absorb_failure(name, exc)
                else:
                    summary = future.result()
                    _absorb_profile(summary)
                    if summary.get("no_repos"):
                        profiles_empty.append(name)
                    else:
                        profiles_ok += 1

    # === Post-all-profiles lifecycle reconcile (ADR-0056) ===
    # Runs AFTER every profile worker joined (no MERGE in flight for any
    # version), once per version: retirement of pending names, the orphan
    # sweep, and the version-wide GCs - including the dep-stub GC (FUFU-1),
    # which must run after all profiles so a stub promoted to a real module by
    # a later profile is not deleted first. A failed profile never skips it.
    _all_versions: set[str] = {
        p["odoo_version"] for p in profiles if p.get("odoo_version")
    } | set(lifecycle["versions"])
    if _all_versions:
        _gc_writer = Neo4jWriter(uri, user, password)
        try:
            run_lifecycle_reconcile(
                _gc_writer,
                _all_versions,
                run_started_at=run_started_at,
                lifecycle=lifecycle,
                retire=retire,
                allow_mass_retire=allow_mass_retire,
                global_gc=True,
                conn=pg_conn,
            )
        finally:
            _gc_writer.close()
    # === End post-all-profiles lifecycle reconcile ===

    _probe_silent_embedder(lifecycle, embedder)
    summary = {
        "profiles_ok": profiles_ok,
        "profiles_failed": profiles_failed,
        "profiles_empty": sorted(profiles_empty),
        **counts,
        "lifecycle": _finish_lifecycle(lifecycle),
    }
    if failures and profile_workers > 1:
        detail = "; ".join(f"{name}: {exc}" for name, exc in failures)
        raise IndexRunError(
            f"{len(failures)} profile(s) failed: {detail}", summary,
        ) from failures[0][1]
    return summary


# ---------------------------------------------------------------------------
# Facade re-exports (B6 split)
# ---------------------------------------------------------------------------
# Per-repo stage + reembed/audit helpers live in sibling modules now. Re-export
# them here so the historical import surface (``from src.indexer.pipeline import
# _index_repo, _owning_profiles, reembed_stubs_for_profile,
# audit_repo_for_profile``) and the test patch targets
# (``src.indexer.pipeline._index_repo`` etc.) stay valid.
#
# Imported at the BOTTOM of the module body (not the top) so that the children —
# which resolve ``build_registry`` / ``topological_sort`` / ``repo_store`` /
# ``_neo4j_creds`` back through THIS module at call time via a function-local
# ``from . import pipeline`` — never form a module-load cycle. ``index_profile``
# (above) calls the bare name ``_index_repo``, which by call time resolves to the
# re-exported binding below.
from src.indexer.pipeline_reembed import (  # noqa: E402,F401
    audit_repo_for_profile,
    reembed_stubs_for_profile,
)
from src.indexer.pipeline_repo import (  # noqa: E402,F401
    RepoCheckoutUnavailable,
    _index_repo,
    _owning_profiles,
)

