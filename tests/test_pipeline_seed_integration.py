# SPDX-License-Identifier: AGPL-3.0-or-later
"""Auto-reseed pattern catalogue at end of index_profile (M6 W2-7)."""
import json
import textwrap
from pathlib import Path

import pytest

from src.db.migrate import run_migrations
from src.db.pg import repo_store
from src.indexer.pipeline import index_profile
from tests.conftest import (
    TEST_VERSION,
    make_git_repo,
    make_manifest,
)

pytestmark = [pytest.mark.postgres, pytest.mark.neo4j]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WORKTREE = Path(__file__).resolve().parent.parent


def _seed_minimal_module(repo: Path, name: str) -> None:
    """Create a minimal Odoo module under repo/<name>."""
    module = repo / name
    make_manifest(module, name=name, version=f"{TEST_VERSION}.1.0.0", depends=[])
    (module / "models").mkdir()
    (module / "models" / "__init__.py").write_text("")
    (module / "models" / f"{name}.py").write_text(textwrap.dedent(f"""
        from odoo import models, fields

        class Foo(models.Model):
            _name = '{name}.foo'
            x = fields.Char()
    """).strip())


def _make_minimal_patterns_json(tmp_path: Path) -> Path:
    """Write a tiny patterns.json with a single test entry."""
    data = [
        {
            "pattern_id": "pipeline-seed-test-pat",
            "intent_keywords": ["test"],
            "file_ref": "f:1",
            "snippet_text": "# test snippet",
            "gotchas": ["gotcha one", "gotcha two", "gotcha three"],
            "odoo_version_min": TEST_VERSION,
            "language": "python",
        }
    ]
    p = tmp_path / "patterns.json"
    p.write_text(json.dumps(data))
    return p


def _get_seed_meta_sha(neo4j_driver) -> str | None:
    """Return sha256 stored on _SeedMeta patterns_neo4j sentinel, or None.

    Per ADR-0007 D6-split: reads the patterns_neo4j key (written after Neo4j
    PatternExample nodes are seeded).  The old key='patterns' is no longer written.
    """
    with neo4j_driver.session() as session:
        row = session.run(
            "MATCH (s:_SeedMeta {key: 'patterns_neo4j'}) RETURN s.sha256 AS sha LIMIT 1"
        ).single()
        return row["sha"] if row else None


def _wipe_seed_meta(neo4j_driver) -> None:
    """Remove all _SeedMeta sentinel nodes so each test starts clean.

    Per ADR-0007 D6-split: wipes all keys (patterns_neo4j, patterns_pgvector,
    legacy 'patterns') to ensure a clean state for each test.
    """
    with neo4j_driver.session() as session:
        session.run("MATCH (s:_SeedMeta) DELETE s")


def _count_pattern_examples(neo4j_driver) -> int:
    """Count PatternExample nodes in Neo4j."""
    with neo4j_driver.session() as session:
        row = session.run(
            "MATCH (p:PatternExample) RETURN count(p) AS n"
        ).single()
        return row["n"] if row else 0


# ---------------------------------------------------------------------------
# Test 1: index_profile auto-seeds PatternExample nodes
# ---------------------------------------------------------------------------


def test_index_profile_seeds_patterns(
    clean_neo4j, clean_pg, neo4j_driver, tmp_path, monkeypatch
):
    """After index_profile completes, PatternExample nodes exist in Neo4j."""
    # Patch the default patterns file to our minimal test file.
    # After FIX-5, pipeline no longer references _DEFAULT_PATTERNS_FILE directly;
    # patching at seed_patterns module level is sufficient.
    patterns_file = _make_minimal_patterns_json(tmp_path)
    import src.indexer.seed_patterns as _sp_mod
    monkeypatch.setattr(_sp_mod, "_DEFAULT_PATTERNS_FILE", patterns_file)

    # Clean sentinel so gating does not skip.
    _wipe_seed_meta(neo4j_driver)

    run_migrations(clean_pg)
    repo = make_git_repo(tmp_path / "repo_seed", branch=TEST_VERSION)
    _seed_minimal_module(repo, "seed_mod")
    pid = repo_store().add_profile("seed_prof", TEST_VERSION)
    repo_store().add_repo(pid, "local/seed", TEST_VERSION, str(repo))

    # index_profile should complete and auto-reseed.
    summary = index_profile(clean_pg, profile_name="seed_prof")
    assert summary["modules"] >= 1

    # PatternExample nodes must exist after the run.
    count = _count_pattern_examples(neo4j_driver)
    assert count >= 1, f"Expected ≥1 PatternExample node after index_profile, got {count}"

    # Sentinel must be set.
    sha = _get_seed_meta_sha(neo4j_driver)
    assert sha is not None, "_SeedMeta sentinel not written after auto-reseed"

    # Cleanup sentinel so it doesn't bleed into other tests.
    _wipe_seed_meta(neo4j_driver)


# ---------------------------------------------------------------------------
# Test 2: second run skips reseed (sentinel unchanged)
# ---------------------------------------------------------------------------


