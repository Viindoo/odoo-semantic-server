# SPDX-License-Identifier: AGPL-3.0-or-later
"""Re-writing a module does not re-embed the chunks whose text did not change (no DB).

Business rules protected here:

- A module re-written with the same chunk texts (a full re-index after a
  ``head_sha`` reset, ``--full``, a ``needs_rewrite`` rewrite) sends nothing
  to the embedder: every row is written with the vector already stored for
  that text, and the rows are still upserted (fresh provenance such as
  ``line_start``), because the shared-module prune reads ``indexed_at``.
- Only the chunks whose text changed (or is new) are embedded.
- A stored vector is reused only when it was produced by the same embedding
  model AND dimension as the current embedder; after a model switch every
  chunk is embedded again. An embedder that does not name its model or
  dimension reuses nothing.
- The lookup is scoped to the module, version and profile being written.
- A failed lookup never fails the write: the module is embedded in full.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from src.indexer import writer_pgvector as wp
from src.indexer.writer_pgvector import EmbeddingChunk, write_module_embeddings

MODEL = "qwen3-embedding-q5km"
DIM = 3


class _RecordingEmbedder:
    """Embeds every text as a vector derived from its length; records the texts."""

    def __init__(self, model: str | None = MODEL, dim: int | None = DIM) -> None:
        if model is not None:
            self.model = model
        if dim is not None:
            self.dim = dim
        self.sent: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.sent.extend(texts)
        return [[float(len(t)), 0.0, 1.0] for t in texts]


def _chunk(entity: str, content: str, *, line: int = 1) -> EmbeddingChunk:
    return EmbeddingChunk(
        chunk_type="field", module="reuse_mod", odoo_version="99.0",
        entity_name=entity, model_name="reuse.mod.record",
        file_path="reuse_mod/models/record.py", chunk_idx=0, content=content,
        line_start=line,
    )


class _Table:
    """The rows of the embeddings table the reuse lookup can see."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.inserted: list[tuple] = []
        self.fail_lookup = False
        self.lookups: list[tuple] = []

    def add(self, content: str, vec: list[float], *, module="reuse_mod", version="99.0",
            profile="p", model=MODEL, dim=DIM) -> None:
        self.rows.append({
            "module": module, "odoo_version": version, "profile_name": profile,
            "embedding_model": model, "embedding_dim": dim,
            "content": content, "vec": vec,
        })


class _Cursor:
    def __init__(self, table: _Table) -> None:
        self.table = table
        self._result: list = []

    def execute(self, sql, params=None) -> None:
        if sql == wp._REUSABLE_VECTORS_SQL:
            if self.table.fail_lookup:
                raise RuntimeError("connection reset")
            module, version, profile, model, dim, contents = params
            self.table.lookups.append(params)
            self._result = [
                (r["content"], r["vec"]) for r in self.table.rows
                if r["module"] == module and r["odoo_version"] == version
                and r["profile_name"] == profile and r["embedding_model"] == model
                and r["embedding_dim"] == dim and r["content"] in contents
            ]

    def fetchall(self) -> list:
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


class _Conn:
    autocommit = True

    def __init__(self, table: _Table) -> None:
        self.table = table

    def cursor(self):
        return _Cursor(self.table)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


@pytest.fixture
def table(monkeypatch):
    t = _Table()

    class _Pool:
        @contextmanager
        def checkout_vec(self):
            yield _Conn(t)

    import src.db.embedding_guard as guard
    import src.db.pg as pg

    monkeypatch.setattr(pg, "get_pool", lambda: _Pool())
    monkeypatch.setattr(guard, "assert_dim_matches", lambda *_a, **_k: None)
    monkeypatch.setattr(wp, "execute_values", lambda _cur, _sql, batch: t.inserted.extend(batch))
    return t


def _written(table: _Table) -> dict[str, tuple]:
    """entity_name -> (vec, line_start) of the rows the write upserted."""
    return {row[3]: (list(row[8]), row[10]) for row in table.inserted}


