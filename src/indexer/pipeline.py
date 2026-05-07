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
import logging
import os
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


# ---------------------------------------------------------------------------
# Production connection helpers (consumed by __main__.py)
# ---------------------------------------------------------------------------

def _neo4j_creds() -> tuple[str, str, str]:
    """Return (uri, user, password) — single source of truth for Neo4j connection.

    Priority: NEO4J_TEST_* env (tests) → NEO4J_* env (Docker/CI) →
              [database]/neo4j_* in config file → hardcoded fallback.
    """
    uri = (
        os.getenv("NEO4J_TEST_URI")
        or os.getenv("NEO4J_URI")
        or config.get("database", "neo4j_uri", fallback="bolt://localhost:7687")
    )
    user = (
        os.getenv("NEO4J_TEST_USER")
        or os.getenv("NEO4J_USER")
        or config.get("database", "neo4j_user", fallback="neo4j")
    )
    password = (
        os.getenv("NEO4J_TEST_PASSWORD")
        or os.getenv("NEO4J_PASSWORD")
        or config.get("database", "neo4j_password", fallback="password")
    )
    return uri, user, password


def open_production_neo4j():
    """Open a Neo4j driver using config / env vars."""
    uri, user, password = _neo4j_creds()
    return GraphDatabase.driver(uri, auth=(user, password))


def open_production_pg():
    """Open a psycopg2 connection using config / env vars."""
    import psycopg2  # lazy import — not available in all envs at module load time
    dsn = (
        os.getenv("PG_DSN")
        or config.get(
            "database", "pg_dsn",
            fallback="postgresql://odoo_semantic:password@localhost:5432/odoo_semantic",
        )
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
) -> dict:
    """Index a single repo dict (from get_repos_for_profile).

    Returns per-repo counters: {modules, views, qweb, embeddings}.
    Pass pg_conn + embedder to also write semantic embeddings to pgvector.
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
        for mod_name in sorted_names:
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


def index_profile(pg_conn, *, profile_name: str, embedder=None) -> dict:
    """Index all repos belonging to *profile_name*.

    Args:
        pg_conn:      psycopg2 connection (autocommit OK).
        profile_name: Name of the profile to index.
        embedder:     Optional EmbedderClient. When provided (and pgvector is
                      available), semantic embeddings are written to PostgreSQL.

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
                counters = _index_repo(repo, writer, pg_conn=pg_conn, embedder=embedder)
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


def index_all(pg_conn, embedder=None) -> dict:
    """Index every profile registered in PostgreSQL.

    Continues after per-profile failures — failed profiles are listed in
    the returned summary under 'profiles_failed'.

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
            summary = index_profile(pg_conn, profile_name=name, embedder=embedder)
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
