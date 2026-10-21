# SPDX-License-Identifier: AGPL-3.0-or-later
"""A module whose embeddings were incomplete is re-embedded by the next run (no DB).

Business rules protected here (the no-DB half; the real ``index_profile`` runs
are in ``tests/test_embedding_retry_pipeline.py``):

- A chunk the embedder refuses (a 504, a timeout) is dropped by the write and
  the tracked module is marked ``embeddings_incomplete`` - for a partial AND a
  total failure - while the rows of the chunks that did embed are written.
- The modules the index run flags ``needs_rewrite`` for a re-embed are exactly
  the PRESENT modules whose embeddings were incomplete: a complete module, a
  module that was not embedded at all (no embedder) and a module the scan no
  longer has are not flagged.
- The operator line names the modules (capped sample with the total) and says
  whether they were flagged; a partial embed failure is never a run-wide
  outage, so it never makes the run exit 3 or mail the admins.
- ``reembed-stubs`` flags a module it could not embed completely in the
  ledger, and never fails because the ledger cannot be written.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from src.indexer import parse_health
from src.indexer.writer_pgvector import EmbeddingChunk, write_module_embeddings

POISON = "flaky_marker"


class _FlakyEmbedder:
    """Fails every call whose batch holds a text with POISON (like a 504 on
    one oversized request); embeds every other text."""

    def __init__(self, *, fail_all: bool = False) -> None:
        self.fail_all = fail_all

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.fail_all or any(POISON in t for t in texts):
            raise RuntimeError("504 Gateway Time-out")
        return [[0.1, 0.2, 0.3] for _ in texts]


def _chunk(entity: str, content: str) -> EmbeddingChunk:
    return EmbeddingChunk(
        chunk_type="field", module="flaky_mod", odoo_version="99.0",
        entity_name=entity, model_name="flaky.mod.record",
        file_path="flaky_mod/models/record.py", chunk_idx=0, content=content,
    )


class _FakeCursor:
    def __init__(self, sink: list) -> None:
        self.sink = sink

    def execute(self, *_a, **_k) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


class _FakeConn:
    autocommit = True

    def __init__(self, sink: list) -> None:
        self.sink = sink

    def cursor(self):
        return _FakeCursor(self.sink)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


@pytest.fixture
def written_rows(monkeypatch):
    """Route the pgvector write to an in-memory list of inserted rows."""
    rows: list = []

    class _Pool:
        @contextmanager
        def checkout_vec(self):
            yield _FakeConn(rows)

    import src.db.pg as pg
    import src.indexer.writer_pgvector as wp

    monkeypatch.setattr(pg, "get_pool", lambda: _Pool())
    monkeypatch.setattr(wp, "execute_values", lambda _cur, _sql, batch: rows.extend(batch))
    return rows


# ---------------------------------------------------------------------------
# The write marks the module incomplete
# ---------------------------------------------------------------------------

def test_a_chunk_the_embedder_refuses_marks_the_module_incomplete_and_the_rest_is_written(
    written_rows,
):
    chunks = [
        _chunk("flaky.mod.record.label", "label = fields.Char()"),
        _chunk("flaky.mod.record.flaky_marker", f"{POISON} = fields.Char()"),
    ]
    stats: dict = {}
    with parse_health.track("flaky_mod", "99.0") as health:
        write_module_embeddings(
            "flaky_mod", "99.0", chunks, _FlakyEmbedder(), profile_name="p",
            replace=False, stats=stats,
        )

    assert health.embeddings_incomplete is True
    assert [r[3] for r in written_rows] == ["flaky.mod.record.label"]
    assert stats == {"attempted": 2, "embedded": 1}


def test_an_embedder_that_refuses_every_chunk_marks_the_module_incomplete_and_writes_nothing(
    written_rows,
):
    chunks = [_chunk("flaky.mod.record.label", "label = fields.Char()")]
    with parse_health.track("flaky_mod", "99.0") as health:
        write_module_embeddings(
            "flaky_mod", "99.0", chunks, _FlakyEmbedder(fail_all=True), profile_name="p",
            replace=False,
        )

    assert health.embeddings_incomplete is True
    assert written_rows == [], "0 usable vectors must keep the existing rows untouched"


def test_a_complete_embed_does_not_mark_the_module_incomplete(written_rows):
    chunks = [_chunk("flaky.mod.record.label", "label = fields.Char()")]
    with parse_health.track("flaky_mod", "99.0") as health:
        write_module_embeddings(
            "flaky_mod", "99.0", chunks, _FlakyEmbedder(), profile_name="p", replace=False,
        )

    assert health.embeddings_incomplete is False
    assert len(written_rows) == 1


# ---------------------------------------------------------------------------
# Which modules the run flags for a re-embed
# ---------------------------------------------------------------------------

def _health(name: str, *, written: bool = True, incomplete: bool = False):
    h = parse_health.ModuleParseHealth(module=name, odoo_version="99.0")
    h.embeddings_written = written
    h.embeddings_incomplete = incomplete
    return h


def test_only_present_modules_with_incomplete_embeddings_are_flagged_for_a_reembed():
    from src.indexer.pipeline_repo import _embedding_retry_names

    module_health = {
        ("99.0", "flaky_mod"): _health("flaky_mod", incomplete=True),
        ("99.0", "healthy_mod"): _health("healthy_mod"),
        ("99.0", "no_embedder_mod"): _health("no_embedder_mod", written=False),
        ("99.0", "gone_mod"): _health("gone_mod", incomplete=True),
        ("99.0", "unparsed_mod"): None,
    }
    present = {"flaky_mod", "healthy_mod", "no_embedder_mod", "unparsed_mod"}

    assert _embedding_retry_names(module_health, present) == {"flaky_mod"}


def test_a_module_incomplete_on_every_run_is_flagged_on_every_run():
    """No once-per-state rule (unlike a degraded parse): the same module with the
    same incomplete outcome is flagged again by the next run."""
    from src.indexer.pipeline_repo import _embedding_retry_names

    for _run in range(3):
        module_health = {("99.0", "flaky_mod"): _health("flaky_mod", incomplete=True)}
        assert _embedding_retry_names(module_health, {"flaky_mod"}) == {"flaky_mod"}


# ---------------------------------------------------------------------------
# What the operator is told
# ---------------------------------------------------------------------------

def test_the_operator_line_names_the_modules_and_says_they_are_flagged():
    from src.indexer.pipeline_repo import _embedding_retry_message

    line = _embedding_retry_message({"b_mod", "a_mod"}, flagged=True)

    assert "2 module(s)" in line
    assert "a_mod, b_mod" in line
    assert "needs_rewrite" in line and "next run re-embeds" in line


def test_the_operator_line_caps_its_sample_but_keeps_the_total():
    from src.indexer.pipeline_repo import EMBED_RETRY_SAMPLE, _embedding_retry_message

    names = {f"mod_{i:02d}" for i in range(EMBED_RETRY_SAMPLE + 5)}
    line = _embedding_retry_message(names, flagged=True)

    assert f"{EMBED_RETRY_SAMPLE + 5} module(s)" in line
    assert f"mod_{EMBED_RETRY_SAMPLE - 1:02d}" in line
    assert f"mod_{EMBED_RETRY_SAMPLE:02d}" not in line
    assert "... 5 more" in line


def test_without_a_ledger_the_operator_line_says_the_modules_were_not_flagged():
    from src.indexer.pipeline_repo import FETCH_FAILED_MARK, _embedding_retry_message

    line = _embedding_retry_message({"a_mod"}, flagged=False)

    assert "not flagged" in line and "index-repo --full" in line
    # The attention text is read back by the next run for the stale-fetch rule.
    assert FETCH_FAILED_MARK not in line


def test_a_partial_embed_failure_is_not_a_run_outage_and_needs_no_attention():
    """Exit 3 / the alert mail are for what OSM cannot self-heal; an embedder
    that returned vectors for part of the run is self-healed by the retry."""
    from src.indexer.__main__ import _lifecycle_exit_code
    from src.indexer.pipeline import _empty_lifecycle, _finish_lifecycle

    lc = _empty_lifecycle()
    lc["embed_attempted"], lc["embed_ok"] = 10, 7

    done = _finish_lifecycle(lc)

    assert done["outages"] == []
    assert done["needs_attention"] is False
    assert _lifecycle_exit_code(done) == 0


# ---------------------------------------------------------------------------
# reembed-stubs
# ---------------------------------------------------------------------------

class _Presence:
    def __init__(self, *, raises: bool = False) -> None:
        self.raises = raises
        self.flagged: list = []

    def mark_needs_rewrite(self, repo_id, name):
        if self.raises:
            raise RuntimeError("ledger down")
        self.flagged.append((repo_id, name))
        return True


def test_reembed_stubs_flags_an_incomplete_module_in_the_ledger():
    from src.indexer.pipeline_reembed import _flag_for_reembed

    presence = _Presence()

    assert _flag_for_reembed(presence, {"id": 7}, "flaky_mod") is True
    assert presence.flagged == [(7, "flaky_mod")]


def test_reembed_stubs_never_fails_because_the_ledger_cannot_be_written():
    from src.indexer.pipeline_reembed import _flag_for_reembed

    assert _flag_for_reembed(_Presence(raises=True), {"id": 7}, "flaky_mod") is False
    assert _flag_for_reembed(None, {"id": 7}, "flaky_mod") is False
    assert _flag_for_reembed(_Presence(), {"id": None}, "flaky_mod") is False
