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
import time
from contextlib import contextmanager
from pathlib import Path

from neo4j import GraphDatabase

from src import config
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
from src.indexer.registry import build_registry  # noqa: F401
from src.indexer.resolver import topological_sort  # noqa: F401
from src.indexer.writer_neo4j import Neo4jWriter

_logger = logging.getLogger(__name__)


def _profile_lock_id(profile_name: str) -> int:
    """Hash profile name to a 31-bit advisory lock id."""
    return int(hashlib.md5(f"odoo-semantic-{profile_name}".encode()).hexdigest(), 16) % (2**31)


def _repo_lock_id(repo_id: int) -> int:
    """Derive a 31-bit Postgres advisory lock id from a repo_id (ADR-0035 D2).

    Uses a different namespace prefix ("osm-repo-") than the profile lock
    ("odoo-semantic-") to guarantee the two key spaces never collide even if
    a profile name were a stringified integer equal to a repo_id.
    """
    return int(hashlib.md5(f"osm-repo-{repo_id}".encode()).hexdigest(), 16) % (2**31)


@contextmanager
def _indexer_lock(pg_conn, profile_name: str):
    """Postgres advisory lock — prevents concurrent indexer runs for a profile.

    Auto-releases on process exit/crash (unlike fcntl which is process-local).
    Cross-container safe — lock lives in PostgreSQL, not filesystem.
    Each profile gets its own lock id, so parallel indexing of different profiles
    is allowed.
    """
    from src.db.pg import advisory_lock
    lock_id = _profile_lock_id(profile_name)
    with advisory_lock(pg_conn, lock_id) as acquired:
        if not acquired:
            raise RuntimeError(
                f"Indexer already running for profile {profile_name!r} "
                f"(Postgres advisory lock {lock_id} held). "
                "Wait for it to finish or restart PostgreSQL to release stale lock."
            )
        yield


@contextmanager
def _repo_git_lock(pg_conn, repo_id: int):
    """Per-repo Postgres advisory lock guarding mutating git ops (ADR-0035 D2).

    Wraps clone/fetch/reset for a single repo so two concurrent workers never
    race on ``.git/index.lock``.  Read-only git ops (rev-parse, diff --name-only)
    must NOT be wrapped — they run lock-free for performance.

    The lock is keyed by ``repo_id`` (not profile) so cross-repo operations
    run fully in parallel.

    Raises ``RuntimeError`` if the lock cannot be acquired (another worker is
    mutating the same repo).
    """
    from src.db.pg import advisory_lock
    lock_id = _repo_lock_id(repo_id)
    with advisory_lock(pg_conn, lock_id) as acquired:
        if not acquired:
            raise RuntimeError(
                f"Git mutation already in progress for repo id={repo_id} "
                f"(Postgres advisory lock {lock_id} held). "
                "Wait for the other worker to finish."
            )
        yield


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
    return GraphDatabase.driver(uri, auth=(user, password))


def open_production_pg():
    """Open a psycopg2 connection + initialize centralized pool."""
    import psycopg2  # lazy import — not available in all envs at module load time

    from src.db.pg import get_pool, init_pool
    dsn = config.from_env_or_ini(
        "PG_DSN", "database", "pg_dsn", fallback=None,
    )
    if not dsn:
        raise RuntimeError(
            "PostgreSQL DSN missing. Set PG_DSN env var OR pg_dsn "
            "in [database] section of odoo-semantic.conf."
        )
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
# through that re-export. The split is structural only — the per-repo logic,
# head_sha advance order (ADR-0007) and provenance stamping are byte-identical.


