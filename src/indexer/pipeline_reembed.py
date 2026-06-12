# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/pipeline_reembed.py
"""Re-embed-stubs + audit-repo helpers (B6 split from pipeline.py — no behavior change).

Two profile-scoped, read-mostly maintenance operations originally in pipeline.py
(M10 WI-3):

    reembed_stubs_for_profile(pg_conn, *, profile_name, embedder) -> dict
        Re-embed modules that have Neo4j nodes but zero pgvector embeddings.
    audit_repo_for_profile(pg_conn, *, profile_name) -> list[dict]
        Read-only per-module coverage stats (model/field/method/view/embedding).

``pipeline.py`` re-exports both at the bottom of its body so existing call sites
(``src/indexer/__main__.py``) and test imports
(``from src.indexer.pipeline import reembed_stubs_for_profile, audit_repo_for_profile``)
keep working unchanged.

``_neo4j_creds`` lives in ``pipeline.py``; it is resolved through the parent
module at call time via a deferred (cold-import-safe) ``from . import pipeline``.
``repo_store`` / ``GraphDatabase`` / ``Path`` are not test-patched on the
``pipeline`` namespace for these two functions, so they are ordinary imports here.
"""
import logging
from pathlib import Path

from neo4j import GraphDatabase

from src.db.pg import repo_store

# Log under the parent "src.indexer.pipeline" name (NOT __name__) so the
# reembed/audit log lines stay on the SAME logger they were emitted from before
# the B6 split — operators (and tests) that scope log filters to
# "src.indexer.pipeline" keep seeing them.
_logger = logging.getLogger("src.indexer.pipeline")


