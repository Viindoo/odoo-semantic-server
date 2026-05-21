# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-profile parallel indexing via --profile-workers / ThreadPoolExecutor (M6 W2-8)."""
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.db.migrate import run_migrations
from src.db.pg import repo_store

pytestmark = [pytest.mark.postgres, pytest.mark.neo4j]

TEST_VERSION = "99.0"
TEST_VERSION_ALT = "98.0"  # second version for isolation test


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_profile(name: str) -> dict:
    return {"name": name, "odoo_version": TEST_VERSION}


def _make_repo(repo_id: int, local_path: str = "/fake/repo") -> dict:
    return {
        "id": repo_id,
        "local_path": local_path,
        "odoo_version": TEST_VERSION,
        "url": "file://local",
        "branch": TEST_VERSION,
    }


def _fake_counters(modules: int = 1) -> dict:
    return {
        "modules": modules,
        "views": 0,
        "qweb": 0,
        "embeddings": 0,
        "js_patches": 0,
        "owl_comps": 0,
    }


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_pg_conn():
    """Top-level mock psycopg2 connection passed to index_all."""
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = (True,)  # advisory lock acquired
    conn.cursor.return_value = cur
    return conn


def _make_thread_pg():
    """Factory for per-thread mock pg connections (opened by each worker)."""
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = (True,)  # advisory lock acquired
    conn.cursor.return_value = cur
    return conn


@pytest.fixture
def mock_writer():
    w = MagicMock()
    w.setup_indexes.return_value = None
    w.close.return_value = None
    return w


# ---------------------------------------------------------------------------
# Test 1: two profiles indexed in parallel, totals correct
# ---------------------------------------------------------------------------

class TestTwoProfilesParallel:
    def test_two_profiles_parallel_complete(self, mock_pg_conn, mock_writer):
        """profile_workers=2 → both profiles indexed; aggregated Module count correct."""
        profiles = [_make_profile("alpha"), _make_profile("beta")]
        repos_alpha = [_make_repo(1)]
        repos_beta = [_make_repo(2)]

        def fake_get_repos(profile_name):
            return repos_alpha if profile_name == "alpha" else repos_beta

        def fake_index_repo(
            repo, writer, pg_conn=None, embedder=None, progress=False, full_reindex=False,
            gc=False, ancestor_profiles=None,
        ):
            # repo id 1 → 3 modules, repo id 2 → 5 modules
            return _fake_counters(modules=3 if repo["id"] == 1 else 5)

        mock_store = MagicMock()
        mock_store.list_profiles.return_value = profiles
        mock_store.get_repos_for_profile.side_effect = fake_get_repos
        mock_store.get_ancestor_profile_names.side_effect = lambda pn: [pn]

        with (
            patch("src.indexer.pipeline.repo_store", return_value=mock_store),
            patch("src.indexer.pipeline.Neo4jWriter", return_value=mock_writer),
            patch("src.indexer.pipeline._neo4j_creds", return_value=("bolt://x", "u", "p")),
            patch("src.indexer.pipeline._index_repo", side_effect=fake_index_repo),
            patch("src.indexer.pipeline.open_production_pg", side_effect=_make_thread_pg),
        ):
            from src.indexer.pipeline import index_all
            result = index_all(mock_pg_conn, profile_workers=2)

        assert result["profiles_ok"] == 2
        assert result["profiles_failed"] == []
        assert result["modules"] == 8  # 3 + 5


# ---------------------------------------------------------------------------
# Test 2: degenerate — 1 profile with profile_workers=2 still works
# ---------------------------------------------------------------------------

class TestOneProfileWithProfileWorkers2:
    def test_one_profile_with_profile_workers_2(self, mock_pg_conn, mock_writer):
        """Degenerate: 1 profile + profile_workers=2 completes without error."""
        profiles = [_make_profile("solo")]
        repos = [_make_repo(10)]

        mock_store = MagicMock()
        mock_store.list_profiles.return_value = profiles
        mock_store.get_repos_for_profile.return_value = repos

        with (
            patch("src.indexer.pipeline.repo_store", return_value=mock_store),
            patch("src.indexer.pipeline.Neo4jWriter", return_value=mock_writer),
            patch("src.indexer.pipeline._neo4j_creds", return_value=("bolt://x", "u", "p")),
            patch("src.indexer.pipeline._index_repo", return_value=_fake_counters(modules=7)),
            patch("src.indexer.pipeline.open_production_pg", side_effect=_make_thread_pg),
        ):
            from src.indexer.pipeline import index_all
            result = index_all(mock_pg_conn, profile_workers=2)

        assert result["profiles_ok"] == 1
        assert result["profiles_failed"] == []
        assert result["modules"] == 7


