# SPDX-License-Identifier: AGPL-3.0-or-later
"""A module whose embeddings were incomplete is re-embedded by the next run.

Trigger (prod 2026-10-21): the embed endpoint returned 504 / timed out while
the GPU was shared. The chunks it refused were dropped, and the next nightly
run skipped the module at its unchanged HEAD, so its embeddings stayed missing
until a new commit or ``--full``. Business rules protected here:

- A module some of whose chunks the embedder refused is flagged
  ``needs_rewrite`` by that run; the refused chunks have no row, the others do.
- The next run re-parses and re-embeds it with no operator action (no new
  commit): every chunk then has a row and the flag is cleared, so the run
  after that is the zero-cost skip again.
- A module incomplete again on its retry run keeps its flag (retried on every
  run until it succeeds, never cleared by the run that re-wrote it).
- An embedder that refuses every chunk of the module (0 usable vectors, the
  existing rows kept) flags it too.
- A partial embed failure is self-healing: the run names the modules in the
  repo's ``lifecycle_attention`` but does not need attention (no exit 3, no
  alert mail).

Every test drives the real ``index_profile`` over temp git repos with a bare
origin, against real Neo4j and PostgreSQL + pgvector.
"""
from __future__ import annotations

import pytest

from src.db.migrate import _vector_extension_available, run_migrations
from src.indexer.embedder import FakeEmbedder
from tests._lifecycle_repo import (
    GitRepo,
    V,
    embeddings,
    lc,
    ledger,
    register,
    repo_row,
    run,
    write_module,
)

pytestmark = [pytest.mark.postgres, pytest.mark.neo4j]

POISON = "flaky_marker"
FLAKY_FIELD = f"{POISON} = fields.Char()"


@pytest.fixture
def pg(clean_pg, clean_neo4j):
    """Migrated PG (with pgvector) + clean Neo4j; the pool comes from pg_conn."""
    run_migrations(clean_pg)
    if not _vector_extension_available(clean_pg):
        pytest.skip("pgvector extension not installed - embeddings cannot be checked")
    return clean_pg


class _FlakyEmbedder(FakeEmbedder):
    """Refuses every request holding a POISON text (a 504 on that request),
    or every request at all with *fail_all*; embeds the rest like FakeEmbedder."""

    def __init__(self, *, fail_all: bool = False) -> None:
        super().__init__(dim=1024)
        self.fail_all = fail_all

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.fail_all or any(POISON in t for t in texts):
            raise RuntimeError("504 Gateway Time-out")
        return super().embed(texts)


def _poison_rows(pg_conn) -> int:
    """Embedding rows of the chunks the flaky embedder refuses."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM embeddings WHERE odoo_version = %s AND module = %s "
            "AND content LIKE %s",
            (V, "flaky_mod", f"%{POISON}%"),
        )
        return cur.fetchone()[0]


def _flagged(pg_conn, rid: int, name: str) -> bool:
    return bool(ledger(pg_conn, rid, name)["needs_rewrite"])


def _attention(pg_conn, rid: int) -> str:
    return repo_row(pg_conn, rid).get("lifecycle_attention") or ""


def _two_modules(tmp_path, *, poisoned: bool = True) -> GitRepo:
    repo = GitRepo(tmp_path, "embretry_addons")
    write_module(repo, "flaky_mod", extra_field=FLAKY_FIELD if poisoned else "")
    write_module(repo, "healthy_mod")
    repo.commit("add")
    return repo


def test_refused_chunks_flag_the_module_and_the_next_run_re_embeds_it(pg, tmp_path):
    repo = _two_modules(tmp_path)
    (rid,) = register("embretry_99", repo)

    first = run(pg, "embretry_99", embedder=_FlakyEmbedder())

    assert _poison_rows(pg) == 0, "precondition: the refused chunk has no row"
    assert embeddings(pg, "flaky_mod") > 0, "the chunks that embedded are written"
    assert _flagged(pg, rid, "flaky_mod"), "an incomplete module must be flagged needs_rewrite"
    assert not _flagged(pg, rid, "healthy_mod"), "a complete module must not be flagged"
    assert "flaky_mod" in _attention(pg, rid)
    assert "healthy_mod" not in _attention(pg, rid)
    assert not lc(first).get("needs_attention"), (
        "a partial embed failure self-heals: it must not make the run exit 3"
    )

    second = run(pg, "embretry_99")  # healthy embedder, no new commit

    assert second["modules"] >= 1, "the flagged module must be re-parsed at the unchanged HEAD"
    assert _poison_rows(pg) > 0, "the retry must embed the chunk the first run refused"
    assert not _flagged(pg, rid, "flaky_mod"), "a complete re-embed must clear the flag"
    assert "flaky_mod" not in _attention(pg, rid)

    third = run(pg, "embretry_99")

    assert third["modules"] == 0, "once healed, the unchanged repo is the zero-cost skip"


def test_a_module_incomplete_again_on_its_retry_keeps_its_flag(pg, tmp_path):
    repo = _two_modules(tmp_path)
    (rid,) = register("embretry_99", repo)
    run(pg, "embretry_99", embedder=_FlakyEmbedder())
    assert _flagged(pg, rid, "flaky_mod"), "precondition: flagged by the first run"

    retry = run(pg, "embretry_99", embedder=_FlakyEmbedder())

    assert retry["modules"] >= 1, "precondition: the retry run re-parsed the module"
    assert _poison_rows(pg) == 0
    assert _flagged(pg, rid, "flaky_mod"), (
        "the run that re-wrote the module but could not embed it must not clear the flag"
    )
    assert "flaky_mod" in _attention(pg, rid)

    run(pg, "embretry_99")

    assert _poison_rows(pg) > 0
    assert not _flagged(pg, rid, "flaky_mod")


def test_a_module_whose_every_chunk_is_refused_keeps_its_rows_and_is_flagged(pg, tmp_path):
    """0 usable vectors keeps the module's existing rows (no destructive rewrite)
    and still sends it back for a re-embed."""
    repo = _two_modules(tmp_path, poisoned=False)
    (rid,) = register("embretry_99", repo)
    run(pg, "embretry_99")
    before = embeddings(pg, "flaky_mod")
    assert before > 0
    write_module(repo, "flaky_mod", extra_field=FLAKY_FIELD)
    repo.commit("flaky_mod: add a field")

    run(pg, "embretry_99", embedder=_FlakyEmbedder(fail_all=True))

    assert embeddings(pg, "flaky_mod") == before, "existing rows must be kept"
    assert _poison_rows(pg) == 0
    assert _flagged(pg, rid, "flaky_mod")

    run(pg, "embretry_99")

    assert _poison_rows(pg) > 0, "the next run must embed the new field"
    assert not _flagged(pg, rid, "flaky_mod")
