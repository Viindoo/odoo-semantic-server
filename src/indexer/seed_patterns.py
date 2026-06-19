# SPDX-License-Identifier: AGPL-3.0-or-later
"""One-shot CLI: load patterns.json → write Neo4j PatternExample nodes + embed pgvector.

Usage:
    python -m src.indexer.seed_patterns                    # all versions, with embed
    python -m src.indexer.seed_patterns --version 17.0     # filter by version_min
    python -m src.indexer.seed_patterns --no-embed         # skip pgvector step

Idempotent — MERGE on pattern_id; embedding rows replaced via DELETE-WHERE-INSERT.
Hash-gated by _SeedMeta sentinel node to skip when patterns.json is unchanged.

Per ADR-0003: PatternExample = Neo4j node (composite key pattern_id) + reuse
`embeddings` table with chunk_type='pattern_example', module='__patterns__'.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from src import config
from src.constants import DEFAULT_EMBEDDER_MODEL, GLOBAL_PROFILE
from src.indexer.models import PatternExample
from src.indexer.writer_neo4j import Neo4jWriter
from src.indexer.writer_pgvector import (
    EmbeddingChunk,
    _embedder_meta,
    make_pattern_chunks,
)

_logger = logging.getLogger("seed_patterns")

# Re-export GLOBAL_PROFILE for callers that import it from here.
# The canonical definition lives in src.constants — do not duplicate it here.
# GLOBAL_PROFILE imported above from src.constants.

# Default patterns file lives next to the package (src/data/patterns.json).
_DEFAULT_PATTERNS_FILE = (
    Path(__file__).resolve().parent.parent / "data" / "patterns.json"
)

# Schema file lives next to patterns.json.
_PATTERNS_SCHEMA_FILE = (
    Path(__file__).resolve().parent.parent / "data" / "patterns.schema.json"
)

@functools.lru_cache(maxsize=1)
def _get_patterns_validator():
    """Return a cached Draft202012Validator for patterns.schema.json.

    lru_cache makes construction thread-safe and lazy — safe under
    --profile-workers parallel indexing (M6 W2-8).
    """
    from jsonschema import Draft202012Validator
    schema = json.loads(_PATTERNS_SCHEMA_FILE.read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def _compute_patterns_sha256(json_path: Path) -> str:
    """SHA-256 hex of patterns.json file bytes (legacy fallback only).

    WI-RV F-D: NOT the canonical SHA for sentinel comparison.  Use
    :func:`compute_patterns_canonical_sha` instead so the SHA tracks the
    actual source-of-truth content (DB rows post-backfill, file bytes
    only as fallback).  Kept here for the deprecation path where Neo4j
    sentinels were stamped with file-bytes SHA before the unification.
    """
    return hashlib.sha256(json_path.read_bytes()).hexdigest()


def _canonical_patterns_json(patterns: list[PatternExample]) -> str:
    """Serialise *patterns* to the canonical JSON form used for SHA.

    The serialisation MUST be deterministic across processes / Python
    versions so two callers with the same DB content compute the same
    SHA:
      * ``sort_keys=True``         — stable key order regardless of dict iteration
      * ``default=str``            — handles any datetime/Decimal escape valves
      * fields enumerated explicitly (NOT ``vars(p)``) so a future field
        addition is an explicit migration, not a silent SHA churn.
    """
    return json.dumps(
        [
            {
                "pattern_id": p.pattern_id,
                "intent_keywords": p.intent_keywords,
                "file_ref": p.file_ref,
                "snippet_text": p.snippet_text,
                "gotchas": p.gotchas,
                "odoo_version_min": p.odoo_version_min,
                # #329 one-time migration: adding odoo_version_max to the canonical
                # JSON DELIBERATELY changes the SHA once, forcing a single reseed so
                # the new field lands in Neo4j (mirrors the WI-RV F-D migration note).
                "odoo_version_max": p.odoo_version_max,
                "language": p.language,
                "core_symbol_names": p.core_symbol_names,
            }
            for p in patterns
        ],
        sort_keys=True,
        default=str,
    )


def compute_patterns_canonical_sha(
    *,
    version_filter: str | None = None,
    patterns_file: Path | None = None,
) -> str:
    """Canonical SHA over the current pattern source-of-truth.

    Resolution order (WI-RV F-D — unifies sentinel SHA across all writers):
      1. DB rows via :func:`_load_patterns_from_db`  — primary, matches the
         shape consumed by Neo4j + pgvector writers.
      2. JSON file via :func:`_load_patterns`        — fallback when the DB
         is unreachable or empty (cold bootstrap).

    The SHA is computed over the canonical JSON form of the loaded
    :class:`PatternExample` list, so the value matches whatever
    :func:`run` writes to Neo4j/pgvector regardless of which source
    produced it.

    Args:
      version_filter: pass-through to :func:`_load_patterns_from_db` and
        :func:`_load_patterns` so the SHA scopes to the same subset that
        will be written.
      patterns_file: explicit JSON file for the fallback path; defaults
        to :data:`_DEFAULT_PATTERNS_FILE`.  Callers that pass a custom
        ``--patterns-file`` (e.g. CLI ``main()``) MUST forward it here so
        the SHA covers the file actually written to the store.

    Before WI-RV F-D, :func:`run` compared file-bytes SHA against the
    DB-content SHA written by :func:`recompute_sentinel_sha` (called from
    the admin patterns CRUD endpoint).  The mismatch caused a perpetual
    reseed every time the indexer cycled — confirmed F-D, score 92.

    Migration: a deployment whose sentinel was stamped with file-bytes
    SHA before the upgrade will reseed exactly once on the first run
    after upgrade, then stabilise on the canonical SHA going forward.
    """
    try:
        db_rows = _load_patterns_from_db(version_filter)
        if db_rows is not None:
            canonical = _canonical_patterns_json(db_rows)
            return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    except Exception as exc:
        _logger.warning(
            "compute_patterns_canonical_sha: DB load failed (%s); "
            "falling back to JSON file",
            exc,
        )

    # Fallback: load from JSON file and compute canonical SHA over the
    # parsed list (NOT raw file bytes — we want byte-for-byte parity with
    # the DB-sourced path so a switch between the two stays SHA-stable).
    fallback_path = patterns_file or _DEFAULT_PATTERNS_FILE
    file_patterns = _load_patterns(fallback_path, version_filter)
    return hashlib.sha256(
        _canonical_patterns_json(file_patterns).encode("utf-8")
    ).hexdigest()


def _get_stored_patterns_sha(driver, key: str = "patterns_neo4j") -> str | None:
    """Return sha256 stored on the _SeedMeta sentinel node for ``key``, or None.

    Reads the split sentinel key (e.g. 'patterns_neo4j' or 'patterns_pgvector').
    Also checks the legacy 'patterns' key as a fallback for the Neo4j sentinel so
    that existing deployments with the old single-key sentinel are still detected
    as "already seeded" until a --force run migrates them to split keys.

    Per ADR-0007 D6-split: sentinel is split into two keys so divergence between
    Neo4j PatternExample nodes and pgvector embeddings is explicitly detectable.
    """
    with driver.session() as session:
        row = session.run(
            "MATCH (s:_SeedMeta {key: $key}) RETURN s.sha256 AS sha LIMIT 1",
            key=key,
        ).single()
        if row:
            return row["sha"]
        # Legacy fallback: old single-key sentinel (key='patterns') is treated as
        # the neo4j sentinel only (pgvector was never written with that key).
        if key == "patterns_neo4j":
            legacy = session.run(
                "MATCH (s:_SeedMeta {key: 'patterns'}) RETURN s.sha256 AS sha LIMIT 1"
            ).single()
            return legacy["sha"] if legacy else None
        return None


def _set_stored_patterns_sha(driver, sha: str, key: str = "patterns_neo4j") -> None:
    """MERGE _SeedMeta sentinel node for ``key`` and set sha256 + updated_at.

    Per ADR-0007 D6-split: use key='patterns_neo4j' after Neo4j write succeeds and
    key='patterns_pgvector' after pgvector write succeeds.  Both must match the
    current sha256 for auto-reseed to skip BOTH stores on the next run.
    """
    with driver.session() as session:
        session.run(
            "MERGE (s:_SeedMeta {key: $key}) "
            "SET s.sha256 = $sha, s.updated_at = datetime()",
            key=key,
            sha=sha,
        )


def _load_patterns_from_db(
    version_filter: str | None = None,
) -> list[PatternExample] | None:
    """Load patterns from the `patterns` DB table (primary source, ADR-0007 + WI-8).

    Returns a list of PatternExample objects for active (non-soft-deleted) rows,
    ordered by pattern_id for stable hash computation.

    Returns None when the DB is unreachable or the table is empty so callers can
    fall back to the JSON file without further error propagation.

    Args:
        version_filter: When set, restrict to rows WHERE odoo_version_min = filter.

    """
    try:
        from src.db.pg import get_pool

        pool = get_pool()
        sql = (
            "SELECT pattern_id, intent_keywords, file_ref, snippet_text, gotchas, "
            "odoo_version_min, odoo_version_max, language, core_symbol_names "
            "FROM patterns WHERE soft_deleted = FALSE"
        )
        params: tuple = ()
        if version_filter:
            sql += " AND odoo_version_min = %s"
            params = (version_filter,)
        sql += " ORDER BY pattern_id"

        with pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()

        if not rows:
            return None  # empty DB -> caller falls back to JSON

        result: list[PatternExample] = []
        for r in rows:
            # gotchas stored as JSONB; may be a Python list already or a string
            gotchas_raw = r[4]
            if isinstance(gotchas_raw, str):
                gotchas_raw = json.loads(gotchas_raw)
            result.append(
                PatternExample(
                    pattern_id=r[0],
                    intent_keywords=list(r[1]) if r[1] else [],
                    file_ref=r[2],
                    snippet_text=r[3],
                    gotchas=gotchas_raw or [],
                    odoo_version_min=r[5],
                    # r[6] = odoo_version_max (already SELECTed; #329 plumbs it through).
                    odoo_version_max=r[6],
                    language=r[7],
                    core_symbol_names=list(r[8]) if r[8] else [],
                )
            )
        return result

    except Exception as exc:
        _logger.warning(
            "Pattern DB load failed (%s); falling back to JSON file", exc
        )
        return None


def _load_patterns_source(
    patterns_file: Path, version_filter: str | None,
) -> list[PatternExample]:
    """DB-primary load with JSON fallback (ADR-0007 + WI-8).

    Try 1: DB rows WHERE soft_deleted = FALSE (source-of-truth post-backfill).
    Try 2: JSON file (bootstrap fallback when DB empty or unreachable).
    """
    db_rows = _load_patterns_from_db(version_filter)
    if db_rows is not None:
        _logger.debug("Loaded %d patterns from DB", len(db_rows))
        return db_rows

    _logger.info(
        "DB load returned None (empty or unreachable); "
        "falling back to JSON file %s",
        patterns_file,
    )
    return _load_patterns(patterns_file, version_filter)


def recompute_sentinel_sha() -> str:
    """Recompute _SeedMeta sentinel SHA from the current pattern source-of-truth.

    Primary: DB rows (WHERE soft_deleted = FALSE, ORDER BY pattern_id).
    Fallback: patterns.json (when DB is empty or unreachable).

    The resulting SHA is stored on the Neo4j _SeedMeta nodes (both
    ``patterns_neo4j`` and ``patterns_pgvector`` keys) via
    ``_set_stored_patterns_sha()`` so that ADR-0007 auto-reseed picks up the
    change on the next ``index_profile()`` run.

    Returns the new hex SHA-256 string (64 chars).

    This function is called automatically after every CRUD write via the admin
    patterns endpoint (admin_patterns.py). It can also be called manually via
    POST /api/admin/patterns/sentinel/recompute.

    Side effects:
        - Writes updated sentinel SHA to Neo4j _SeedMeta nodes when Neo4j is
          reachable. Silently logs a warning and returns the computed SHA without
          updating the nodes when Neo4j is unavailable (e.g. during unit tests
          that do not have a Neo4j container running).
    """
    # 1. Compute the canonical content hash (WI-RV F-D: SSOT helper).
    try:
        new_sha = compute_patterns_canonical_sha()
    except Exception as exc:
        _logger.warning(
            "recompute_sentinel_sha: canonical hash failed (%s); "
            "using legacy file-bytes SHA as last resort",
            exc,
        )
        new_sha = _compute_patterns_sha256(_DEFAULT_PATTERNS_FILE)

    # 2. Persist the new SHA to Neo4j _SeedMeta nodes (best-effort).
    writer = _get_neo4j_writer()
    if writer:
        try:
            _set_stored_patterns_sha(writer.driver, new_sha, key="patterns_neo4j")
            _set_stored_patterns_sha(writer.driver, new_sha, key="patterns_pgvector")
        except Exception as exc:
            _logger.warning(
                "recompute_sentinel_sha: Neo4j sentinel write failed (%s) — "
                "returning SHA without persisting",
                exc,
            )
        finally:
            writer.close()
    else:
        _logger.debug(
            "recompute_sentinel_sha: Neo4j not configured — SHA computed "
            "but sentinel NOT written (Neo4j password missing)"
        )

    return new_sha


def _load_patterns(
    patterns_file: Path, version_filter: str | None,
) -> list[PatternExample]:
    raw = json.loads(patterns_file.read_text(encoding="utf-8"))

    # Validate against JSON Schema (Draft 2020-12) before processing.
    # Raises ValueError with human-readable path + message on first violation.
    if _PATTERNS_SCHEMA_FILE.exists():
        validator = _get_patterns_validator()
        errors = list(validator.iter_errors(raw))
        if errors:
            # Report the first error with its JSON path and message.
            first = errors[0]
            path = list(first.absolute_path)
            # Try to extract pattern_id for actionable error message.
            pattern_id = "<unknown>"
            if path and isinstance(path[0], int) and isinstance(raw, list):
                entry = raw[path[0]]
                if isinstance(entry, dict):
                    pattern_id = entry.get("pattern_id", f"index {path[0]}")
            raise ValueError(
                f"patterns.json schema validation failed "
                f"(pattern_id={pattern_id!r}, path={path}): {first.message}"
            )

    patterns: list[PatternExample] = []
    for entry in raw:
        if version_filter and entry.get("odoo_version_min") != version_filter:
            continue
        patterns.append(PatternExample(
            pattern_id=entry["pattern_id"],
            intent_keywords=entry.get("intent_keywords", []),
            file_ref=entry["file_ref"],
            snippet_text=entry["snippet_text"],
            gotchas=entry.get("gotchas", []),
            odoo_version_min=entry["odoo_version_min"],
            odoo_version_max=entry.get("odoo_version_max"),
            language=entry["language"],
            core_symbol_names=entry.get("core_symbol_names", []),
        ))
    return patterns


def run(
    *,
    writer,
    embedder=None,
    force: bool = False,
    patterns_file: Path | None = None,
    odoo_version_min_filter: str | None = None,
) -> dict:
    """Public entry point: idempotent seed of pattern catalogue.

    Re-uses an EXISTING Neo4jWriter (no open/close inside) — caller manages lifecycle.

    Returns: {"patterns": N_written, "embeddings": N_embedded, "skipped": bool}

    Sentinel policy (ADR-0007 D6-split):
    - ``patterns_neo4j`` sentinel updated only after Neo4j write succeeds.
    - ``patterns_pgvector`` sentinel updated only after pgvector write succeeds.
    - When ``embedder is None`` the pgvector sentinel is NOT updated — a future run
      with an embedder present will still write the embeddings.
    """
    patterns_path = patterns_file or _DEFAULT_PATTERNS_FILE
    if not patterns_path.exists():
        _logger.warning("patterns file not found at %s — skipping reseed", patterns_path)
        return {"patterns": 0, "embeddings": 0, "skipped": True}

    # WI-RV F-D: canonical SHA over the source-of-truth (DB-primary, file
    # fallback) — matches the SHA written by recompute_sentinel_sha() after
    # an admin patterns CRUD, so the two never diverge.  Prior to F-D this
    # was _compute_patterns_sha256(patterns_path) (file-bytes) which caused
    # a perpetual reseed whenever the DB diverged from disk.
    current_sha = compute_patterns_canonical_sha(
        version_filter=odoo_version_min_filter,
        patterns_file=patterns_path,
    )

    # Determine which stores need an update.
    neo4j_needs_update = force
    pgvector_needs_update = force and embedder is not None

    if not force:
        stored_neo4j_sha = _get_stored_patterns_sha(writer.driver, key="patterns_neo4j")
        neo4j_needs_update = current_sha != stored_neo4j_sha

        if embedder is not None:
            stored_pgvec_sha = _get_stored_patterns_sha(writer.driver, key="patterns_pgvector")
            pgvector_needs_update = current_sha != stored_pgvec_sha

    if not neo4j_needs_update and not pgvector_needs_update:
        _logger.info(
            "Auto-reseed: patterns unchanged (sha=%s) — skipping", current_sha[:12]
        )
        return {"patterns": 0, "embeddings": 0, "skipped": True}

    # WI-RV F-D: load patterns from the same source the SHA covers (DB-primary,
    # file fallback) so the written rows are byte-for-byte the SHA's payload.
    n_patterns = 0
    if neo4j_needs_update:
        patterns = _load_patterns_source(patterns_path, odoo_version_min_filter)
        writer.write_pattern_examples(patterns)
        n_patterns = len(patterns)
        # Neo4j sentinel updated after successful write.
        _set_stored_patterns_sha(writer.driver, current_sha, key="patterns_neo4j")
        _logger.info("Neo4j: wrote %d PatternExample nodes", n_patterns)
    else:
        _logger.info(
            "Auto-reseed: patterns_neo4j unchanged (sha=%s) — skipping Neo4j write",
            current_sha[:12],
        )
        patterns = _load_patterns_source(patterns_path, odoo_version_min_filter)

    n_embeddings = 0
    if embedder is not None and pgvector_needs_update:
        from src.indexer.writer_pgvector import make_pattern_chunks
        chunks = make_pattern_chunks(patterns)
        _write_pgvector_with_embedder(chunks, embedder)
        n_embeddings = len(chunks)
        # pgvector sentinel updated only after successful write.
        _set_stored_patterns_sha(writer.driver, current_sha, key="patterns_pgvector")
    elif embedder is None:
        _logger.info(
            "embedder=None — skipping pattern embeddings (patterns_pgvector sentinel NOT updated)"
        )
    else:
        _logger.info(
            "Auto-reseed: patterns_pgvector unchanged (sha=%s) — skipping pgvector write",
            current_sha[:12],
        )

    return {
        "patterns": n_patterns,
        "embeddings": n_embeddings,
        "skipped": not neo4j_needs_update and not pgvector_needs_update,
    }


def _write_pgvector_with_embedder(chunks: list, embedder) -> None:
    """Embed pattern chunks using an already-constructed embedder + write to pgvector.

    Replaces existing pattern_example rows for clean re-seed (idempotent).
    This variant accepts an external embedder object (unlike _write_pgvector which
    constructs one from config).

    WI-B: uses _embed_chunks_resilient for partial-failure tolerance; writes
    embedding_model/embedding_dim into each row; calls assert_dim_matches
    once as fail-fast guard against incompatible vector spaces.
    """
    from psycopg2.extras import execute_values

    from src.db.embedding_guard import assert_dim_matches
    from src.db.pg import get_pool
    from src.indexer.writer_pgvector import _INSERT_SQL, _embed_chunks_resilient

    live_chunks, vecs, _embed_calls = _embed_chunks_resilient(embedder, chunks)

    # Guard: if all chunks failed embedding, preserve existing rows.
    if not live_chunks:
        _logger.error(
            "embed produced 0 usable vectors for pattern chunks — "
            "preserving existing rows, skipping destructive rewrite"
        )
        return

    # Tolerate embedders without .model/.dim (test doubles, pre-ADR-0045) — see
    # writer_pgvector.write_module_embeddings for rationale.
    emb_model, emb_dim = _embedder_meta(embedder)

    with get_pool().checkout_vec() as conn:
        if emb_dim is not None:
            assert_dim_matches(conn, emb_dim, emb_model)
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                # Patterns are global (cross-tenant) → profile_name = GLOBAL_PROFILE.
                # Stamp the sentinel on every chunk before building the insert rows.
                for _c in live_chunks:
                    _c.profile_name = GLOBAL_PROFILE
                cur.execute(
                    "DELETE FROM embeddings "
                    "WHERE chunk_type = 'pattern_example' AND module = '__patterns__' "
                    "AND profile_name = %s",
                    (GLOBAL_PROFILE,),
                )
                rows = [
                    c.as_tuple(vecs[i], emb_model, emb_dim)
                    for i, c in enumerate(live_chunks)
                ]
                execute_values(cur, _INSERT_SQL, rows)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True  # restore for pool reuse


def _write_neo4j(patterns: list[PatternExample]) -> None:
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
            "neo4j_password in [database] section of odoo-semantic.conf.",
        )
    writer = Neo4jWriter(uri=uri, user=user, password=password)
    try:
        writer.setup_indexes()
        writer.write_pattern_examples(patterns)
    finally:
        writer.close()


def _get_neo4j_writer():
    """Build and return a Neo4jWriter from config, or None if password missing."""
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


def _write_pgvector(chunks: list[EmbeddingChunk]) -> None:
    """Embed pattern chunks via configured embedder + write to pgvector embeddings table.

    Replaces existing pattern_example rows for clean re-seed (idempotent).

    WI-B: constructs embedder via make_embedder() (factory, respects EMBEDDER_BACKEND);
    uses _embed_chunks_resilient for partial-failure tolerance; writes
    embedding_model/embedding_dim into each row; calls assert_dim_matches
    once as fail-fast guard against incompatible vector spaces.
    """
    from psycopg2.extras import execute_values

    from src.db.embedding_guard import assert_dim_matches
    from src.db.pg import get_pool
    from src.indexer.embedder import make_embedder
    from src.indexer.writer_pgvector import _INSERT_SQL, _embed_chunks_resilient

    embedder_url = config.from_env_or_ini(
        "EMBEDDER_URL", "embedder", "url",
        fallback="http://localhost:11434",
    )
    embedder_model = config.from_env_or_ini(
        "EMBEDDER_MODEL", "embedder", "model",
        fallback=DEFAULT_EMBEDDER_MODEL,
    )
    embedder_dim = int(config.from_env_or_ini(
        "EMBEDDER_DIM", "embedder", "dim", fallback="1024",
    ))
    embedder_auth = config.from_env_or_ini(
        "EMBEDDER_AUTH_TOKEN", "embedder", "auth_token", fallback=None,
    )
    embedder = make_embedder(
        url=embedder_url, model=embedder_model, dim=embedder_dim, auth_token=embedder_auth,
    )

    live_chunks, vecs, _embed_calls = _embed_chunks_resilient(embedder, chunks)

    # Guard: if all chunks failed embedding, preserve existing rows.
    if not live_chunks:
        _logger.error(
            "embed produced 0 usable vectors for pattern chunks — "
            "preserving existing rows, skipping destructive rewrite"
        )
        return

    emb_model, emb_dim = _embedder_meta(embedder)

    with get_pool().checkout_vec() as conn:
        if emb_dim is not None:
            assert_dim_matches(conn, emb_dim, emb_model)
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                # Patterns are global (cross-tenant) → profile_name = GLOBAL_PROFILE
                # (explicit '__global__' sentinel, m13_021).
                # Stamp the sentinel on every chunk before building the insert rows.
                for _c in live_chunks:
                    _c.profile_name = GLOBAL_PROFILE
                cur.execute(
                    "DELETE FROM embeddings "
                    "WHERE chunk_type = 'pattern_example' AND module = '__patterns__' "
                    "AND profile_name = %s",
                    (GLOBAL_PROFILE,),
                )
                rows = [
                    c.as_tuple(vecs[i], emb_model, emb_dim)
                    for i, c in enumerate(live_chunks)
                ]
                execute_values(cur, _INSERT_SQL, rows)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True  # restore for pool reuse


def _get_job_store() -> object | None:
    """Return the JobStore singleton for job tracking updates.

    Returns None (silently) when the pool is not initialized — job tracking
    is best-effort and must never block the seeding work.
    """
    try:
        from src.db.pg import job_store
        return job_store()
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    # ADR-0031: load `.env` at the CLI entry point so PG_DSN / NEO4J_* / EMBEDDER_*
    # (with secrets) resolve on a fresh prod box without manually sourcing .env.
    # Idempotent + main()-only (never at import) so pytest is unaffected; mirrors
    # src/db/migrate.py::main().
    config.init_dotenv()
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version", default=None,
        help="Filter to a specific odoo_version_min (e.g. 17.0). "
             "Default: all versions.",
    )
    parser.add_argument(
        "--no-embed", action="store_true",
        help="Skip the pgvector embed+write step (Neo4j only).",
    )
    parser.add_argument(
        "--patterns-file",
        default=str(_DEFAULT_PATTERNS_FILE),
        help=f"Path to patterns.json (default: {_DEFAULT_PATTERNS_FILE}).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Bypass sha256 gating, force reseed even if patterns.json unchanged.",
    )
    parser.add_argument(
        "--job-id",
        type=int,
        default=None,
        help="indexer_jobs row to update (queued→running→done/error).",
    )
    args = parser.parse_args(argv)

    # Initialize PostgreSQL pool (must be done before job_store() is called).
    dsn = config.from_env_or_ini("PG_DSN", "database", "pg_dsn", fallback=None)
    if dsn:
        from src.db.pg import get_pool, init_pool
        try:
            get_pool()
        except RuntimeError:
            init_pool(dsn, min_conn=1, max_conn=5)

    job_id: int | None = args.job_id
    _js = None
    if job_id is not None:
        _js = _get_job_store()

    def _mark_running():
        if job_id is not None and _js is not None:
            try:
                _js.update_job(
                    job_id,
                    status="running",
                    pid=os.getpid(),
                    started_at=datetime.now(UTC),
                )
            except Exception:
                pass

    def _mark_done(note: str | None = None):
        if job_id is not None and _js is not None:
            try:
                kwargs: dict = {
                    "status": "done",
                    "finished_at": datetime.now(UTC),
                }
                if note:
                    kwargs["error_msg"] = note
                _js.update_job(job_id, **kwargs)
            except Exception:
                pass

    def _mark_error(msg: str):
        if job_id is not None and _js is not None:
            try:
                _js.update_job(
                    job_id,
                    status="error",
                    finished_at=datetime.now(UTC),
                    error_msg=msg[:1000],
                )
            except Exception:
                pass

    try:
        _mark_running()

        patterns_file = Path(args.patterns_file)
        if not patterns_file.exists():
            _logger.error("patterns file not found: %s", patterns_file)
            _mark_error(f"patterns file not found: {patterns_file}")
            return 2

        # WI-RV F-D: canonical SHA (DB-primary + JSON fallback) so the CLI
        # entry-point agrees with run() + recompute_sentinel_sha() — see ADR-0042.
        current_sha = compute_patterns_canonical_sha(
            version_filter=args.version,
            patterns_file=patterns_file,
        )

        # Check sentinel gating (unless --force).
        # Per ADR-0007 D6-split: sentinel is split into patterns_neo4j and
        # patterns_pgvector.  Skip only when the relevant store(s) are up-to-date.
        if not args.force:
            writer = _get_neo4j_writer()
            if writer:
                try:
                    neo4j_sha = _get_stored_patterns_sha(writer.driver, key="patterns_neo4j")
                    neo4j_done = current_sha == neo4j_sha
                    if args.no_embed:
                        # Only Neo4j store matters — skip if neo4j is already up-to-date.
                        if neo4j_done:
                            _logger.info(
                                "Patterns unchanged (sha=%s) — skipping reseed (--no-embed)",
                                current_sha[:8],
                            )
                            _mark_done(note="skipped: hash unchanged")
                            return 0
                    else:
                        # Both stores must be up-to-date to skip.
                        pgvec_sha = _get_stored_patterns_sha(
                            writer.driver, key="patterns_pgvector"
                        )
                        pgvec_done = current_sha == pgvec_sha
                        if neo4j_done and pgvec_done:
                            _logger.info(
                                "Patterns unchanged (sha=%s) — skipping reseed",
                                current_sha[:8],
                            )
                            _mark_done(note="skipped: hash unchanged")
                            return 0
                        elif neo4j_done and not pgvec_done:
                            _logger.info(
                                "patterns_neo4j up-to-date but patterns_pgvector missing "
                                "(sha=%s) — will re-run pgvector embed only",
                                current_sha[:8],
                            )
                finally:
                    writer.close()

        # WI-RV F-D: load patterns through the same source-of-truth chain
        # the SHA covers (DB-primary + JSON fallback).  Loading via
        # _load_patterns(patterns_file, …) directly would skip the DB and
        # write stale JSON data over fresh DB rows on every CLI run.
        patterns = _load_patterns_source(patterns_file, args.version)
        if not patterns:
            _logger.warning(
                "No patterns matched filter (version=%s). Nothing to seed.",
                args.version or "<any>",
            )
            _mark_done()
            return 0

        _logger.info("Loaded %d patterns from source-of-truth chain", len(patterns))

        _write_neo4j(patterns)
        _logger.info("Neo4j: wrote %d PatternExample nodes", len(patterns))

        # Update the Neo4j sentinel after successful Neo4j write.
        writer = _get_neo4j_writer()
        if writer:
            try:
                _set_stored_patterns_sha(writer.driver, current_sha, key="patterns_neo4j")
            finally:
                writer.close()

        if args.no_embed:
            _logger.info(
                "Skipping pgvector embed step (--no-embed). "
                "patterns_pgvector sentinel NOT updated — "
                "run without --no-embed to populate pgvector embeddings."
            )
            _mark_done()
            return 0

        chunks = make_pattern_chunks(patterns)
        _write_pgvector(chunks)
        _logger.info("pgvector: wrote %d embedding chunks", len(chunks))

        # Update the pgvector sentinel only after successful pgvector write.
        writer = _get_neo4j_writer()
        if writer:
            try:
                _set_stored_patterns_sha(writer.driver, current_sha, key="patterns_pgvector")
            finally:
                writer.close()

        _mark_done()
        return 0

    except Exception as exc:
        _mark_error(str(exc))
        raise


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