def reembed_stubs_for_profile(
    pg_conn,
    *,
    profile_name: str,
    embedder,
) -> dict:
    """Re-embed modules that have Neo4j nodes but zero embeddings (catch-up).

    Finds Module nodes in Neo4j for the given profile where field_count > 0 but
    embeddings_count == 0 in pgvector, then re-runs make_chunks +
    write_module_embeddings for each. Idempotent: a second run is a no-op because
    write_module_embeddings uses DELETE+INSERT with ON CONFLICT DO UPDATE.

    Args:
        pg_conn:      psycopg2 connection (autocommit OK). Used to query the
                      embeddings table and call make_chunks/write_module_embeddings.
        profile_name: Name of the profile to scan.
        embedder:     EmbedderClient instance (required - no-op guard would skip
                      the whole point of this command).

    Returns:
        Summary dict: {modules_checked, modules_reembedded, total_embed_calls}.
    """
    from src.db.migrate import _vector_extension_available  # noqa: PLC0415
    from src.indexer import pipeline as _pipeline  # noqa: PLC0415
    from src.indexer.writer_pgvector import make_chunks, write_module_embeddings  # noqa: PLC0415

    if not _vector_extension_available(pg_conn):
        _logger.warning(
            "reembed_stubs_for_profile: pgvector extension not available — skipping"
        )
        return {"modules_checked": 0, "modules_reembedded": 0, "total_embed_calls": 0}

    # Resolve profile repos to get (odoo_version, local_path) pairs.
    repos = repo_store().get_repos_for_profile(profile_name)
    if not repos:
        _logger.warning(
            "reembed_stubs_for_profile: no repos found for profile %r", profile_name
        )
        return {"modules_checked": 0, "modules_reembedded": 0, "total_embed_calls": 0}

    uri, user, password = _pipeline._neo4j_creds()
    driver = GraphDatabase.driver(uri, auth=(user, password))
    total_embed_calls = 0
    modules_checked = 0
    modules_reembedded = 0

    try:
        for repo in repos:
            odoo_version: str = repo["odoo_version"]
            local_path: str = repo["local_path"]

            if not Path(local_path).is_dir():
                _logger.warning(
                    "reembed_stubs_for_profile: local_path %r does not exist — skipping repo",
                    local_path,
                )
                continue

            # Query Neo4j for all Module names in this (repo, odoo_version).
            with driver.session() as session:
                rows = session.run(
                    """
                    MATCH (mod:Module {odoo_version: $v})
                    WHERE $profile_name IN coalesce(mod.profile, [])
                    RETURN mod.name AS module_name, mod.path AS module_path
                    """,
                    v=odoo_version, profile_name=profile_name,
                ).data()

            # Build registry once per repo (not per module) to avoid N+1 rglob scans.
            # ADR-0037 D4: pass repo_url + repo_id so re-embedded css/scss/less
            # chunks keep their repo provenance (mirror the _index_repo path) —
            # without these the ModuleInfo carries repo_id=None and provenance is lost.
            from src.indexer.registry import build_registry  # noqa: PLC0415

            _registry = build_registry(
                [(local_path, odoo_version)],
                repo_url=repo.get("url"),
                repo_id=repo.get("id"),
            )
            # Flatten to {module_name: ModuleInfo} for O(1) lookup in the inner loop.
            _modules_map: dict = {}
            for _ver, _mmap in _registry.items():
                _modules_map.update(_mmap)

            # Batch-fetch embedding counts for all modules in this repo/version.
            mod_names_in_rows: list[str] = [r["module_name"] for r in rows]
            _embed_counts: dict[str, int] = {}
            with pg_conn.cursor() as cur:
                cur.execute(
                    "SELECT module, COUNT(*) FROM embeddings "
                    "WHERE odoo_version = %s AND module = ANY(%s) "
                    "GROUP BY module",
                    (odoo_version, mod_names_in_rows),
                )
                for _mod, _cnt in cur.fetchall():
                    _embed_counts[_mod] = _cnt

            # For each module: check embeddings count in pgvector.
            for row in rows:
                mod_name: str = row["module_name"]
                modules_checked += 1

                embedding_count: int = _embed_counts.get(mod_name, 0)

                if embedding_count > 0:
                    # Already has embeddings — idempotent skip.
                    _logger.debug(
                        "reembed_stubs_for_profile: %s/%s already has %d embeddings — skip",
                        odoo_version, mod_name, embedding_count,
                    )
                    continue

                # Module has zero embeddings — re-embed it.
                if mod_name not in _modules_map:
                    continue
                try:
                    info = _modules_map[mod_name]

                    # Parse all artefacts needed for make_chunks.
                    from src.indexer import (  # noqa: PLC0415
                        parser_css,
                        parser_js,
                        parser_less,
                        parser_python,
                        parser_qweb,
                        parser_scss,
                        parser_xml,
                    )
                    from src.indexer.models import ViewParseResult  # noqa: PLC0415
                    from src.indexer.writer_pgvector import (  # noqa: PLC0415
                        make_css_chunks,
                        make_less_chunks,
                        make_scss_chunks,
                    )

                    py_result = parser_python.parse_module(info)
                    xml_result = parser_xml.parse_module(info)
                    qweb_result = parser_qweb.parse_module(info)
                    merged = ViewParseResult(
                        module=info,
                        views=xml_result.views,
                        qweb=qweb_result.qweb,
                    )
                    js_chunks = parser_js.parse_module(info)
                    css_chunks_mod, _ = parser_css.parse_module(info)
                    scss_chunks_mod, _ = parser_scss.parse_module(info)
                    less_chunks_mod, _ = parser_less.parse_module(info)

                    chunks = make_chunks(mod_name, odoo_version, py_result, merged, js_chunks)
                    # WS-C: pass info so stylesheet chunks carry repo/repo_id +
                    # relative file_path (ADR-0037), consistent with the main path.
                    chunks.extend(make_css_chunks(css_chunks_mod, info))
                    chunks.extend(make_scss_chunks(scss_chunks_mod, info))
                    chunks.extend(make_less_chunks(less_chunks_mod, info))

                    if not chunks:
                        _logger.debug(
                            "reembed_stubs_for_profile: %s/%s produced 0 chunks — skip",
                            odoo_version, mod_name,
                        )
                        continue

                    embed_calls = write_module_embeddings(
                        mod_name, odoo_version, chunks, embedder,
                        profile_name=profile_name,
                    )
                    total_embed_calls += embed_calls
                    modules_reembedded += 1
                    _logger.info(
                        "reembed_stubs_for_profile: re-embedded %s/%s "
                        "(%d chunks, %d embed calls)",
                        odoo_version, mod_name, len(chunks), embed_calls,
                    )
                except Exception:
                    _logger.exception(
                        "reembed_stubs_for_profile: failed to re-embed %s/%s — skipping",
                        odoo_version, mod_name,
                    )
    finally:
        driver.close()

    _logger.info(
        "reembed_stubs_for_profile: checked %d modules, re-embedded %d, %d embed calls total",
        modules_checked, modules_reembedded, total_embed_calls,
    )
    return {
        "modules_checked": modules_checked,
        "modules_reembedded": modules_reembedded,
        "total_embed_calls": total_embed_calls,
    }


