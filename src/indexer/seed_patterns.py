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
from src.constants import DEFAULT_EMBEDDER_MODEL
from src.indexer.models import PatternExample
from src.indexer.writer_neo4j import Neo4jWriter
from src.indexer.writer_pgvector import (
    EmbeddingChunk,
    make_pattern_chunks,
)

_logger = logging.getLogger("seed_patterns")

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
    """SHA-256 hex of patterns.json content (for change detection)."""
    return hashlib.sha256(json_path.read_bytes()).hexdigest()


def _get_stored_patterns_sha(driver) -> str | None:
    """Return sha256 stored on the _SeedMeta sentinel node, or None if no sentinel."""
    with driver.session() as session:
        row = session.run(
            "MATCH (s:_SeedMeta {key: 'patterns'}) RETURN s.sha256 AS sha LIMIT 1"
        ).single()
        return row["sha"] if row else None


def _set_stored_patterns_sha(driver, sha: str) -> None:
    """MERGE _SeedMeta sentinel node and set sha256 + updated_at."""
    with driver.session() as session:
        session.run(
            "MERGE (s:_SeedMeta {key: 'patterns'}) "
            "SET s.sha256 = $sha, s.updated_at = datetime()",
            sha=sha,
        )


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
    """
    patterns_path = patterns_file or _DEFAULT_PATTERNS_FILE
    if not patterns_path.exists():
        _logger.warning("patterns file not found at %s — skipping reseed", patterns_path)
        return {"patterns": 0, "embeddings": 0, "skipped": True}

    current_sha = _compute_patterns_sha256(patterns_path)

    if not force:
        stored_sha = _get_stored_patterns_sha(writer.driver)
        if current_sha == stored_sha:
            _logger.info(
                "Patterns unchanged (sha=%s) — skipping reseed", current_sha[:12]
            )
            return {"patterns": 0, "embeddings": 0, "skipped": True}

    patterns = _load_patterns(patterns_path, odoo_version_min_filter)
    writer.write_pattern_examples(patterns)

    n_embeddings = 0
    if embedder is not None:
        from src.indexer.writer_pgvector import make_pattern_chunks
        chunks = make_pattern_chunks(patterns)
        _write_pgvector_with_embedder(chunks, embedder)
        n_embeddings = len(chunks)
    else:
        _logger.info("embedder=None — skipping pattern embeddings")

    # Sentinel updated only after success
    _set_stored_patterns_sha(writer.driver, current_sha)
    return {"patterns": len(patterns), "embeddings": n_embeddings, "skipped": False}


def _write_pgvector_with_embedder(chunks: list, embedder) -> None:
    """Embed pattern chunks using an already-constructed embedder + write to pgvector.

    Replaces existing pattern_example rows for clean re-seed (idempotent).
    This variant accepts an external embedder object (unlike _write_pgvector which
    constructs one from config).
    """
    from psycopg2.extras import execute_values

    from src.db.pg import get_pool
    from src.indexer.writer_pgvector import _INSERT_SQL

    texts = [c.content for c in chunks]
    vecs = embedder.embed(texts)

    with get_pool().checkout_vec() as conn:
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM embeddings "
                    "WHERE chunk_type = 'pattern_example' AND module = '__patterns__'",
                )
                execute_values(
                    cur, _INSERT_SQL,
                    [c.as_tuple(vecs[i]) for i, c in enumerate(chunks)],
                )
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
    """Embed pattern chunks via Qwen3 + write to pgvector embeddings table.

    Replaces existing pattern_example rows for clean re-seed (idempotent).
    """
    from psycopg2.extras import execute_values

    from src.db.pg import get_pool
    from src.indexer.embedder import Qwen3Embedder
    from src.indexer.writer_pgvector import _INSERT_SQL

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
    embedder = Qwen3Embedder(
        embedder_url, embedder_model, dim=embedder_dim, auth_token=embedder_auth,
    )

    texts = [c.content for c in chunks]
    vecs = embedder.embed(texts)

    with get_pool().checkout_vec() as conn:
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM embeddings "
                    "WHERE chunk_type = 'pattern_example' AND module = '__patterns__'",
                )
                execute_values(
                    cur, _INSERT_SQL,
                    [c.as_tuple(vecs[i]) for i, c in enumerate(chunks)],
                )
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

        # Compute sha256 of patterns.json for change detection.
        current_sha = _compute_patterns_sha256(patterns_file)

        # Check sentinel gating (unless --force).
        if not args.force:
            writer = _get_neo4j_writer()
            if writer:
                try:
                    stored_sha = _get_stored_patterns_sha(writer.driver)
                    if current_sha == stored_sha:
                        _logger.info(
                            "Patterns unchanged (sha=%s) — skipping reseed",
                            current_sha[:8],
                        )
                        _mark_done(note="skipped: hash unchanged")
                        return 0
                finally:
                    writer.close()

        patterns = _load_patterns(patterns_file, args.version)
        if not patterns:
            _logger.warning(
                "No patterns matched filter (version=%s). Nothing to seed.",
                args.version or "<any>",
            )
            _mark_done()
            return 0

        _logger.info("Loaded %d patterns from %s", len(patterns), patterns_file)

        _write_neo4j(patterns)
        _logger.info("Neo4j: wrote %d PatternExample nodes", len(patterns))

        if args.no_embed:
            _logger.info("Skipping pgvector embed step (--no-embed)")
            # Still update sentinel even if no embed (only Neo4j written).
            writer = _get_neo4j_writer()
            if writer:
                try:
                    _set_stored_patterns_sha(writer.driver, current_sha)
                finally:
                    writer.close()
            _mark_done()
            return 0

        chunks = make_pattern_chunks(patterns)
        _write_pgvector(chunks)
        _logger.info("pgvector: wrote %d embedding chunks", len(chunks))

        # After successful seed, update sentinel.
        writer = _get_neo4j_writer()
        if writer:
            try:
                _set_stored_patterns_sha(writer.driver, current_sha)
            finally:
                writer.close()

        _mark_done()
        return 0

    except Exception as exc:
        _mark_error(str(exc))
        raise


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