# ---------------------------------------------------------------------------
# Test 3: failure in one profile doesn't block others; exception propagates
# ---------------------------------------------------------------------------

class TestProfileFailureDoesNotBlockOthers:
    def test_profile_failure_does_not_block_others(self, mock_pg_conn, mock_writer):
        """profile_workers=2: profile 'bad' raises → 'good' still completes; index_all raises."""
        profiles = [_make_profile("good"), _make_profile("bad")]
        repos_good = [_make_repo(1)]
        repos_bad = [_make_repo(2)]

        good_indexed = threading.Event()

        def fake_get_repos(profile_name):
            return repos_good if profile_name == "good" else repos_bad

        def fake_index_repo(
            repo, writer, pg_conn=None, embedder=None, progress=False, full_reindex=False,
            gc=False, ancestor_profiles=None,
        ):
            if repo["id"] == 2:
                raise RuntimeError("simulated failure for profile 'bad'")
            good_indexed.set()
            return _fake_counters(modules=4)

        mock_store = MagicMock()
        mock_store.list_profiles.return_value = profiles
        mock_store.get_repos_for_profile.side_effect = fake_get_repos
        mock_store.get_ancestor_profile_names.side_effect = lambda pn: [pn]

        with (
            patch("src.indexer.pipeline.repo_store", return_value=mock_store),
            patch("src.indexer.pipeline.Neo4jWriter", return_value=mock_writer),
            patch("src.indexer.pipeline._neo4j_creds", return_value=("bolt://x", "u", "p")),
            patch("src.indexer.pipeline._index_repo", side_effect=fake_index_repo),
            patch("src.indexer.pipeline.open_production_pg", side_effect=_make_thread_pg),
        ):
            from src.indexer.pipeline import index_all
            with pytest.raises(RuntimeError) as exc_info:
                index_all(mock_pg_conn, profile_workers=2)

        assert "simulated failure" in str(exc_info.value), (
            "first_exc should be the bad profile's exception"
        )

        # 'good' profile should have been indexed
        assert good_indexed.is_set(), (
            "profile 'good' should complete even when profile 'bad' failed"
        )


# ---------------------------------------------------------------------------
# Test 3b: full_reindex propagates through parallel profile workers
# ---------------------------------------------------------------------------

class TestProfileWorkersFullReindex:
    """Cross-impact W2-4 ↔ W2-8: full_reindex propagates through parallel path."""

    def test_full_reindex_passes_through_parallel_workers(self, mock_pg_conn, mock_writer):
        profiles = [_make_profile("p1"), _make_profile("p2")]
        seen_full_reindex: list[bool] = []

        def fake_index_profile(
            pg_conn, *, profile_name, embedder, progress, max_workers, full_reindex=False,
            gc=False,
        ):
            seen_full_reindex.append(full_reindex)
            return _fake_counters(modules=1)

        mock_store = MagicMock()
        mock_store.list_profiles.return_value = profiles

        with (
            patch("src.indexer.pipeline.repo_store", return_value=mock_store),
            patch("src.indexer.pipeline.index_profile", side_effect=fake_index_profile),
            patch("src.indexer.pipeline.Neo4jWriter", return_value=mock_writer),
            patch("src.indexer.pipeline._neo4j_creds", return_value=("bolt://x", "u", "p")),
            patch("src.indexer.pipeline.open_production_pg", side_effect=_make_thread_pg),
        ):
            from src.indexer.pipeline import index_all
            result = index_all(
                mock_pg_conn,
                profile_workers=2,
                full_reindex=True,
            )

        assert result["profiles_ok"] == 2
        assert len(seen_full_reindex) == 2
        assert all(seen_full_reindex), (
            "full_reindex must propagate to all parallel profile workers"
        )


# ---------------------------------------------------------------------------
# Test 4: profile_workers=1 (sequential) still works correctly
# ---------------------------------------------------------------------------

class TestSequentialFallback:
    def test_sequential_path_profile_workers_1(self, mock_pg_conn):
        """profile_workers=1 uses sequential loop — same behaviour as before Wave 2."""
        profiles = [_make_profile("p1"), _make_profile("p2")]
        call_order: list[str] = []

        def fake_index_profile(
            pg_conn, *, profile_name, embedder, progress, max_workers, full_reindex=False,
            gc=False,
        ):
            call_order.append(profile_name)
            return _fake_counters(modules=2)

        mock_store = MagicMock()
        mock_store.list_profiles.return_value = profiles

        with (
            patch("src.indexer.pipeline.repo_store", return_value=mock_store),
            patch("src.indexer.pipeline.index_profile", side_effect=fake_index_profile),
        ):
            from src.indexer.pipeline import index_all
            result = index_all(mock_pg_conn, profile_workers=1)

        assert call_order == ["p1", "p2"], "Sequential: profiles must be processed in list order"
        assert result["profiles_ok"] == 2
        assert result["modules"] == 4


