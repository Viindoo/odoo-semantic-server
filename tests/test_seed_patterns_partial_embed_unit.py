# SPDX-License-Identifier: AGPL-3.0-or-later
"""A pattern chunk the embedder refuses keeps its row and the reseed retries (no DB).

Business rules protected here (the real SQL against pgvector + Neo4j is in
``tests/test_dual_store_integrity.py``):

- A refused pattern chunk (a 504 after the batch retry and the per-chunk
  fallback) keeps its existing row; the chunks that embedded are upserted.
- When no chunk gets a vector, nothing is written or deleted.
- A complete full-catalogue reseed deletes the rows of patterns no longer in
  the catalogue; a version-filtered reseed deletes nothing (it holds part of
  the catalogue only).
- ``patterns_pgvector`` is stamped only after a complete write, by ``run()``
  (auto-reseed) and by the CLI ``main()``, so an incomplete embed is retried
  by the next reseed; ``patterns_neo4j`` is unaffected.
- A WARNING says how many pattern chunks failed and that the next reseed
  retries.

The pgvector table is an in-memory dict keyed like ``ux_embeddings_chunk``
for the pattern rows: ``(odoo_version, entity_name, file_path, chunk_idx)``.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import src.indexer.seed_patterns as sp
from src.constants import GLOBAL_PROFILE
from src.indexer.models import PatternExample
from src.indexer.writer_pgvector import make_pattern_chunks

POISON = "refused_by_the_embedder"


def _pattern(pid: str, snippet: str, version: str = "99.0") -> PatternExample:
    return PatternExample(
        pattern_id=pid, intent_keywords=["k"], file_ref=f"{pid}.py:1",
        snippet_text=snippet, gotchas=["g"], odoo_version_min=version,
        language="python",
    )


def _key(p: PatternExample) -> tuple:
    return (p.odoo_version_min, f"python__{p.pattern_id}", p.file_ref, 0)


class _Embedder:
    """Refuses any request that holds a POISON text (like a 504), or every
    request with *fail_all*. No ``.dim``: the dim guard is skipped."""

    def __init__(self, *, fail_all: bool = False) -> None:
        self.fail_all = fail_all

    def embed(self, texts):
        if self.fail_all or any(POISON in t for t in texts):
            raise RuntimeError("504 Gateway Time-out")
        return [[0.5, 0.5] for _ in texts]


class _Table:
    """The pattern rows of ``embeddings``: key -> content."""

    def __init__(self) -> None:
        self.rows: dict[tuple, str] = {}
        self.touched = False

    def upsert(self, batch) -> None:
        for row in batch:
            # EmbeddingChunk.as_tuple: chunk_type, module, odoo_version,
            # entity_name, model_name, file_path, chunk_idx, content, ...
            assert row[9] == GLOBAL_PROFILE
            self.rows[(row[2], row[3], row[5], row[6])] = row[7]

    def prune(self, params) -> int:
        profile, versions, entities, files, idxs = params
        assert profile == GLOBAL_PROFILE
        keep = set(zip(versions, entities, files, idxs, strict=True))
        gone = [k for k in self.rows if k not in keep]
        for k in gone:
            del self.rows[k]
        return len(gone)


@pytest.fixture
def table(monkeypatch):
    t = _Table()

    class _Cursor:
        rowcount = 0

        def execute(self, sql, params=None):
            if "DELETE FROM embeddings" in sql:
                self.rowcount = t.prune(params)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    class _Conn:
        autocommit = True

        def cursor(self):
            return _Cursor()

        def commit(self):
            return None

        def rollback(self):
            return None

    class _Pool:
        @contextmanager
        def checkout_vec(self):
            t.touched = True
            yield _Conn()

    import psycopg2.extras

    import src.db.pg as pg

    monkeypatch.setattr(pg, "get_pool", lambda: _Pool())
    monkeypatch.setattr(psycopg2.extras, "execute_values", lambda _c, _s, batch: t.upsert(batch))
    return t


def _seed_rows(table: _Table, *patterns: PatternExample, content: str = "old") -> None:
    for p in patterns:
        table.rows[_key(p)] = content


# ---------------------------------------------------------------------------
# The pgvector write
# ---------------------------------------------------------------------------

def test_a_refused_pattern_chunk_keeps_its_existing_row(table, caplog):
    a, b = _pattern("a", f"new a {POISON}"), _pattern("b", "new b")
    _seed_rows(table, a, b)

    with caplog.at_level(logging.WARNING, logger="seed_patterns"):
        result = sp._write_pgvector_with_embedder(make_pattern_chunks([a, b]), _Embedder())

    assert table.rows[_key(a)] == "old", "the refused chunk's row must survive unchanged"
    assert "new b" in table.rows[_key(b)]
    assert not result.complete
    assert "1 of 2 chunk(s) were not embedded" in caplog.text
    assert "next reseed retries" in caplog.text


def test_no_vector_at_all_writes_and_deletes_nothing(table):
    a = _pattern("a", "a")
    _seed_rows(table, a, _pattern("gone", "gone"))

    result = sp._write_pgvector_with_embedder(
        make_pattern_chunks([a]), _Embedder(fail_all=True),
    )

    assert not table.touched, "0 usable vectors must not open a write at all"
    assert len(table.rows) == 2
    assert (result.attempted, result.embedded, result.complete) == (1, 0, False)


def test_a_complete_full_catalogue_reseed_deletes_the_rows_of_removed_patterns(table):
    a, gone = _pattern("a", "a"), _pattern("gone", "gone")
    _seed_rows(table, a, gone)

    result = sp._write_pgvector_with_embedder(make_pattern_chunks([a]), _Embedder())

    assert set(table.rows) == {_key(a)}
    assert result.complete and result.pruned == 1


def test_a_reseed_without_prune_deletes_nothing(table):
    other = _pattern("other", "other", version="98.0")
    _seed_rows(table, other)

    sp._write_pgvector_with_embedder(
        make_pattern_chunks([_pattern("a", "a")]), _Embedder(), prune=False,
    )

    assert _key(other) in table.rows


def test_a_reseed_of_the_same_catalogue_is_idempotent(table):
    chunks = [_pattern("a", "a"), _pattern("b", "b")]
    sp._write_pgvector_with_embedder(make_pattern_chunks(chunks), _Embedder())
    first = dict(table.rows)

    again = sp._write_pgvector_with_embedder(make_pattern_chunks(chunks), _Embedder())

    assert table.rows == first
    assert again.complete and again.pruned == 0


# ---------------------------------------------------------------------------
# The sentinel: run() (auto-reseed)
# ---------------------------------------------------------------------------

@pytest.fixture
def stamps(monkeypatch, tmp_path):
    """run() / main() wired to an in-memory sentinel store; returns the keys stamped."""
    stamped: list[str] = []
    monkeypatch.setattr(sp, "_DEFAULT_PATTERNS_FILE", tmp_path / "patterns.json")
    (tmp_path / "patterns.json").write_text("[]")
    monkeypatch.setattr(sp, "compute_patterns_canonical_sha", lambda **_kw: "sha-now")
    monkeypatch.setattr(sp, "_get_stored_patterns_sha", lambda _d, key="patterns_neo4j": None)
    monkeypatch.setattr(
        sp, "_set_stored_patterns_sha",
        lambda _d, sha, key="patterns_neo4j": stamped.append(key),
    )
    return stamped


def _writer():
    return SimpleNamespace(driver=object(), write_pattern_examples=lambda _p, prune: None)


def test_run_stamps_the_pgvector_sentinel_only_after_a_complete_embed(
    table, stamps, monkeypatch,
):
    patterns = [_pattern("a", f"a {POISON}"), _pattern("b", "b")]
    monkeypatch.setattr(sp, "_load_patterns_source", lambda _f, _v: patterns)

    incomplete = sp.run(writer=_writer(), embedder=_Embedder())

    assert stamps == ["patterns_neo4j"], "an incomplete embed must leave patterns_pgvector"
    assert incomplete["embed_failed"] == 1 and incomplete["embeddings"] == 1

    stamps.clear()
    healed = [_pattern("a", "a"), _pattern("b", "b")]
    monkeypatch.setattr(sp, "_load_patterns_source", lambda _f, _v: healed)
    done = sp.run(writer=_writer(), embedder=_Embedder())

    assert stamps == ["patterns_neo4j", "patterns_pgvector"]
    assert done["embed_failed"] == 0 and done["embeddings"] == 2


def test_run_stamps_nothing_for_pgvector_when_every_chunk_is_refused(table, stamps, monkeypatch):
    monkeypatch.setattr(sp, "_load_patterns_source", lambda _f, _v: [_pattern("a", "a")])

    sp.run(writer=_writer(), embedder=_Embedder(fail_all=True))

    assert stamps == ["patterns_neo4j"]


def test_a_version_filtered_run_keeps_the_other_versions_rows(table, stamps, monkeypatch):
    other = _pattern("other", "other", version="98.0")
    _seed_rows(table, other)
    monkeypatch.setattr(sp, "_load_patterns_source", lambda _f, _v: [_pattern("a", "a")])

    sp.run(writer=_writer(), embedder=_Embedder(), odoo_version_min_filter="99.0")

    assert _key(other) in table.rows, "a filtered load must not prune the rest"


# ---------------------------------------------------------------------------
# The sentinel: the CLI main() (seed-patterns, also spawned by the Web UI)
# ---------------------------------------------------------------------------

def _cli(monkeypatch, tmp_path, written: sp.PatternEmbedResult) -> int:
    from src import config

    # Never load a .env or a config file here: no DSN, no pool, no Neo4j.
    monkeypatch.setattr(config, "init_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(
        config, "from_env_or_ini", lambda *_a, fallback=None, **_k: fallback,
    )
    monkeypatch.setattr(sp, "_get_neo4j_writer", lambda: SimpleNamespace(
        driver=object(), close=lambda: None,
    ))
    monkeypatch.setattr(sp, "_write_neo4j", lambda _p, *, prune=False: None)
    monkeypatch.setattr(sp, "_load_patterns_source", lambda _f, _v: [_pattern("a", "a")])
    monkeypatch.setattr(sp, "_write_pgvector", lambda _c, *, prune=True: written)
    patterns_file = tmp_path / "cli_patterns.json"
    patterns_file.write_text("[]")
    return sp.main(["--patterns-file", str(patterns_file)])


def test_cli_leaves_the_pgvector_sentinel_after_an_incomplete_embed(stamps, monkeypatch, tmp_path):
    rc = _cli(monkeypatch, tmp_path, sp.PatternEmbedResult(attempted=3, embedded=2))

    assert rc == 0
    assert stamps == ["patterns_neo4j"]


def test_cli_stamps_the_pgvector_sentinel_after_a_complete_embed(stamps, monkeypatch, tmp_path):
    rc = _cli(monkeypatch, tmp_path, sp.PatternEmbedResult(attempted=3, embedded=3))

    assert rc == 0
    assert stamps == ["patterns_neo4j", "patterns_pgvector"]
