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
import os
import sys
from contextlib import contextmanager
from pathlib import Path

from neo4j import GraphDatabase

from src import config
from src.db import repo_registry as _repo_registry
from src.db.repo_registry import (
    get_repos_for_profile,
    list_profiles,
    update_repo_status,
)
from src.indexer import incremental as _incremental
from src.indexer import parser_js, parser_python, parser_qweb, parser_xml
from src.indexer.models import ViewParseResult
from src.indexer.registry import build_registry
from src.indexer.resolver import topological_sort
from src.indexer.writer_neo4j import Neo4jWriter

_logger = logging.getLogger(__name__)


def _profile_lock_id(profile_name: str) -> int:
    """Hash profile name to a 31-bit advisory lock id."""
    return int(hashlib.md5(f"odoo-semantic-{profile_name}".encode()).hexdigest(), 16) % (2**31)


@contextmanager
def _indexer_lock(pg_conn, profile_name: str):
    """Postgres advisory lock — prevents concurrent indexer runs for a profile.

    Auto-releases on process exit/crash (unlike fcntl which is process-local).
    Cross-container safe — lock lives in PostgreSQL, not filesystem.
    Each profile gets its own lock id, so parallel indexing of different profiles
    is allowed.
    """
    lock_id = _profile_lock_id(profile_name)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,))
        acquired = cur.fetchone()[0]
    if not acquired:
        raise RuntimeError(
            f"Indexer already running for profile {profile_name!r} "
            f"(Postgres advisory lock {lock_id} held). "
            "Wait for it to finish or restart PostgreSQL to release stale lock."
        )
    try:
        yield
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))


def indexer_is_running(pg_conn, profile_name: str) -> bool:
    """Non-destructive advisory lock peek — True if the indexer is currently running
    for the given profile.

    Acquire-then-release pattern: avoids pg_locks table scan, stays consistent
    with the same lock id that _indexer_lock uses. Caller's connection must be
    autocommit (Web UI _get_conn already sets autocommit=True).
    """
    lock_id = _profile_lock_id(profile_name)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,))
        acquired = cur.fetchone()[0]
        if acquired:
            cur.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
    return not acquired


# ---------------------------------------------------------------------------
# Production connection helpers (consumed by __main__.py)
# ---------------------------------------------------------------------------

