"""Pipeline incremental indexer integration tests (M6 W2-4).

Tests verify the incremental indexing contract:
  - First run sets head_sha in DB
  - Identical second run skips (zero-cost)
  - Changed modules triggers targeted re-index
  - Force-push falls back to full reindex
  - --full flag bypasses skip
  - Partial failure preserves head_sha (rollback semantics)
"""
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from src.db.migrate import run_migrations
from src.db.repo_registry import add_profile, add_repo
from src.indexer.pipeline import index_profile
from tests.conftest import TEST_VERSION, make_manifest

pytestmark = [pytest.mark.postgres, pytest.mark.neo4j]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> None:
    """Run a git command in repo, raising on failure."""
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


def _make_git_repo_with_commit(path: Path, branch: str = TEST_VERSION) -> Path:
    """Create git repo, configure minimal user identity, initial empty commit."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "checkout", "-b", branch)
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "Test")
    # Initial commit so HEAD exists
    (path / ".gitkeep").write_text("")
    _git(path, "add", ".gitkeep")
    _git(path, "commit", "-m", "init")
    return path


def _seed_module(repo: Path, name: str) -> None:
    """Create a minimal Odoo module and commit it."""
    module = repo / name
    make_manifest(module, name=name, version=f"{TEST_VERSION}.1.0.0", depends=[])
    (module / "models").mkdir(parents=True, exist_ok=True)
    (module / "models" / "__init__.py").write_text("")
    (module / "models" / f"{name}.py").write_text(textwrap.dedent(f"""
        from odoo import models, fields

        class FooModel(models.Model):
            _name = '{name}.foo'
            x = fields.Char()
    """).strip())
    _git(repo, "add", name)
    _git(repo, "commit", "-m", f"add module {name}")


def _get_head(repo: Path) -> str:
    """Return current HEAD sha."""
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Test 1: First run (no stored head_sha) → full reindex + sets head_sha
# ---------------------------------------------------------------------------

def test_first_run_full_reindex_sets_head_sha(
    clean_neo4j, clean_pg, neo4j_driver, tmp_path,
):
    """First run with no stored head_sha → full reindex; head_sha recorded."""
    run_migrations(clean_pg)
    repo = _make_git_repo_with_commit(tmp_path / "repo1", branch=TEST_VERSION)
    _seed_module(repo, "mod_alpha")
    expected_sha = _get_head(repo)

    pid = add_profile(clean_pg, "prof1", TEST_VERSION)
    add_repo(clean_pg, pid, "file://local", TEST_VERSION, str(repo))

    # Sanity: head_sha is NULL before first run
    with clean_pg.cursor() as cur:
        cur.execute("SELECT head_sha FROM repos WHERE profile_id = %s", (pid,))
        assert cur.fetchone()[0] is None

    summary = index_profile(clean_pg, profile_name="prof1")
    assert summary["modules"] >= 1

    # head_sha must be set to current HEAD
    with clean_pg.cursor() as cur:
        cur.execute("SELECT head_sha FROM repos WHERE profile_id = %s", (pid,))
        stored_sha = cur.fetchone()[0]
    assert stored_sha == expected_sha

    # Verify Neo4j Module node was written
    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (m:Model {odoo_version: $v}) RETURN m LIMIT 1",
            v=TEST_VERSION,
        ).single()
    assert rec is not None, "First run must write Model nodes to Neo4j"


# ---------------------------------------------------------------------------
# Test 2: Second run with same HEAD → skip (zero-cost)
# ---------------------------------------------------------------------------

def test_second_run_unchanged_skips(
    clean_neo4j, clean_pg, neo4j_driver, tmp_path, caplog,
):
    """Second run with identical HEAD → logged skip, no extra writes."""
    import logging
    run_migrations(clean_pg)
    repo = _make_git_repo_with_commit(tmp_path / "repo2", branch=TEST_VERSION)
    _seed_module(repo, "mod_beta")

    pid = add_profile(clean_pg, "prof2", TEST_VERSION)
    add_repo(clean_pg, pid, "file://local", TEST_VERSION, str(repo))

    # First run — establishes head_sha
    index_profile(clean_pg, profile_name="prof2")

    # Count Module nodes after first run
    with neo4j_driver.session() as session:
        count_before = session.run(
            "MATCH (m:Model {odoo_version: $v}) RETURN count(m) AS n",
            v=TEST_VERSION,
        ).single()["n"]

    # Second run — HEAD unchanged → should skip
    with caplog.at_level(logging.INFO, logger="src.indexer.pipeline"):
        summary2 = index_profile(clean_pg, profile_name="prof2")

    assert summary2["modules"] == 0, "Skipped run must report 0 modules"
    assert "skipping reindex" in caplog.text.lower() or "unchanged" in caplog.text.lower()

    # Node count must be identical (no extra writes)
    with neo4j_driver.session() as session:
        count_after = session.run(
            "MATCH (m:Model {odoo_version: $v}) RETURN count(m) AS n",
            v=TEST_VERSION,
        ).single()["n"]
    assert count_after == count_before


# ---------------------------------------------------------------------------
# Test 3: One module changed → incremental re-index of changed module only
# ---------------------------------------------------------------------------

def test_second_run_one_module_changed(
    clean_neo4j, clean_pg, neo4j_driver, tmp_path, caplog,
):
    """After changing 1 of 2 modules, incremental run indexes only that module."""
    import logging
    run_migrations(clean_pg)
    repo = _make_git_repo_with_commit(tmp_path / "repo3", branch=TEST_VERSION)
    _seed_module(repo, "mod_unchanged")
    _seed_module(repo, "mod_changed")

    pid = add_profile(clean_pg, "prof3", TEST_VERSION)
    add_repo(clean_pg, pid, "file://local", TEST_VERSION, str(repo))

    # First run
    summary1 = index_profile(clean_pg, profile_name="prof3")
    assert summary1["modules"] >= 2

    # Modify only mod_changed
    model_file = repo / "mod_changed" / "models" / "mod_changed.py"
    model_file.write_text(textwrap.dedent("""
        from odoo import models, fields

        class FooModel(models.Model):
            _name = 'mod_changed.foo'
            x = fields.Char()
            y = fields.Integer()  # new field added
    """).strip())
    _git(repo, "add", "mod_changed")
    _git(repo, "commit", "-m", "add field y to mod_changed")

    with caplog.at_level(logging.INFO, logger="src.indexer.pipeline"):
        summary2 = index_profile(clean_pg, profile_name="prof3")

    # Only 1 module should be re-indexed (the changed one)
    assert summary2["modules"] == 1
    assert "incremental" in caplog.text.lower()

    # head_sha updated to new HEAD
    new_sha = _get_head(repo)
    with clean_pg.cursor() as cur:
        cur.execute("SELECT head_sha FROM repos WHERE profile_id = %s", (pid,))
        stored = cur.fetchone()[0]
    assert stored == new_sha


# ---------------------------------------------------------------------------
# Test 4: Force-push → log warning + full reindex
# ---------------------------------------------------------------------------

def test_force_push_falls_back_to_full(
    clean_neo4j, clean_pg, neo4j_driver, tmp_path, caplog,
):
    """Force-push (history rewrite) detected → full reindex, warning logged."""
    import logging
    run_migrations(clean_pg)
    repo = _make_git_repo_with_commit(tmp_path / "repo4", branch=TEST_VERSION)
    _seed_module(repo, "mod_gamma")

    pid = add_profile(clean_pg, "prof4", TEST_VERSION)
    add_repo(clean_pg, pid, "file://local", TEST_VERSION, str(repo))

    # First run
    index_profile(clean_pg, profile_name="prof4")
    sha_after_first = _get_head(repo)

    # Simulate force-push: reset to initial commit + new commit (rewrites history)
    # Get initial commit sha (parent of current)
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD~1"],
        capture_output=True, text=True, check=True,
    )
    parent_sha = result.stdout.strip()

    _git(repo, "reset", "--hard", parent_sha)
    # Make a new diverging commit
    (repo / "diverge.txt").write_text("diverged")
    _git(repo, "add", "diverge.txt")
    _git(repo, "commit", "-m", "diverging commit after reset")
    new_sha = _get_head(repo)

    assert new_sha != sha_after_first, "History should have diverged"

    with caplog.at_level(logging.WARNING, logger="src.indexer.pipeline"):
        index_profile(clean_pg, profile_name="prof4")

    assert "force-push" in caplog.text.lower() or "history rewrite" in caplog.text.lower()
    # Full reindex should proceed (modules >= 0, and head_sha updated)
    with clean_pg.cursor() as cur:
        cur.execute("SELECT head_sha FROM repos WHERE profile_id = %s", (pid,))
        stored = cur.fetchone()[0]
    assert stored == new_sha


# ---------------------------------------------------------------------------
# Test 5: --full flag bypasses skip-unchanged
# ---------------------------------------------------------------------------

def test_full_flag_bypasses_skip(
    clean_neo4j, clean_pg, neo4j_driver, tmp_path, caplog,
):
    """With full_reindex=True, second run proceeds even when HEAD unchanged."""
    import logging
    run_migrations(clean_pg)
    repo = _make_git_repo_with_commit(tmp_path / "repo5", branch=TEST_VERSION)
    _seed_module(repo, "mod_delta")

    pid = add_profile(clean_pg, "prof5", TEST_VERSION)
    add_repo(clean_pg, pid, "file://local", TEST_VERSION, str(repo))

    # First run
    index_profile(clean_pg, profile_name="prof5")

    # Second run with full_reindex=True — should NOT skip
    with caplog.at_level(logging.INFO, logger="src.indexer.pipeline"):
        summary2 = index_profile(clean_pg, profile_name="prof5", full_reindex=True)

    # full_reindex=True → processes all modules (not 0)
    assert summary2["modules"] >= 1, "full_reindex=True must not skip"
    assert "skipping reindex" not in caplog.text.lower()

    # head_sha should still equal current HEAD
    sha = _get_head(repo)
    with clean_pg.cursor() as cur:
        cur.execute("SELECT head_sha FROM repos WHERE profile_id = %s", (pid,))
        stored = cur.fetchone()[0]
    assert stored == sha


# ---------------------------------------------------------------------------
# Tests 5b + 5c: Module rename — ADR-0007 D5 enforcement
#
# ADR-0007 D5 (accepted trade-off):
#   Incremental run after rename → old Module node stays (stale orphan).
#   --full run after rename → old Module node STILL stays (no gc code yet).
#   Cleanup is deferred to M7 via a future --gc flag.
#
# These tests lock in the documented behavior so future changes that
# silently alter the trade-off surface in CI.
# ---------------------------------------------------------------------------

def _setup_rename_repo(tmp_path: Path, profile_suffix: str, pg_conn):
    """Shared setup for module-rename tests.

    Creates a git repo with module 'mod_foo', registers it, runs first
    index_profile, then renames the module dir to 'mod_bar' and commits.

    Returns (pid, repo_path) ready for the second index_profile call.
    """
    run_migrations(pg_conn)
    repo = _make_git_repo_with_commit(
        tmp_path / f"repo_rename_{profile_suffix}", branch=TEST_VERSION
    )

    # Create 'mod_foo' module with a trivial model so Neo4j gets a Module node
    _seed_module(repo, "mod_foo")

    prof_name = f"prof_rename_{profile_suffix}"
    pid = add_profile(pg_conn, prof_name, TEST_VERSION)
    add_repo(pg_conn, pid, "file://local", TEST_VERSION, str(repo))

    # First run — establishes mod_foo Module node in Neo4j
    summary1 = index_profile(pg_conn, profile_name=prof_name)
    assert summary1["modules"] >= 1, "First run must index mod_foo"

    # git mv mod_foo → mod_bar and commit
    _git(repo, "mv", "mod_foo", "mod_bar")
    _git(repo, "commit", "-m", "rename mod_foo to mod_bar")

    return pid, repo, prof_name


def _neo4j_module_exists(neo4j_driver, name: str) -> bool:
    """True if a Module node with the given name + TEST_VERSION exists in Neo4j."""
    with neo4j_driver.session() as session:
        rec = session.run(
            "MATCH (m:Module {name: $name, odoo_version: $v}) RETURN m LIMIT 1",
            name=name, v=TEST_VERSION,
        ).single()
    return rec is not None


def test_module_rename_leaves_stale_neo4j_node(
    clean_neo4j, clean_pg, neo4j_driver, tmp_path,
):
    """ADR-0007 D5: incremental run after module rename → stale Module node remains.

    'mod_foo' is renamed to 'mod_bar'. The incremental diff sees both paths as
    changed; the scanner finds 'mod_bar' (new) but not 'mod_foo' (dir gone).
    Neo4j MERGE only writes what the scanner returns — stale 'mod_foo' node is
    never deleted. This is the documented accepted trade-off (see ADR-0007 D5).

    This test enforces that behavior so any future refactor that silently
    auto-cleans the stale node (consuming scarce Neo4j write cycles) surfaces.
    """
    pid, repo, prof_name = _setup_rename_repo(tmp_path, "a", clean_pg)

    # Second run — incremental (no --full)
    summary2 = index_profile(clean_pg, profile_name=prof_name)
    assert summary2["modules"] >= 1, "Second run must index mod_bar"

    # Guard: confirm incremental diff-filter ran (not the unchanged-skip path).
    # repos_skipped == 0 because the rename advanced the head_sha, triggering re-index.
    assert summary2.get("repos_skipped", 0) == 0, (
        f"expected diff-filter path, got summary2={summary2}"
    )

    # mod_bar must be indexed (new path picked up by scanner)
    assert _neo4j_module_exists(neo4j_driver, "mod_bar"), (
        "mod_bar Module node must exist after incremental run following rename"
    )

    # mod_foo must STILL exist — stale orphan, NOT cleaned up by incremental run
    assert _neo4j_module_exists(neo4j_driver, "mod_foo"), (
        "mod_foo Module node must remain as stale orphan after incremental rename run "
        "(ADR-0007 D5 accepted trade-off; cleanup deferred to M7 --gc flag)"
    )


def test_module_rename_full_flag_still_leaves_stale(
    clean_neo4j, clean_pg, neo4j_driver, tmp_path,
):
    """ADR-0007 D5 + D4: --full reindex after rename indexes new module but does NOT
    remove the stale Module node (no gc code exists; deferred to M7 --gc flag).

    This test locks in the current behavior: --full is NOT a cleanup mechanism
    for stale Module nodes (only a bypass of the diff-filter / skip-unchanged
    logic). Stale orphan 'mod_foo' must persist even after --full.

    If M7 implements --gc and this test starts failing, update the assertion
    here to reflect the new documented behavior.
    """
    pid, repo, prof_name = _setup_rename_repo(tmp_path, "b", clean_pg)

    # Second run — forced full reindex (bypasses skip + diff filter)
    summary2 = index_profile(clean_pg, profile_name=prof_name, full_reindex=True)
    assert summary2["modules"] >= 1, "--full run must index mod_bar"

    # mod_bar must be indexed (scanner found the new path)
    assert _neo4j_module_exists(neo4j_driver, "mod_bar"), (
        "mod_bar Module node must exist after --full run following rename"
    )

    # mod_foo must STILL exist — --full does not delete stale Module nodes
    # (ADR-0007 D5 defers gc to M7; pipeline uses MERGE, never DELETE on Module)
    assert _neo4j_module_exists(neo4j_driver, "mod_foo"), (
        "mod_foo Module node must remain as stale orphan after --full run "
        "(ADR-0007 D5: gc deferred to M7 --gc flag; current pipeline never DELETEs Module nodes)"
    )


# ---------------------------------------------------------------------------
# Test 6: Partial failure preserves head_sha
# ---------------------------------------------------------------------------

def test_partial_failure_preserves_head_sha(
    clean_neo4j, clean_pg, neo4j_driver, tmp_path,
):
    """If writer raises mid-run, head_sha must NOT advance (rollback semantics)."""
    run_migrations(clean_pg)
    repo = _make_git_repo_with_commit(tmp_path / "repo6", branch=TEST_VERSION)
    _seed_module(repo, "mod_epsilon")

    pid = add_profile(clean_pg, "prof6", TEST_VERSION)
    add_repo(clean_pg, pid, "file://local", TEST_VERSION, str(repo))

    # First run succeeds → records head_sha
    index_profile(clean_pg, profile_name="prof6")
    sha_after_first = _get_head(repo)

    with clean_pg.cursor() as cur:
        cur.execute("SELECT head_sha FROM repos WHERE profile_id = %s", (pid,))
        stored_after_first = cur.fetchone()[0]
    assert stored_after_first == sha_after_first

    # Add a commit inside the module dir so incremental filter passes through
    # to write_results (a root-level file change would be filtered as no module changed)
    model_file = repo / "mod_epsilon" / "models" / "mod_epsilon.py"
    model_file.write_text(textwrap.dedent("""
        from odoo import models, fields

        class FooModel(models.Model):
            _name = 'mod_epsilon.foo'
            x = fields.Char()
            z = fields.Boolean()  # new field
    """).strip())
    _git(repo, "add", "mod_epsilon")
    _git(repo, "commit", "-m", "modify mod_epsilon to trigger re-index")
    sha_after_second_commit = _get_head(repo)
    assert sha_after_second_commit != sha_after_first

    # Patch writer.write_results to raise — simulates mid-write failure
    from src.indexer.writer_neo4j import Neo4jWriter

    def failing_write_results(self, *args, **kwargs):
        raise RuntimeError("Simulated write failure mid-run")

    with patch.object(Neo4jWriter, "write_results", failing_write_results):
        with pytest.raises(Exception):
            index_profile(clean_pg, profile_name="prof6")

    # head_sha must still be sha_after_first (NOT advanced to sha_after_second_commit)
    with clean_pg.cursor() as cur:
        cur.execute("SELECT head_sha FROM repos WHERE profile_id = %s", (pid,))
        stored_after_failure = cur.fetchone()[0]

    assert stored_after_failure == sha_after_first, (
        f"head_sha must not advance on partial failure. "
        f"Expected {sha_after_first[:8]!r}, got {(stored_after_failure or '')[:8]!r}"
    )