def index_profile(
    pg_conn,
    *,
    profile_name: str,
    embedder=None,
    progress: bool = False,
    max_workers: int = 1,
    full_reindex: bool = False,
    gc: bool = False,
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
                       and force a full reindex for all repos. Use periodically
                       to clean up stale Neo4j Module nodes from rename/move.
        gc:            When True, after a full scan of each repo, compare Module
                       nodes in Neo4j vs scanner output and DETACH DELETE stale
                       nodes (modules that no longer exist on disk). Risk-gated:
                       only runs when scanner found ≥1 module. Recommended for
                       monthly runs or after module renames. See ADR-0007 §D5.

    Returns:
        Summary dict: {modules, views, qweb, embeddings, js_patches, owl_comps}.
    """
    repos = repo_store().get_repos_for_profile(profile_name)
    if not repos:
        _logger.warning("index_profile: no repos found for profile %r", profile_name)
        return {
            "modules": 0,
            "views": 0,
            "qweb": 0,
            "embeddings": 0,
            "js_patches": 0,
            "owl_comps": 0,
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

            total_modules = 0
            total_views = 0
            total_qweb = 0
            total_embeddings = 0
            total_js_patches = 0
            total_owl_comps = 0

            if max_workers <= 1:
                # --- Sequential path (original behaviour, unchanged) ----------
                failed_repos: list[tuple[int, str]] = []
                for repo in repos:
                    repo_id: int = repo["id"]
                    _t0 = time.monotonic()
                    try:
                        counters = _index_repo(
                            repo, writer, pg_conn=pg_conn, embedder=embedder,
                            progress=progress, full_reindex=full_reindex, gc=gc,
                            profile_name=profile_name,
                            core_rng_root=core_rng_root,
                        )
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
                    raise RuntimeError(f"{len(failed_repos)} repo(s) failed: {summary}")
            else:
                # --- Parallel path (ThreadPoolExecutor) ----------------------
                if progress:
                    print(
                        f"[index_profile] progress bar disabled when max_workers={max_workers} "
                        f"(parallel mode — tqdm bars would interleave)"
                    )

                def _worker(repo: dict) -> dict:
                    """Per-repo worker: own pg_conn + shared writer."""
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
                            gc=gc,
                            profile_name=profile_name,
                            core_rng_root=core_rng_root,
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
                    except Exception as e:
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

                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=max_workers
                ) as executor:
                    futures = {executor.submit(_worker, repo): repo for repo in repos}
                    first_exc: BaseException | None = None
                    failed_repo_ids: list[tuple[int, str]] = []
                    for future in concurrent.futures.as_completed(futures):
                        repo_for_future = futures[future]
                        try:
                            counters = future.result()
                            total_modules += counters["modules"]
                            total_views += counters["views"]
                            total_qweb += counters["qweb"]
                            total_embeddings += counters.get("embeddings", 0)
                            total_js_patches += counters.get("js_patches", 0)
                            total_owl_comps += counters.get("owl_comps", 0)
                        except Exception as e:
                            failed_repo_ids.append((repo_for_future["id"], str(e)[:200]))
                            if first_exc is None:
                                first_exc = e
                    if first_exc is not None:
                        summary = "; ".join(
                            f"id={rid}: {msg}" for rid, msg in failed_repo_ids
                        )
                        raise RuntimeError(
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
            for _rv in sorted(_indexed_versions):
                writer.reconcile_same_name_inherits(_rv)
            # === End post-pass reconciliation ===

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

        return {
            "modules": total_modules,
            "views": total_views,
            "qweb": total_qweb,
            "embeddings": total_embeddings,
            "js_patches": total_js_patches,
            "owl_comps": total_owl_comps,
        }


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
    1. Parses CoreSymbol from the 8 allow-list files in `source_root`.
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
    from src.indexer.parser_cli import parse_cli_commands, parse_cli_flags
    from src.indexer.parser_lint_rules import parse_lint_rules_for_version
    from src.indexer.parser_odoo_core import parse_odoo_core
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
    symbols = parse_odoo_core(source_root, odoo_version)
    tool_symbols = load_tools_symbols(odoo_version, static_data_dir=static_data_dir)
    parsed_qnames: set[str] = {s.qualified_name for s in symbols}
    deduped_tool_symbols = [s for s in tool_symbols if s.qualified_name not in parsed_qnames]
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

    # 4. CLIFlag
    flags = parse_cli_flags(source_root, odoo_version, static_data_dir=static_data_dir)
    writer.write_cli_flags(flags)
    _logger.info("index_core: wrote %d CLIFlag nodes", len(flags))
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
) -> dict:
    """Index every profile registered in PostgreSQL.

    Continues after per-profile failures — failed profiles are listed in
    the returned summary under 'profiles_failed'.

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
        gc:              When True, run Module GC for each repo (see index_profile
                         and ADR-0007 §D5). Forwarded to each index_profile() call.

    Returns aggregate summary: {profiles_ok, profiles_failed, modules, views,
    qweb, embeddings, js_patches, owl_comps}.
    """
    profiles = repo_store().list_profiles()
    agg_modules = 0
    agg_views = 0
    agg_qweb = 0
    agg_embeddings = 0
    agg_js_patches = 0
    agg_owl_comps = 0
    profiles_ok = 0
    profiles_failed: list[str] = []

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
                    gc=gc,
                )
                agg_modules += summary["modules"]
                agg_views += summary["views"]
                agg_qweb += summary["qweb"]
                agg_embeddings += summary.get("embeddings", 0)
                agg_js_patches += summary.get("js_patches", 0)
                agg_owl_comps += summary.get("owl_comps", 0)
                profiles_ok += 1
            except Exception:
                _logger.exception("index_all: profile %r failed — skipping", name)
                profiles_failed.append(name)
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
        first_exc: Exception | None = None

        def _run_one_profile(profile_name: str) -> dict:
            """Per-profile worker: own pg_conn, own advisory lock."""
            pg_conn_thread = open_production_pg()
            try:
                return index_profile(
                    pg_conn_thread,
                    profile_name=profile_name,
                    embedder=embedder,
                    progress=False,  # avoid tqdm collision
                    max_workers=max_workers,
                    full_reindex=full_reindex,
                    gc=gc,
                )
            finally:
                pg_conn_thread.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=profile_workers) as executor:
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
                    profiles_failed.append(name)
                    if first_exc is None:
                        first_exc = exc
                else:
                    summary = future.result()
                    agg_modules += summary["modules"]
                    agg_views += summary["views"]
                    agg_qweb += summary["qweb"]
                    agg_embeddings += summary.get("embeddings", 0)
                    agg_js_patches += summary.get("js_patches", 0)
                    agg_owl_comps += summary.get("owl_comps", 0)
                    profiles_ok += 1

        if first_exc is not None:
            raise first_exc

    # === Global post-all-profiles GC: childless repo_id-NULL dep-stubs (FUFU-1) ===
    # Must run AFTER all profile indexing completes so stubs promoted to real
    # modules in a later-running profile are not deleted before that profile
    # runs.  In parallel mode (profile_workers > 1) this block is outside the
    # ThreadPoolExecutor context so no dep-MERGEs are in-flight here.
    # Gated on the same gc=True flag; runs once per unique odoo_version across
    # all profiles (NOT per-profile — ordering hazard in parallel mode).
    if gc:
        _all_versions: set[str] = {
            p["odoo_version"] for p in profiles if p.get("odoo_version")
        }
        if _all_versions:
            uri, user, password = _neo4j_creds()
            _gc_writer = Neo4jWriter(uri, user, password)
            try:
                for _v in sorted(_all_versions):
                    _n = _gc_writer.gc_null_repo_dep_stubs(_v)
                    if _n > 0:
                        _logger.info(
                            "index_all dep-stub GC [post-all-profiles]: "
                            "deleted %d stubs for version %s",
                            _n, _v,
                        )
            finally:
                _gc_writer.close()
    # === End global dep-stub GC ===

    return {
        "profiles_ok": profiles_ok,
        "profiles_failed": profiles_failed,
        "modules": agg_modules,
        "views": agg_views,
        "qweb": agg_qweb,
        "embeddings": agg_embeddings,
        "js_patches": agg_js_patches,
        "owl_comps": agg_owl_comps,
    }


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
from src.indexer.pipeline_repo import _index_repo, _owning_profiles  # noqa: E402,F401