def _neo4j_creds() -> tuple[str, str, str]:
    """Return (uri, user, password) — single source of truth for Neo4j connection.

    Priority: NEO4J_TEST_* env (tests) → NEO4J_* env (Docker/CI) →
              [database]/neo4j_* in config file → hardcoded fallback (none for password).
    """
    uri = (
        os.getenv("NEO4J_TEST_URI")
        or config.from_env_or_ini(
            "NEO4J_URI", "database", "neo4j_uri",
            fallback="bolt://localhost:7687",
        )
    )
    user = (
        os.getenv("NEO4J_TEST_USER")
        or config.from_env_or_ini(
            "NEO4J_USER", "database", "neo4j_user", fallback="neo4j",
        )
    )
    password = (
        os.getenv("NEO4J_TEST_PASSWORD")
        or config.from_env_or_ini(
            "NEO4J_PASSWORD", "database", "neo4j_password", fallback=None,
        )
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
    """Open a psycopg2 connection using config / env vars."""
    import psycopg2  # lazy import — not available in all envs at module load time
    dsn = config.from_env_or_ini(
        "PG_DSN", "database", "pg_dsn", fallback=None,
    )
    if not dsn:
        raise RuntimeError(
            "PostgreSQL DSN missing. Set PG_DSN env var OR pg_dsn "
            "in [database] section of odoo-semantic.conf."
        )
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    return conn


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

def _index_repo(
    repo: dict,
    writer: Neo4jWriter,
    pg_conn=None,
    embedder=None,
    progress: bool = False,
    full_reindex: bool = False,
    gc: bool = False,
) -> dict:
    """Index a single repo dict (from get_repos_for_profile).

    Returns per-repo counters: {modules, views, qweb, embeddings}.
    Pass pg_conn + embedder to also write semantic embeddings to pgvector.
    Set progress=True to show tqdm progress bar during module iteration.

    Incremental behaviour (M6 W2-4):
    - Compares current git HEAD to repos.head_sha (stored from last run).
    - Equal → zero-cost skip.
    - Force-push detected (stored sha not ancestor of HEAD) → full reindex.
    - Otherwise → diff-filter scan results to changed modules only.
    - head_sha advanced to current HEAD ONLY after all writes succeed.
    - full_reindex=True bypasses the skip + diff filter (use to clean stale nodes).
    """
    local_path: str = repo["local_path"]
    odoo_version: str = repo["odoo_version"]

    if not Path(local_path).is_dir():
        raise FileNotFoundError(f"local_path does not exist: {local_path!r}")

    repo_path = Path(local_path)

    # === Incremental check (W2-4) ===
    current_head = _incremental.get_repo_head(repo_path)
    last_head: str | None = None

    if current_head is None:
        _logger.warning(
            "Cannot determine HEAD for repo %s — full reindex without head_sha tracking",
            repo["url"],
        )

    if not full_reindex and pg_conn is not None:
        last_head = _repo_registry.get_repo_head_sha(pg_conn, repo["id"])

        if current_head and last_head and current_head == last_head:
            _logger.info(
                "Repo %s unchanged (HEAD %s) — skipping reindex",
                repo.get("url", local_path), current_head[:8],
            )
            return {
                "modules": 0,
                "views": 0,
                "qweb": 0,
                "embeddings": 0,
                "js_patches": 0,
                "owl_comps": 0,
            }

        if last_head and current_head and not _incremental.is_ancestor(
            repo_path, last_head, current_head
        ):
            _logger.warning(
                "Repo %s: force-push or history rewrite detected "
                "(stored %s not ancestor of HEAD %s) — falling back to full reindex",
                repo.get("url", local_path),
                last_head[:8],
                current_head[:8],
            )
            last_head = None  # force full reindex below
    elif full_reindex:
        last_head = None  # ensure diff filter is skipped
    # === End incremental check ===

    # build_registry expects list[tuple[repo_path, odoo_version]]
    registry = build_registry([(local_path, odoo_version)])
    # registry: {odoo_version: {module_name: ModuleInfo}}
    modules_by_version = registry  # alias for clarity

    # Collect live_paths (all module paths found on disk) BEFORE incremental filter.
    # GC compares these against Neo4j Module nodes to detect stale (renamed/removed) modules.
    # Must use the FULL scan (not the incremental-filtered subset) so GC sees ALL live dirs.
    live_paths: set[str] = {
        info.path
        for mods in registry.values()
        for info in mods.values()
    }
    # Repo dir name (m.repo in Neo4j) — derived the same way registry.py does it.
    repo_root_name: str = Path(local_path).name

    # === Incremental filter (W2-4) ===
    if last_head and current_head and not full_reindex:
        changed_rel_paths = _incremental.compute_changed_module_paths(
            repo_path, last_head, current_head,
        )
        # convert relative paths to absolute to match ModuleInfo.path
        changed_abs_paths = {str(repo_path / rel) for rel in changed_rel_paths}

        filtered_by_version: dict[str, dict] = {}
        total_before = sum(len(mods) for mods in modules_by_version.values())
        for ver, mods in modules_by_version.items():
            filtered_by_version[ver] = _incremental.filter_modules_by_changed(
                mods, changed_abs_paths,
            )
        total_after = sum(len(mods) for mods in filtered_by_version.values())

        _logger.info(
            "Repo %s: incremental — %d/%d modules changed",
            repo.get("url", local_path), total_after, total_before,
        )

        if total_after == 0:
            _logger.info(
                "Repo %s: no module dirs changed (only meta files) — "
                "head_sha will still be advanced",
                repo.get("url", local_path),
            )
            if current_head and pg_conn is not None:
                _repo_registry.update_repo_head_sha(pg_conn, repo["id"], current_head)
            return {
                "modules": 0,
                "views": 0,
                "qweb": 0,
                "embeddings": 0,
                "js_patches": 0,
                "owl_comps": 0,
            }

        modules_by_version = filtered_by_version
    # === End incremental filter ===

    py_results = []
    view_results: list[ViewParseResult] = []
    js_graph_results = []

    total_modules = 0
    total_views = 0
    total_qweb = 0
    total_embeddings = 0
    total_js_patches = 0
    total_owl_comps = 0
    total_embed_calls = 0

    # Pre-flight: check whether embedding is possible (once, not per module).
    embed_enabled = pg_conn is not None and embedder is not None
    if embed_enabled:
        from src.db.migrate import _vector_extension_available
        embed_enabled = _vector_extension_available(pg_conn)
    if embed_enabled:
        from src.indexer.writer_pgvector import make_chunks, write_module_embeddings

    for version, modules in modules_by_version.items():
        sorted_names = topological_sort(modules)

        # Try to import tqdm for progress bar; graceful fallback if not installed.
        try:
            from tqdm import tqdm as _tqdm
        except ImportError:
            _tqdm = None

        # Wrap iteration with tqdm if progress enabled, tqdm available, and stdout is a TTY.
        iterable = sorted_names
        if progress and _tqdm is not None and sys.stdout.isatty():
            iterable = _tqdm(sorted_names, desc=f"[{version}]", unit="mod", leave=True)

        for mod_name in iterable:
            info = modules[mod_name]
            total_modules += 1

            # Python models
            py_result = parser_python.parse_module(info)
            py_results.append(py_result)

            # XML views (ir.ui.view records)
            xml_result = parser_xml.parse_module(info)
            total_views += len(xml_result.views)

            # QWeb templates
            qweb_result = parser_qweb.parse_module(info)
            total_qweb += len(qweb_result.qweb)

            # Merge both view parsers into one ViewParseResult per module.
            # writer.write_view_results handles both .views and .qweb in one call.
            merged = ViewParseResult(
                module=info,
                views=xml_result.views,
                qweb=qweb_result.qweb,
            )
            view_results.append(merged)

            # JS graph extraction — patches and OWL components
            js_graph = parser_js.parse_module_graph(info)
            js_graph_results.append(js_graph)
            total_js_patches += len(js_graph.patches)
            total_owl_comps += len(js_graph.components)

            # Semantic embeddings — optional, skipped when pg_conn/embedder absent,
            # pgvector extension is not installed, or version could not be resolved.
            if embed_enabled and version != "unknown":
                js_chunks = parser_js.parse_module(info)
                chunks = make_chunks(mod_name, version, py_result, merged, js_chunks)
                embed_calls = write_module_embeddings(
                    pg_conn, mod_name, version, chunks, embedder
                )
                total_embeddings += len(chunks)
                total_embed_calls += embed_calls

    writer.write_results(py_results)
    writer.write_view_results(view_results)
    writer.write_js_graph_results(js_graph_results)

    # === Module GC (M7 C4): delete stale Module nodes after successful writes ===
    # Risk gate: only run when scanner found ≥1 module to avoid data loss when
    # scanner fails silently (e.g. filesystem permission error, empty repo).
    if gc:
        if len(live_paths) >= 1:
            gc_deleted = writer.gc_stale_modules(repo_root_name, odoo_version, live_paths)
            if gc_deleted > 0:
                _logger.info(
                    "Module GC: deleted %d stale Module nodes for repo %s version %s",
                    gc_deleted, repo_root_name, odoo_version,
                )
            else:
                _logger.info(
                    "Module GC: no stale Module nodes found for repo %s version %s",
                    repo_root_name, odoo_version,
                )
        else:
            _logger.warning(
                "Module GC requested but scanner returned 0 modules — "
                "skipping to avoid data loss (repo %s version %s)",
                repo.get("url", local_path), odoo_version,
            )
    # === End Module GC ===

    # Observability summary log (M7 C5) — one line per repo, readable by admins.
    _logger.info(
        "Indexer run: %d modules, %d embed calls, %d rows written",
        total_modules,
        total_embed_calls,
        total_embeddings,
    )

    # === On full success (W2-4): advance head_sha AFTER all writes ===
    # Must be the last statement — any exception above prevents this,
    # preserving last_head so next run retries the same diff (or full reindex).
    if current_head and pg_conn is not None:
        _repo_registry.update_repo_head_sha(pg_conn, repo["id"], current_head)
    # =====================================================================

    # === Cross-repo dep propagation (M7 W14) ===
    # Only on incremental runs (diff-based): collect the changed module names,
    # query Neo4j for modules in OTHER repos that DEPENDS_ON those modules, and
    # NULL their repos.head_sha so they are re-indexed on the next run.
    # Full reindex skips this — it already re-evaluates everything.
    _is_incremental = (
        last_head is not None
        and current_head is not None
        and not full_reindex
    )
    if _is_incremental and pg_conn is not None:
        changed_module_names: set[str] = {
            mod_name
            for mods in modules_by_version.values()
            for mod_name in mods
        }
        if changed_module_names:
            from src.db.repo_registry import (
                get_repo_ids_by_local_path_basenames,
                reset_head_sha,
            )
            from src.indexer.cross_repo import find_dependent_repos
            dep_repo_basenames = find_dependent_repos(
                writer.driver, odoo_version, changed_module_names,
            )
            # Exclude the repo we just indexed (its head_sha was just updated).
            dep_repo_basenames = [b for b in dep_repo_basenames if b != repo_root_name]
            if dep_repo_basenames:
                dep_repo_ids = get_repo_ids_by_local_path_basenames(
                    pg_conn, dep_repo_basenames,
                )
                if dep_repo_ids:
                    n_reset = reset_head_sha(pg_conn, dep_repo_ids)
                    _logger.info(
                        "Cross-repo dep propagation: reset head_sha on %d dependent repo(s) "
                        "(changed modules: %s)",
                        n_reset,
                        ", ".join(sorted(changed_module_names)),
                    )
    # === End cross-repo dep propagation ===

    return {
        "modules": total_modules,
        "views": total_views,
        "qweb": total_qweb,
        "embeddings": total_embeddings,
        "embed_calls": total_embed_calls,
        "js_patches": total_js_patches,
        "owl_comps": total_owl_comps,
    }


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
    repos = get_repos_for_profile(pg_conn, profile_name)
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
                for repo in repos:
                    repo_id: int = repo["id"]
                    try:
                        counters = _index_repo(
                            repo, writer, pg_conn=pg_conn, embedder=embedder,
                            progress=progress, full_reindex=full_reindex, gc=gc,
                        )
                        total_modules += counters["modules"]
                        total_views += counters["views"]
                        total_qweb += counters["qweb"]
                        total_embeddings += counters.get("embeddings", 0)
                        total_js_patches += counters.get("js_patches", 0)
                        total_owl_comps += counters.get("owl_comps", 0)
                        update_repo_status(pg_conn, repo_id, "indexed")
                        _logger.info(
                            "Indexed repo id=%d: %d modules, %d views, %d qweb, "
                            "%d embeddings, %d js_patches, %d owl_comps",
                            repo_id,
                            counters["modules"],
                            counters["views"],
                            counters["qweb"],
                            counters.get("embeddings", 0),
                            counters.get("js_patches", 0),
                            counters.get("owl_comps", 0),
                        )
                    except Exception as e:
                        _logger.exception("Failed to index repo id=%d", repo_id)
                        update_repo_status(pg_conn, repo_id, "error", error_msg=str(e)[:500])
                        raise  # re-raise so index_profile can propagate failure
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
                    try:
                        counters = _index_repo(
                            repo, writer,
                            pg_conn=pg_conn_local,
                            embedder=embedder,
                            progress=False,
                            full_reindex=full_reindex,
                            gc=gc,
                        )
                        update_repo_status(pg_conn_local, repo_id, "indexed")
                        _logger.info(
                            "Indexed repo id=%d: %d modules, %d views, %d qweb, "
                            "%d embeddings, %d js_patches, %d owl_comps",
                            repo_id,
                            counters["modules"],
                            counters["views"],
                            counters["qweb"],
                            counters.get("embeddings", 0),
                            counters.get("js_patches", 0),
                            counters.get("owl_comps", 0),
                        )
                        return counters
                    except Exception as e:
                        _logger.exception("Failed to index repo id=%d", repo_id)
                        try:
                            update_repo_status(
                                pg_conn_local, repo_id, "error", error_msg=str(e)[:500]
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
                    for future in concurrent.futures.as_completed(futures):
                        try:
                            counters = future.result()
                            total_modules += counters["modules"]
                            total_views += counters["views"]
                            total_qweb += counters["qweb"]
                            total_embeddings += counters.get("embeddings", 0)
                            total_js_patches += counters.get("js_patches", 0)
                            total_owl_comps += counters.get("owl_comps", 0)
                        except Exception as e:
                            if first_exc is None:
                                first_exc = e
                    if first_exc is not None:
                        raise first_exc

            # Auto-reseed pattern catalogue (W2-7).
            # Hash-gated via _SeedMeta sentinel (W2-6) — cheap when patterns.json unchanged.
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
    current_version: str, writer: Neo4jWriter,
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
    writer: Neo4jWriter,
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

    _logger.info("index_core: version=%s source_root=%s", odoo_version, source_root)

    # 1. CoreSymbol
    symbols = parse_odoo_core(source_root, odoo_version)
    writer.write_core_symbols(symbols)
    _logger.info("index_core: wrote %d CoreSymbol nodes", len(symbols))

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
    commands = parse_cli_commands(source_root, odoo_version)
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
    profiles = list_profiles(pg_conn)
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