# ---------------------------------------------------------------------------
# Test 5 (INTEGRATION): Two profiles with different odoo_version — Neo4j data
# must be isolated; no cross-version field leakage even under parallel workers.
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> None:
    """Run a git command in the given repo, raising on failure."""
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


def _make_minimal_module_repo(
    root: Path,
    branch: str,
    module_name: str,
    model_name: str,
    field_names: list[str],
) -> Path:
    """Create a git repo at *root* containing one Odoo module.

    The module has a single model ``model_name`` with the supplied ``field_names``
    declared as ``fields.Char()``.  A real commit is created so that the
    incremental indexer can read HEAD and track head_sha.

    Model code is placed directly in the module root (not under models/)
    to avoid any subdirectory scanning issues.
    """
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init")
    _git(root, "checkout", "-b", branch)
    _git(root, "config", "user.email", "test@test.com")
    _git(root, "config", "user.name", "Test")

    module_dir = root / module_name
    module_dir.mkdir(parents=True, exist_ok=True)

    # __manifest__.py
    (module_dir / "__manifest__.py").write_text(
        f"{{'name': {module_name!r}, 'version': '1.0.0', "
        f"'depends': [], 'installable': True}}\n"
    )

    # <module_name>.py — one model with requested fields, directly in module root
    field_lines = "\n".join(f"    {fname} = fields.Char()" for fname in field_names)
    model_code = (
        "from odoo import models, fields\n\n"
        "class A(models.Model):\n"
        f"    _name = '{model_name}'\n"
        f"{field_lines}\n"
    )
    (module_dir / f"{module_name}.py").write_text(model_code)

    # __init__.py (module root)
    (module_dir / "__init__.py").write_text(f"from . import {module_name}\n")

    # Initial commit
    _git(root, "add", ".")
    _git(root, "commit", "-m", f"init {module_name}")

    return root


