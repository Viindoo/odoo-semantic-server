"""One-shot CLI: load patterns.json → write Neo4j PatternExample nodes + embed pgvector.

Usage:
    python -m src.indexer.seed_patterns                    # all versions, with embed
    python -m src.indexer.seed_patterns --version 17.0     # filter by version_min
    python -m src.indexer.seed_patterns --no-embed         # skip pgvector step

Idempotent — MERGE on pattern_id; embedding rows replaced via DELETE-WHERE-INSERT.

Per ADR-0003: PatternExample = Neo4j node (composite key pattern_id) + reuse
`embeddings` table with chunk_type='pattern_example', module='__patterns__'.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from src import config
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


def _load_patterns(
    patterns_file: Path, version_filter: str | None,
) -> list[PatternExample]:
    raw = json.loads(patterns_file.read_text(encoding="utf-8"))
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


def _write_pgvector(chunks: list[EmbeddingChunk]) -> None:
    """Embed pattern chunks via Qwen3 + write to pgvector embeddings table.

    Replaces existing pattern_example rows for clean re-seed (idempotent).
    """
    import psycopg2
    from pgvector.psycopg2 import register_vector
    from psycopg2.extras import execute_values

    from src.indexer.embedder import Qwen3Embedder
    from src.indexer.writer_pgvector import _INSERT_SQL

    dsn = config.from_env_or_ini(
        "PG_DSN", "database", "pg_dsn", fallback=None,
    )
    if not dsn:
        raise RuntimeError(
            "PostgreSQL DSN missing. Set PG_DSN env var OR pg_dsn "
            "in [database] section of odoo-semantic.conf.",
        )

    embedder_url = config.from_env_or_ini(
        "EMBEDDER_URL", "embedder", "url",
        fallback="http://localhost:11434",
    )
    embedder_model = config.from_env_or_ini(
        "EMBEDDER_MODEL", "embedder", "model",
        fallback="qwen3-embedding-q5km",
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

    conn = psycopg2.connect(dsn)
    try:
        register_vector(conn)
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
        conn.close()


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
    args = parser.parse_args(argv)

    patterns_file = Path(args.patterns_file)
    if not patterns_file.exists():
        _logger.error("patterns file not found: %s", patterns_file)
        return 2

    patterns = _load_patterns(patterns_file, args.version)
    if not patterns:
        _logger.warning(
            "No patterns matched filter (version=%s). Nothing to seed.",
            args.version or "<any>",
        )
        return 0

    _logger.info("Loaded %d patterns from %s", len(patterns), patterns_file)

    _write_neo4j(patterns)
    _logger.info("Neo4j: wrote %d PatternExample nodes", len(patterns))

    if args.no_embed:
        _logger.info("Skipping pgvector embed step (--no-embed)")
        return 0

    chunks = make_pattern_chunks(patterns)
    _write_pgvector(chunks)
    _logger.info("pgvector: wrote %d embedding chunks", len(chunks))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