def test_rewriting_an_unchanged_module_sends_nothing_to_the_embedder(table):
    table.add("[reuse_mod] a = fields.Char()", [9.0, 9.0, 9.0])
    table.add("[reuse_mod] b = fields.Integer()", [8.0, 8.0, 8.0])
    embedder = _RecordingEmbedder()
    stats: dict = {}

    calls = write_module_embeddings(
        "reuse_mod", "99.0",
        [_chunk("a", "[reuse_mod] a = fields.Char()", line=40),
         _chunk("b", "[reuse_mod] b = fields.Integer()", line=41)],
        embedder, profile_name="p", replace=False, stats=stats,
    )

    assert embedder.sent == []
    assert calls == 0
    # Every row is still upserted, with the stored vector and the new provenance.
    assert _written(table) == {"a": ([9.0, 9.0, 9.0], 40), "b": ([8.0, 8.0, 8.0], 41)}
    assert stats == {"attempted": 0, "embedded": 0, "reused": 2}


def test_only_the_chunks_whose_text_changed_are_embedded(table):
    table.add("[reuse_mod] a = fields.Char()", [9.0, 9.0, 9.0])
    table.add("[reuse_mod] b = fields.Integer()", [8.0, 8.0, 8.0])
    embedder = _RecordingEmbedder()

    write_module_embeddings(
        "reuse_mod", "99.0",
        [_chunk("a", "[reuse_mod] a = fields.Char()"),
         _chunk("b", "[reuse_mod] b = fields.Float()"),
         _chunk("c", "[reuse_mod] c = fields.Text()")],
        embedder, profile_name="p", replace=False,
    )

    assert sorted(embedder.sent) == [
        "[reuse_mod] b = fields.Float()", "[reuse_mod] c = fields.Text()",
    ]
    written = _written(table)
    assert written["a"][0] == [9.0, 9.0, 9.0]
    assert written["b"][0] == [float(len("[reuse_mod] b = fields.Float()")), 0.0, 1.0]


@pytest.mark.parametrize(
    ("stored_model", "stored_dim"),
    [("another-embedding-model", DIM), (MODEL, 1024), (None, None)],
    ids=["other-model", "other-dim", "unstamped-row"],
)
def test_a_vector_of_another_model_or_dimension_is_never_reused(
    table, stored_model, stored_dim,
):
    table.add("[reuse_mod] a = fields.Char()", [9.0, 9.0, 9.0],
              model=stored_model, dim=stored_dim)
    embedder = _RecordingEmbedder()

    write_module_embeddings(
        "reuse_mod", "99.0", [_chunk("a", "[reuse_mod] a = fields.Char()")],
        embedder, profile_name="p", replace=False,
    )

    assert embedder.sent == ["[reuse_mod] a = fields.Char()"]


def test_an_embedder_without_model_or_dimension_reuses_nothing(table):
    table.add("[reuse_mod] a = fields.Char()", [9.0, 9.0, 9.0])
    embedder = _RecordingEmbedder(model=None, dim=None)

    write_module_embeddings(
        "reuse_mod", "99.0", [_chunk("a", "[reuse_mod] a = fields.Char()")],
        embedder, profile_name="p", replace=False,
    )

    assert embedder.sent == ["[reuse_mod] a = fields.Char()"]
    assert table.lookups == []


@pytest.mark.parametrize(
    "where",
    [{"module": "other_mod"}, {"version": "98.0"}, {"profile": "other_profile"}],
    ids=["other-module", "other-version", "other-profile"],
)
def test_the_lookup_only_reuses_rows_of_the_same_module_version_and_profile(table, where):
    table.add("[reuse_mod] a = fields.Char()", [9.0, 9.0, 9.0], **where)
    embedder = _RecordingEmbedder()

    write_module_embeddings(
        "reuse_mod", "99.0", [_chunk("a", "[reuse_mod] a = fields.Char()")],
        embedder, profile_name="p", replace=False,
    )

    assert embedder.sent == ["[reuse_mod] a = fields.Char()"]