def test_index_profile_skips_seed_when_unchanged(
    clean_neo4j, clean_pg, neo4j_driver, tmp_path, monkeypatch, caplog
):
    """Second index_profile run logs 'patterns unchanged' and does not re-write nodes."""
    patterns_file = _make_minimal_patterns_json(tmp_path)
    import src.indexer.seed_patterns as _sp_mod
    monkeypatch.setattr(_sp_mod, "_DEFAULT_PATTERNS_FILE", patterns_file)

    _wipe_seed_meta(neo4j_driver)

    run_migrations(clean_pg)
    repo = make_git_repo(tmp_path / "repo_skip", branch=TEST_VERSION)
    _seed_minimal_module(repo, "skip_mod")
    pid = repo_store().add_profile("skip_prof", TEST_VERSION)
    repo_store().add_repo(pid, "local/skip", TEST_VERSION, str(repo))

    # First run — seeds patterns and sets sentinel.
    index_profile(clean_pg, profile_name="skip_prof")
    sha_after_first = _get_seed_meta_sha(neo4j_driver)
    assert sha_after_first is not None

    # Second run — patterns file unchanged → should skip.
    caplog.clear()
    import logging
    with caplog.at_level(logging.INFO, logger="src.indexer.pipeline"):
        index_profile(clean_pg, profile_name="skip_prof")

    assert "unchanged" in caplog.text.lower(), (
        f"Expected 'unchanged' in log output on second run.\nLog:\n{caplog.text}"
    )

    # Sentinel sha must be identical after second run.
    sha_after_second = _get_seed_meta_sha(neo4j_driver)
    assert sha_after_second == sha_after_first, (
        "Sentinel sha changed on second run — sentinel was re-written even though "
        "patterns.json was not modified."
    )

    _wipe_seed_meta(neo4j_driver)


# ---------------------------------------------------------------------------
# Test 3: --no-embed (embedder=None) skips pattern pgvector embeddings
# ---------------------------------------------------------------------------


def test_index_profile_no_embed_skips_pattern_embeddings(
    clean_neo4j, clean_pg, neo4j_driver, tmp_path, monkeypatch, caplog
):
    """When embedder=None, PatternExample Neo4j nodes are written but embedding skipped."""
    patterns_file = _make_minimal_patterns_json(tmp_path)
    import src.indexer.seed_patterns as _sp_mod
    monkeypatch.setattr(_sp_mod, "_DEFAULT_PATTERNS_FILE", patterns_file)

    _wipe_seed_meta(neo4j_driver)

    run_migrations(clean_pg)

    # Capture pre-index pattern_example count — clean_pg fixture doesn't wipe the
    # embeddings table, so prior tests in this file may have left rows. We assert
    # NO NEW rows are added by this test (rather than absolute count == 0).
    from src.db.migrate import _vector_extension_available
    pgvector_available = _vector_extension_available(clean_pg)
    pre_count = 0
    if pgvector_available:
        with clean_pg.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM embeddings "
                "WHERE chunk_type = 'pattern_example' AND module = '__patterns__'"
            )
            pre_count = cur.fetchone()[0]

    repo = make_git_repo(tmp_path / "repo_noembed", branch=TEST_VERSION)
    _seed_minimal_module(repo, "noembed_mod")
    pid = repo_store().add_profile("noembed_prof", TEST_VERSION)
    repo_store().add_repo(pid, "local/noembed", TEST_VERSION, str(repo))

    import logging
    with caplog.at_level(logging.INFO, logger="src.indexer.pipeline"):
        # embedder=None — must skip embedding step.
        summary = index_profile(clean_pg, profile_name="noembed_prof", embedder=None)

    assert summary["modules"] >= 1

    # Neo4j PatternExample nodes must exist.
    count = _count_pattern_examples(neo4j_driver)
    assert count >= 1, (
        f"Expected ≥1 PatternExample node even with embedder=None, got {count}"
    )

    # Embedding skip notice must appear in log.
    assert "skip" in caplog.text.lower(), (
        f"Expected skip notice in log for embedder=None.\nLog:\n{caplog.text}"
    )

    # NEW: verify NO new pattern embeddings added (delta == 0 when embedder=None)
    if pgvector_available:
        with clean_pg.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM embeddings "
                "WHERE chunk_type = 'pattern_example' AND module = '__patterns__'"
            )
            post_count = cur.fetchone()[0]
        assert post_count == pre_count, (
            f"Pattern embeddings must not be written when embedder=None "
            f"(pre={pre_count}, post={post_count}, delta={post_count - pre_count})"
        )

    _wipe_seed_meta(neo4j_driver)


# ---------------------------------------------------------------------------
# Test 4: seed failure does not fail the whole indexer run
# ---------------------------------------------------------------------------


def test_seed_failure_does_not_fail_indexer(
    clean_neo4j, clean_pg, neo4j_driver, tmp_path, monkeypatch, caplog
):
    """If auto-reseed raises, index_profile still completes successfully."""
    # Monkeypatch _compute_patterns_sha256 to raise so the entire seed block fails.
    import src.indexer.seed_patterns as _sp_mod

    def _boom(path):
        raise RuntimeError("injected seed failure")

    monkeypatch.setattr(_sp_mod, "_compute_patterns_sha256", _boom)

    _wipe_seed_meta(neo4j_driver)

    run_migrations(clean_pg)
    repo = make_git_repo(tmp_path / "repo_fail", branch=TEST_VERSION)
    _seed_minimal_module(repo, "fail_mod")
    pid = repo_store().add_profile("fail_prof", TEST_VERSION)
    repo_store().add_repo(pid, "local/fail", TEST_VERSION, str(repo))

    import logging
    with caplog.at_level(logging.WARNING, logger="src.indexer.pipeline"):
        # Must NOT raise — failure is absorbed.
        summary = index_profile(clean_pg, profile_name="fail_prof")

    assert summary["modules"] >= 1, (
        "index_profile must still return a valid summary when auto-reseed fails"
    )

    # Warning must be logged.
    assert "auto-reseed" in caplog.text.lower() or "reseed" in caplog.text.lower(), (
        f"Expected auto-reseed warning in logs.\nLog:\n{caplog.text}"
    )

    _wipe_seed_meta(neo4j_driver)