def test_two_profiles_neo4j_isolation_at_version_boundary(
    clean_pg,
    clean_neo4j,
    tmp_path,
):
    """Integration: composite key (name, odoo_version) isolates Neo4j data.

    Two profiles (99.0 and 98.0) are indexed in parallel via profile_workers=2.
    Each profile has a module named 'account' with version-specific fields.
    After indexing:
    - Module nodes for each version are distinct (no cross-version merge).
    - Fields from 99.0 do NOT appear under 98.0, and vice versa.
    - Shared field names declared in both versions appear in their own version
      only (no single merged node).

    This exercises the M6 thesis: parallel cross-profile indexing + composite
    key MERGE in Neo4j prevent cross-version data pollution.
    """
    # --- Bootstrap schema (clean_pg drops+recreates tables raw; we need them) ---
    run_migrations(clean_pg)

    # Pre-clean TEST_VERSION_ALT ("98.0") Neo4j nodes from any prior test run.
    # clean_neo4j fixture only wipes TEST_VERSION ("99.0"); we own "98.0" cleanup.
    driver = clean_neo4j  # neo4j.Driver
    with driver.session() as _s:
        _s.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n",
            v=TEST_VERSION_ALT,
        )

    # --- Build two minimal git repos, one per version ---
    repo99 = _make_minimal_module_repo(
        root=tmp_path / "repo99",
        branch=TEST_VERSION,
        module_name="account",
        model_name="account",
        field_names=["field_v99", "shared_field"],
    )
    repo98 = _make_minimal_module_repo(
        root=tmp_path / "repo98",
        branch=TEST_VERSION_ALT,
        module_name="account",
        model_name="account",
        field_names=["field_v98", "shared_field"],
    )

    # --- Register profiles + repos in Postgres ---
    pid99 = repo_store().add_profile("test_iso_99", TEST_VERSION)
    repo_store().add_repo(pid99, "file://local99", TEST_VERSION, str(repo99))

    pid98 = repo_store().add_profile("test_iso_98", TEST_VERSION_ALT)
    repo_store().add_repo(pid98, "file://local98", TEST_VERSION_ALT, str(repo98))

    # --- Run index_all with profile_workers=2 (parallel) ---
    # The parallel path spawns worker threads that call open_production_pg() to
    # obtain their own psycopg2 connection.  In tests PG_DSN is not set as an env
    # var — we patch open_production_pg to open a real connection to the test DB
    # using the same DSN the session-scoped pg_conn fixture uses.
    import psycopg2

    from tests.conftest import PG_TEST_DSN  # same DSN as the pg_conn fixture

    def _open_test_pg() -> psycopg2.extensions.connection:
        conn = psycopg2.connect(PG_TEST_DSN)
        conn.autocommit = True
        return conn

    from src.indexer.pipeline import index_all

    with patch("src.indexer.pipeline.open_production_pg", side_effect=_open_test_pg):
        result = index_all(clean_pg, profile_workers=2)

    # Both test profiles must succeed (migration 0004 seeds 5 root profiles with no repos,
    # which also succeed — so profiles_ok >= 2).
    assert result["profiles_ok"] >= 2, (
        f"Expected both profiles indexed OK; got profiles_failed={result['profiles_failed']}"
    )
    assert result["profiles_failed"] == [], (
        f"profiles_failed must be empty; got: {result['profiles_failed']}"
    )

    # --- Neo4j isolation assertions ---
    def _count(cypher: str, **params) -> int:
        with driver.session() as session:
            row = session.run(cypher, **params).single()
            return row[0] if row else 0

    # 1. Exactly one Module node per (name, odoo_version) — no cross-version merge
    count_module_99 = _count(
        "MATCH (m:Module {name: 'account', odoo_version: $v}) RETURN count(m)",
        v=TEST_VERSION,
    )
    assert count_module_99 == 1, (
        f"Expected 1 Module(account, {TEST_VERSION}), got {count_module_99}"
    )

    count_module_98 = _count(
        "MATCH (m:Module {name: 'account', odoo_version: $v}) RETURN count(m)",
        v=TEST_VERSION_ALT,
    )
    assert count_module_98 == 1, (
        f"Expected 1 Module(account, {TEST_VERSION_ALT}), got {count_module_98}"
    )

    # 2. field_v99 exists under 99.0 but NOT under 98.0
    count_v99_in_99 = _count(
        "MATCH (f:Field {name: 'field_v99', odoo_version: $v}) RETURN count(f)",
        v=TEST_VERSION,
    )
    assert count_v99_in_99 >= 1, (
        f"field_v99 must exist under {TEST_VERSION}; got {count_v99_in_99}"
    )

    count_v99_in_98 = _count(
        "MATCH (f:Field {name: 'field_v99', odoo_version: $v}) RETURN count(f)",
        v=TEST_VERSION_ALT,
    )
    assert count_v99_in_98 == 0, (
        f"field_v99 MUST NOT leak into {TEST_VERSION_ALT}; got {count_v99_in_98}"
    )

    # 3. field_v98 exists under 98.0 but NOT under 99.0
    count_v98_in_98 = _count(
        "MATCH (f:Field {name: 'field_v98', odoo_version: $v}) RETURN count(f)",
        v=TEST_VERSION_ALT,
    )
    assert count_v98_in_98 >= 1, (
        f"field_v98 must exist under {TEST_VERSION_ALT}; got {count_v98_in_98}"
    )

    count_v98_in_99 = _count(
        "MATCH (f:Field {name: 'field_v98', odoo_version: $v}) RETURN count(f)",
        v=TEST_VERSION,
    )
    assert count_v98_in_99 == 0, (
        f"field_v98 MUST NOT leak into {TEST_VERSION}; got {count_v98_in_99}"
    )

    # 4. shared_field exists in EACH version independently (not merged into one node)
    count_shared_in_98 = _count(
        "MATCH (f:Field {name: 'shared_field', odoo_version: $v}) RETURN count(f)",
        v=TEST_VERSION_ALT,
    )
    assert count_shared_in_98 >= 1, (
        f"shared_field must exist under {TEST_VERSION_ALT}; got {count_shared_in_98}"
    )

    count_shared_in_99 = _count(
        "MATCH (f:Field {name: 'shared_field', odoo_version: $v}) RETURN count(f)",
        v=TEST_VERSION,
    )
    assert count_shared_in_99 >= 1, (
        f"shared_field must exist under {TEST_VERSION}; got {count_shared_in_99}"
    )

    # Post-test teardown: wipe TEST_VERSION_ALT ("98.0") nodes created by this test.
    # clean_neo4j fixture handles TEST_VERSION ("99.0"); we own the "98.0" cleanup.
    with driver.session() as _s:
        _s.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n",
            v=TEST_VERSION_ALT,
        )