def audit_repo_for_profile(
    pg_conn,
    *,
    profile_name: str,
) -> list[dict]:
    """Read-only: export per-module coverage stats for a profile.

    Queries Neo4j for model/field/method/view counts per Module and
    pgvector for embedding row counts. Returns a list of dicts suitable
    for JSON serialisation.

    Args:
        pg_conn:      psycopg2 connection (autocommit OK). Used only for
                      reading the embeddings table — no writes performed.
        profile_name: Name of the profile to audit.

    Returns:
        List of per-module dicts:
            {module, odoo_version, model_count, field_count, method_count,
             view_count, embedding_count}
        Sorted by (odoo_version, module).
    """
    from src.db.migrate import _vector_extension_available  # noqa: PLC0415
    from src.indexer import pipeline as _pipeline  # noqa: PLC0415

    repos = repo_store().get_repos_for_profile(profile_name)
    if not repos:
        _logger.warning(
            "audit_repo_for_profile: no repos found for profile %r", profile_name
        )
        return []

    embed_available = _vector_extension_available(pg_conn)
    if not embed_available:
        _logger.warning(
            "audit_repo_for_profile: pgvector extension not available — "
            "embedding_count will be 0 for all modules"
        )

    uri, user, password = _pipeline._neo4j_creds()
    driver = GraphDatabase.driver(uri, auth=(user, password))
    results: list[dict] = []

    try:
        # Collect all (odoo_version) values for this profile from repos.
        versions_in_profile = list({r["odoo_version"] for r in repos})

        for odoo_version in versions_in_profile:
            with driver.session() as session:
                # Per-module aggregate counts from Neo4j.
                rows = session.run(
                    """
                    MATCH (mod:Module {odoo_version: $v})
                    WHERE $profile_name IN coalesce(mod.profile, [])
                    RETURN
                        mod.name AS module_name,
                        COUNT { (:Model {module: mod.name, odoo_version: $v}) } AS model_count,
                        COUNT { (:Field {module: mod.name, odoo_version: $v}) } AS field_count,
                        COUNT { (:Method {module: mod.name, odoo_version: $v}) } AS method_count,
                        COUNT { (:View {module: mod.name, odoo_version: $v}) }  AS view_count
                    ORDER BY mod.name ASC
                    """,
                    v=odoo_version, profile_name=profile_name,
                ).data()

            # Batch-fetch embedding counts for all modules in this version (avoid N+1).
            _audit_mod_names: list[str] = [r["module_name"] for r in rows]
            _audit_embed_counts: dict[str, int] = {}
            if embed_available and _audit_mod_names:
                with pg_conn.cursor() as cur:
                    cur.execute(
                        "SELECT module, COUNT(*) FROM embeddings "
                        "WHERE odoo_version = %s AND module = ANY(%s) "
                        "GROUP BY module",
                        (odoo_version, _audit_mod_names),
                    )
                    for _mod, _cnt in cur.fetchall():
                        _audit_embed_counts[_mod] = _cnt

            for row in rows:
                mod_name: str = row["module_name"]

                # Embedding count from pgvector (read-only). 0 when not available.
                embedding_count: int = _audit_embed_counts.get(mod_name, 0)

                results.append({
                    "module": mod_name,
                    "odoo_version": odoo_version,
                    "model_count": row["model_count"],
                    "field_count": row["field_count"],
                    "method_count": row["method_count"],
                    "view_count": row["view_count"],
                    "embedding_count": embedding_count,
                })
    finally:
        driver.close()

    results.sort(key=lambda r: (r["odoo_version"], r["module"]))
    return results
