# SPDX-License-Identifier: AGPL-3.0-or-later
# src/db/embedding_guard.py
"""Fail-fast guard against embedding-model / dimension mismatches.

Problem
-------
The embeddings table stores vectors produced by a specific model (e.g.
qwen3-embedding-q5km, dim=1024).  If the operator switches to a different
model (different dim *or* different latent space) without reindexing, new
and old vectors are in incompatible spaces.  Cosine similarity across spaces
is meaningless but would not raise a SQL error — the bug would silently
corrupt query results.

Solution
--------
`assert_dim_matches` reads the embedding_dim AND embedding_model recorded in
the DB (set by m13_018 and kept current by the writer path) and raises:
- `EmbedderDimMismatch` when the configured dim differs from what is stored.
- `EmbedderModelMismatch` when the configured model name differs from what is
  stored (same dim can still mean incompatible latent spaces, e.g. switching
  from ollama/qwen3 to openai/text-embedding-3-small both at dim=1024).

Call this at indexer startup and at MCP server startup so the operator gets a
clear error message rather than silent ANN corruption.

Usage (startup guard)
---------------------
    from src.db.embedding_guard import (
        assert_dim_matches,
        EmbedderDimMismatch,
        EmbedderModelMismatch,
    )

    # At indexer / server startup, after connecting to PG:
    try:
        assert_dim_matches(pg_conn, configured_dim=embedder.dim,
                           configured_model=embedder.model)
    except (EmbedderDimMismatch, EmbedderModelMismatch) as exc:
        logger.error("Embedder mismatch — run a full reindex. %s", exc)
        raise SystemExit(1) from exc

    # At write time, record the model + dim alongside each vector:
    # INSERT INTO embeddings (..., embedding_model, embedding_dim, vec)
    # VALUES (..., %s, %s, %s)  -- (model_name_str, dim_int, vector)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.constants import normalize_embedder_model_name

if TYPE_CHECKING:
    from src.db._types import PgConn


class EmbedderDimMismatch(RuntimeError):
    """Raised when the configured embedder dimension differs from DB-recorded dim.

    Attributes
    ----------
    configured_dim : int
        The dim the current embedder is configured with.
    stored_dim : int
        The dim found in the `embedding_dim` column of existing rows.
    stored_model : str | None
        The `embedding_model` value of the sampled rows (for diagnosis).
    """

    def __init__(
        self,
        configured_dim: int,
        stored_dim: int,
        stored_model: str | None = None,
    ) -> None:
        self.configured_dim = configured_dim
        self.stored_dim = stored_dim
        self.stored_model = stored_model
        super().__init__(
            f"Embedding dimension mismatch: configured={configured_dim}, "
            f"stored_in_db={stored_dim} "
            f"(model='{stored_model or 'unknown'}').  "
            f"Mixing incompatible vector spaces silently corrupts cosine-similarity results.  "
            f"Fix: update embedder config to dim={stored_dim} OR run a full reindex "
            f"(`python -m src.indexer --full --profile <name>`) to rebuild all vectors."
        )


class EmbedderModelMismatch(RuntimeError):
    """Raised when the configured embedder model differs from DB-recorded model.

    Two models can share the same dimension (e.g. ollama/qwen3 and
    openai/text-embedding-3-small both at 1024) but produce vectors in
    incompatible latent spaces.  Mixing them silently corrupts ANN results.

    Attributes
    ----------
    configured_model : str
        The model name the current embedder is configured with.
    stored_model : str
        The `embedding_model` value found in existing DB rows.
    stored_dim : int | None
        The `embedding_dim` of the sampled rows (for diagnosis).
    """

    def __init__(
        self,
        configured_model: str,
        stored_model: str,
        stored_dim: int | None = None,
    ) -> None:
        self.configured_model = configured_model
        self.stored_model = stored_model
        self.stored_dim = stored_dim
        super().__init__(
            f"Embedding model mismatch: configured='{configured_model}', "
            f"stored_in_db='{stored_model}' "
            f"(dim={stored_dim or 'unknown'}).  "
            f"Different models produce incompatible latent spaces even at the same dimension.  "
            f"Fix: restore embedder config to model='{stored_model}' OR run a full reindex "
            f"(`python -m src.indexer --full --profile <name>`) to rebuild all vectors with "
            f"the new model."
        )


def assert_dim_matches(
    conn: PgConn,
    configured_dim: int,
    configured_model: str | None = None,
) -> None:
    """Raise EmbedderDimMismatch or EmbedderModelMismatch if there is a stored/configured mismatch.

    Queries a single representative row that has `embedding_dim` populated
    (i.e. indexed *after* migration m13_018).  If the table is empty or all
    rows pre-date m13_018 (embedding_dim IS NULL), the check is skipped --
    a full reindex will populate the column, and mismatches will be caught then.

    Parameters
    ----------
    conn:
        Open psycopg2 connection (any isolation level; uses a read-only query).
    configured_dim:
        The vector dimension the current embedder is configured with.
        Typically `embedder.dim` or `DEFAULT_EMBEDDER_DIM` from `src.constants`.
    configured_model:
        The model name the current embedder is configured with (e.g.
        'qwen3-embedding-q5km').  When ``None``, the model check is skipped
        (backward-compatible: callers that only pass dim are unaffected).

    Raises
    ------
    EmbedderDimMismatch
        When at least one row has a recorded `embedding_dim` that differs from
        `configured_dim`.  Checked before the model check.
    EmbedderModelMismatch
        When `configured_model` is not ``None``, the stored `embedding_model`
        is NOT NULL, and it differs from `configured_model`.
    ValueError
        When `configured_dim` is not a positive integer.
    """
    if not isinstance(configured_dim, int) or configured_dim <= 0:
        raise ValueError(f"configured_dim must be a positive int, got {configured_dim!r}")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT embedding_dim, embedding_model
              FROM embeddings
             WHERE embedding_dim IS NOT NULL
             LIMIT 1
            """
        )
        row = cur.fetchone()

    if row is None:
        # Table empty or all rows pre-date m13_018 — nothing to check yet.
        return

    stored_dim: int = row[0]
    stored_model: str | None = row[1]

    # Dim check first (a different dim is always an error).
    if stored_dim != configured_dim:
        raise EmbedderDimMismatch(
            configured_dim=configured_dim,
            stored_dim=stored_dim,
            stored_model=stored_model,
        )

    # Model check: only when caller supplies the model name AND DB row has one.
    # Normalize BOTH operands symmetrically so an optional Ollama ":latest" tag
    # on either side never reads as a model switch (Ollama: foo == foo:latest).
    configured_model = normalize_embedder_model_name(configured_model)
    stored_model = normalize_embedder_model_name(stored_model)
    if (
        configured_model is not None
        and stored_model is not None
        and stored_model != configured_model
    ):
        raise EmbedderModelMismatch(
            configured_model=configured_model,
            stored_model=stored_model,
            stored_dim=stored_dim,
        )
