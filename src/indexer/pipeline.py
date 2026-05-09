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
import hashlib
import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path

from neo4j import GraphDatabase

from src import config
from src.db.repo_registry import (
    get_repos_for_profile,
    list_profiles,
    update_repo_status,
)
from src.indexer import parser_js, parser_python, parser_qweb, parser_xml
from src.indexer.models import ViewParseResult
from src.indexer.registry import build_registry
from src.indexer.resolver import topological_sort
from src.indexer.writer_neo4j import Neo4jWriter

_logger = logging.getLogger(__name__)


_LOCK_ID = int(hashlib.md5(b"odoo-semantic-indexer").hexdigest(), 16) % (2**31)


@contextmanager
def _indexer_lock(pg_conn, profile_name: str):
    """Postgres advisory lock — prevents concurrent indexer runs.

    Auto-releases on process exit/crash (unlike fcntl which is process-local).
    Cross-container safe — lock lives in PostgreSQL, not filesystem.
    """
    with pg_conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_ID,))
        acquired = cur.fetchone()[0]
    if not acquired:
        raise RuntimeError(
            f"Indexer already running for profile {profile_name!r} "
            f"(Postgres advisory lock {_LOCK_ID} held). "
            "Wait for it to finish or restart PostgreSQL to release stale lock."
        )
    try:
        yield
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID,))


def indexer_is_running(pg_conn) -> bool:
    """Non-destructive advisory lock peek — True if the indexer is currently running.

    Acquire-then-release pattern: avoids pg_locks table scan, stays consistent
    with the same _LOCK_ID that _indexer_lock uses. Caller's connection must be
    autocommit (Web UI _get_conn already sets autocommit=True).
    """
    with pg_conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_ID,))
        acquired = cur.fetchone()[0]
        if acquired:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID,))
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
) -> dict:
    """Index a single repo dict (from get_repos_for_profile).

    Returns per-repo counters: {modules, views, qweb, embeddings}.
    Pass pg_conn + embedder to also write semantic embeddings to pgvector.
    Set progress=True to show tqdm progress bar during module iteration.
    """
    local_path: str = repo["local_path"]
    odoo_version: str = repo["odoo_version"]

    if not Path(local_path).is_dir():
        raise FileNotFoundError(f"local_path does not exist: {local_path!r}")

    # build_registry expects list[tuple[repo_path, odoo_version]]
    registry = build_registry([(local_path, odoo_version)])
    # registry: {odoo_version: {module_name: ModuleInfo}}
    modules_by_version = registry  # alias for clarity

    py_results = []
    view_results: list[ViewParseResult] = []
    js_graph_results = []

    total_modules = 0
    total_views = 0
    total_qweb = 0
    total_embeddings = 0
    total_js_patches = 0
    total_owl_comps = 0

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
                write_module_embeddings(pg_conn, mod_name, version, chunks, embedder)
                total_embeddings += len(chunks)

    writer.write_results(py_results)
    writer.write_view_results(view_results)
    writer.write_js_graph_results(js_graph_results)

    return {
        "modules": total_modules,
        "views": total_views,
        "qweb": total_qweb,
        "embeddings": total_embeddings,
        "js_patches": total_js_patches,
        "owl_comps": total_owl_comps,
    }


def index_profile(pg_conn, *, profile_name: str, embedder=None, progress: bool = False) -> dict:
    """Index all repos belonging to *profile_name*.

    Args:
        pg_conn:      psycopg2 connection (autocommit OK).
        profile_name: Name of the profile to index.
        embedder:     Optional EmbedderClient. When provided (and pgvector is
                      available), semantic embeddings are written to PostgreSQL.
        progress:     When True, show tqdm progress bar for module iteration.

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

            for repo in repos:
                repo_id: int = repo["id"]
                try:
                    counters = _index_repo(
                        repo, writer, pg_conn=pg_conn, embedder=embedder, progress=progress
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


def index_all(pg_conn, embedder=None, progress: bool = False) -> dict:
    """Index every profile registered in PostgreSQL.

    Continues after per-profile failures — failed profiles are listed in
    the returned summary under 'profiles_failed'.

    Args:
        pg_conn:   psycopg2 connection (autocommit OK).
        embedder:  Optional EmbedderClient for pgvector embeddings.
        progress:  When True, show tqdm progress bar for module iteration.

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

    for profile in profiles:
        name = profile["name"]
        try:
            summary = index_profile(
                pg_conn, profile_name=name, embedder=embedder, progress=progress
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
