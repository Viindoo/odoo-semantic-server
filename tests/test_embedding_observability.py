"""Embedding observability tests (M7 C5).

Tests:
  1. FakeEmbedder.call_count increments on each embed() call.
  2. Indexing a module with an embedder increments call_count AND writes rows.
  3. Second run on unchanged HEAD skips embed entirely (incremental path).
"""
import subprocess
import textwrap
from pathlib import Path

import pytest

from src.indexer.embedder import FakeEmbedder

pytestmark = [pytest.mark.postgres, pytest.mark.neo4j]

TEST_VERSION = "99.0"


# ---------------------------------------------------------------------------
# Helpers (mirrors test_pipeline_incremental.py style)
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


def _make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "checkout", "-b", TEST_VERSION)
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "Test")
    (path / ".gitkeep").write_text("")
    _git(path, "add", ".gitkeep")
    _git(path, "commit", "-m", "init")
    return path


def _seed_module(repo: Path, name: str) -> None:
    module = repo / name
    module.mkdir(parents=True, exist_ok=True)
    (module / "__manifest__.py").write_text(
        f"{{'name': {name!r}, 'version': '{TEST_VERSION}.1.0.0', "
        f"'depends': [], 'installable': True}}\n"
    )
    models_dir = module / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / "__init__.py").write_text("")
    (models_dir / f"{name}.py").write_text(textwrap.dedent(f"""
        from odoo import models, fields

        class TestModel(models.Model):
            _name = '{name}.model'
            x = fields.Char()
    """).strip())
    _git(repo, "add", name)
    _git(repo, "commit", "-m", f"add module {name}")


# ---------------------------------------------------------------------------
# Test 1: FakeEmbedder.call_count increments
# ---------------------------------------------------------------------------

def test_embedder_call_count_increments():
    """call_count starts at 0 and increments once per embed() call."""
    embedder = FakeEmbedder(dim=16)
    assert embedder.call_count == 0

    embedder.embed(["text one"])
    assert embedder.call_count == 1

    embedder.embed(["text two"])
    assert embedder.call_count == 2

    embedder.embed(["text three"])
    assert embedder.call_count == 3


# ---------------------------------------------------------------------------
# Test 2: Indexing a module increments call_count + writes rows
# ---------------------------------------------------------------------------

@pytest.mark.postgres
@pytest.mark.neo4j
def test_index_module_increments_embedder(
    clean_neo4j, clean_pg, tmp_path,
):
    """Indexing a minimal module with an embedder increments call_count > 0
    and writes at least as many embedding rows as the module has chunks.
    """
    from src.db.migrate import _vector_extension_available, run_migrations
    from src.db.repo_registry import add_profile, add_repo
    from src.indexer.pipeline import index_profile

    run_migrations(clean_pg)
    if not _vector_extension_available(clean_pg):
        pytest.skip("pgvector extension not installed")

    repo = _make_git_repo(tmp_path / "repo_obs1")
    _seed_module(repo, "obs_module")

    pid = add_profile(clean_pg, "obs_profile1", TEST_VERSION)
    add_repo(clean_pg, pid, "file://local", TEST_VERSION, str(repo))

    embedder = FakeEmbedder(dim=1024)
    count_before = embedder.call_count

    summary = index_profile(
        clean_pg,
        profile_name="obs_profile1",
        embedder=embedder,
    )

    # Embedder must have been called at least once
    assert embedder.call_count > count_before, (
        f"Expected call_count to increase from {count_before}, "
        f"got {embedder.call_count}"
    )

    # Embeddings must have been written to Postgres
    n_chunks = summary.get("embeddings", 0)
    with clean_pg.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM embeddings WHERE odoo_version = %s",
            (TEST_VERSION,),
        )
        stored = cur.fetchone()[0]
    assert stored >= n_chunks, (
        f"Expected at least {n_chunks} embedding rows, found {stored}"
    )
    assert stored > 0, "At least one embedding row must be written"


# ---------------------------------------------------------------------------
# Test 3: Re-index on unchanged HEAD does not re-embed (incremental skip)
# ---------------------------------------------------------------------------

@pytest.mark.postgres
@pytest.mark.neo4j
def test_reindex_skip_does_not_re_embed(
    clean_neo4j, clean_pg, tmp_path,
):
    """Second index_profile call with unchanged HEAD must skip embed entirely.

    The incremental indexer detects HEAD == stored head_sha and returns early
    before any embedding happens. call_count delta on the second run must be 0.
    """
    from src.db.migrate import _vector_extension_available, run_migrations
    from src.db.repo_registry import add_profile, add_repo
    from src.indexer.pipeline import index_profile

    run_migrations(clean_pg)
    if not _vector_extension_available(clean_pg):
        pytest.skip("pgvector extension not installed")

    repo = _make_git_repo(tmp_path / "repo_obs2")
    _seed_module(repo, "obs_skip_module")

    pid = add_profile(clean_pg, "obs_profile2", TEST_VERSION)
    add_repo(clean_pg, pid, "file://local", TEST_VERSION, str(repo))

    embedder = FakeEmbedder(dim=1024)

    # First run — must embed
    index_profile(clean_pg, profile_name="obs_profile2", embedder=embedder)
    count_after_first = embedder.call_count
    assert count_after_first > 0, "First run must make at least one embed call"

    # Second run — HEAD unchanged → incremental skip → zero embed calls
    index_profile(clean_pg, profile_name="obs_profile2", embedder=embedder)
    count_after_second = embedder.call_count

    delta = count_after_second - count_after_first
    assert delta == 0, (
        f"Second run on unchanged HEAD must not call embed(). "
        f"Got delta={delta} (count went {count_after_first} → {count_after_second})"
    )