def test_a_failed_lookup_embeds_the_module_in_full(table, caplog):
    table.add("[reuse_mod] a = fields.Char()", [9.0, 9.0, 9.0])
    table.fail_lookup = True
    embedder = _RecordingEmbedder()

    write_module_embeddings(
        "reuse_mod", "99.0", [_chunk("a", "[reuse_mod] a = fields.Char()")],
        embedder, profile_name="p", replace=False,
    )

    assert embedder.sent == ["[reuse_mod] a = fields.Char()"]
    assert len(table.inserted) == 1
    assert "embedding reuse lookup failed" in caplog.text


# ---------------------------------------------------------------------------
# Incomplete-embedding retry (PR #392) with reused chunks
# ---------------------------------------------------------------------------


class _RefusingEmbedder(_RecordingEmbedder):
    """A healthy embedder that refuses one text (like a 500 on one input)."""

    def __init__(self, refused: str) -> None:
        super().__init__()
        self.refused = refused

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.refused in texts:
            self.sent.extend(texts)
            raise RuntimeError("500 Internal Server Error")
        return super().embed(texts)


def test_a_module_whose_chunks_are_all_reused_is_complete(table):
    """Every chunk reused is a complete embed: the module is not flagged
    needs_rewrite for a re-embed (it would be re-written every night)."""
    from src.indexer import parse_health

    table.add("[reuse_mod] a = fields.Char()", [9.0, 9.0, 9.0])
    with parse_health.track("reuse_mod", "99.0") as health:
        write_module_embeddings(
            "reuse_mod", "99.0", [_chunk("a", "[reuse_mod] a = fields.Char()")],
            _RecordingEmbedder(), profile_name="p", replace=False,
        )

    assert health.embeddings_incomplete is False


def test_a_refused_chunk_beside_reused_ones_still_marks_the_module_incomplete(table):
    """The reused rows are written, the refused one is not, and the module is
    retried by the next run (only the refused chunk is sent again then)."""
    from src.indexer import parse_health

    table.add("[reuse_mod] a = fields.Char()", [9.0, 9.0, 9.0])
    embedder = _RefusingEmbedder("[reuse_mod] b = fields.Html()")
    stats: dict = {}
    with parse_health.track("reuse_mod", "99.0") as health:
        write_module_embeddings(
            "reuse_mod", "99.0",
            [_chunk("a", "[reuse_mod] a = fields.Char()"),
             _chunk("b", "[reuse_mod] b = fields.Html()")],
            embedder, profile_name="p", replace=False, stats=stats,
        )

    assert health.embeddings_incomplete is True
    assert set(embedder.sent) == {"[reuse_mod] b = fields.Html()"}
    assert set(_written(table)) == {"a"}
    assert stats == {"attempted": 1, "embedded": 0, "reused": 1}


# ---------------------------------------------------------------------------
# Opt-out: force a re-embed (--no-embed-reuse)
# ---------------------------------------------------------------------------


def test_reuse_off_sends_every_chunk_to_the_embedder(table):
    """Stored vectors can be wrong although text and model stamp match (numerics
    changed under the same model tag, a truncated text before num_ctx grew):
    ``reuse=False`` re-embeds everything and writes the new vectors."""
    table.add("[reuse_mod] a = fields.Char()", [9.0, 9.0, 9.0])
    table.add("[reuse_mod] b = fields.Integer()", [8.0, 8.0, 8.0])
    embedder = _RecordingEmbedder()
    stats: dict = {}

    write_module_embeddings(
        "reuse_mod", "99.0",
        [_chunk("a", "[reuse_mod] a = fields.Char()"),
         _chunk("b", "[reuse_mod] b = fields.Integer()")],
        embedder, profile_name="p", replace=False, stats=stats, reuse=False,
    )

    assert sorted(embedder.sent) == [
        "[reuse_mod] a = fields.Char()", "[reuse_mod] b = fields.Integer()",
    ]
    assert table.lookups == [], "no stored vector is even looked up"
    assert _written(table)["a"][0] == [float(len("[reuse_mod] a = fields.Char()")), 0.0, 1.0]
    assert stats == {"attempted": 2, "embedded": 2}
