# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_indexer_parallel.py
"""Tests for ThreadPoolExecutor parallel repo scanning in index_profile (M6 P3).

These tests exercise the orchestration logic of index_profile() with max_workers>1.
All Neo4j/PostgreSQL I/O is mocked — no live services needed.
"""
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

pytestmark = [pytest.mark.postgres, pytest.mark.neo4j]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_repo(repo_id: int, local_path: str = "/fake/repo") -> dict:
    return {
        "id": repo_id,
        "local_path": local_path,
        "odoo_version": "99.0",
        "url": "file://local",
        "branch": "99.0",
    }


def _fake_counters(n: int = 1) -> dict:
    return {
        "modules": n,
        "views": n,
        "qweb": 0,
        "embeddings": 0,
        "js_patches": 0,
        "owl_comps": 0,
    }


# ---------------------------------------------------------------------------
# Fixtures: mock all external I/O
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_pg_conn():
    """A mock psycopg2 connection (the one passed to index_profile)."""
    conn = MagicMock()
    # pg_try_advisory_lock → acquired=True, then advisory_unlock
    lock_cur = MagicMock()
    lock_cur.__enter__ = lambda s: s
    lock_cur.__exit__ = MagicMock(return_value=False)
    lock_cur.fetchone.return_value = (True,)  # lock acquired
    conn.cursor.return_value = lock_cur
    return conn


@pytest.fixture
def mock_writer():
    """A mock Neo4jWriter."""
    w = MagicMock()
    w.setup_indexes.return_value = None
    w.close.return_value = None
    return w


# ---------------------------------------------------------------------------
# Test 1: max_workers=1 keeps sequential ordering
# ---------------------------------------------------------------------------

class TestMaxWorkers1Sequential:
    def test_max_workers_1_keeps_sequential(self, mock_pg_conn, mock_writer):
        """With max_workers=1, repos are indexed in the order returned by get_repos_for_profile."""
        repos = [_make_repo(1), _make_repo(2)]
        call_order: list[int] = []

        def fake_index_repo(repo, writer, pg_conn=None, embedder=None, gc=False,
                            progress=False, full_reindex=False, ancestor_profiles=None):
            call_order.append(repo["id"])
            return _fake_counters()

        mock_store = MagicMock()
        mock_store.get_repos_for_profile.return_value = repos
        mock_store.get_ancestor_profile_names.return_value = ["test"]

        with (
            patch("src.indexer.pipeline.repo_store", return_value=mock_store),
            patch("src.indexer.pipeline.Neo4jWriter", return_value=mock_writer),
            patch("src.indexer.pipeline._neo4j_creds", return_value=("bolt://x", "u", "p")),
            patch("src.indexer.pipeline._index_repo", side_effect=fake_index_repo),
        ):
            from src.indexer.pipeline import index_profile
            result = index_profile(mock_pg_conn, profile_name="test", max_workers=1)

        assert call_order == [1, 2], "Sequential: repos must be processed in list order"
        assert result["modules"] == 2


# ---------------------------------------------------------------------------
# Test 2: max_workers=2 allows concurrent execution
# ---------------------------------------------------------------------------

class TestMaxWorkers2Concurrent:
    def test_max_workers_2_runs_concurrently(self, mock_pg_conn, mock_writer):
        """With max_workers=2, two repos can execute _index_repo at the same time."""
        repos = [_make_repo(1), _make_repo(2)]

        inside_event = threading.Event()   # set when first thread enters _index_repo
        concurrency_detected = threading.Event()  # set when BOTH are inside simultaneously

        def fake_index_repo(repo, writer, pg_conn=None, embedder=None, gc=False,
                            progress=False, full_reindex=False, ancestor_profiles=None):
            inside_event.set()  # signal that we're inside
            # Give the other thread time to also enter
            concurrency_detected.wait(timeout=2.0)
            if inside_event.is_set() and not concurrency_detected.is_set():
                # We're the second thread to arrive — both are inside now
                concurrency_detected.set()
            # Brief sleep so both threads overlap
            time.sleep(0.02)
            return _fake_counters()

        # Revised logic: detect overlap via a counter
        active_count = 0
        lock = threading.Lock()
        overlap_detected = threading.Event()

        def fake_index_repo_v2(repo, writer, pg_conn=None, embedder=None, gc=False,
                               progress=False, full_reindex=False, ancestor_profiles=None):
            nonlocal active_count
            with lock:
                active_count += 1
                if active_count >= 2:
                    overlap_detected.set()
            time.sleep(0.05)  # stay inside long enough for overlap
            with lock:
                active_count -= 1
            return _fake_counters()

        def fake_open_pg():
            m = MagicMock()
            m.cursor.return_value.__enter__ = lambda s: s
            m.cursor.return_value.__exit__ = MagicMock(return_value=False)
            return m

        mock_store = MagicMock()
        mock_store.get_repos_for_profile.return_value = repos
        mock_store.get_ancestor_profile_names.return_value = ["test"]

        with (
            patch("src.indexer.pipeline.repo_store", return_value=mock_store),
            patch("src.indexer.pipeline.Neo4jWriter", return_value=mock_writer),
            patch("src.indexer.pipeline._neo4j_creds", return_value=("bolt://x", "u", "p")),
            patch("src.indexer.pipeline._index_repo", side_effect=fake_index_repo_v2),
            patch("src.indexer.pipeline.open_production_pg", side_effect=fake_open_pg),
        ):
            from src.indexer.pipeline import index_profile
            result = index_profile(mock_pg_conn, profile_name="test", max_workers=2)

        assert overlap_detected.is_set(), (
            "Expected concurrent execution: both _index_repo calls should overlap in time"
        )
        assert result["modules"] == 2


# ---------------------------------------------------------------------------
# Test 3: degenerate case — 1 repo, max_workers=2 still completes
# ---------------------------------------------------------------------------

class TestMaxWorkers2OneRepo:
    def test_max_workers_2_with_one_repo_works(self, mock_pg_conn, mock_writer):
        """Degenerate: single repo with max_workers=2 completes successfully."""
        repos = [_make_repo(42)]

        def fake_open_pg():
            m = MagicMock()
            m.cursor.return_value.__enter__ = lambda s: s
            m.cursor.return_value.__exit__ = MagicMock(return_value=False)
            return m

        mock_store = MagicMock()
        mock_store.get_repos_for_profile.return_value = repos

        with (
            patch("src.indexer.pipeline.repo_store", return_value=mock_store),
            patch("src.indexer.pipeline.Neo4jWriter", return_value=mock_writer),
            patch("src.indexer.pipeline._neo4j_creds", return_value=("bolt://x", "u", "p")),
            patch("src.indexer.pipeline._index_repo", return_value=_fake_counters(5)),
            patch("src.indexer.pipeline.open_production_pg", side_effect=fake_open_pg),
        ):
            from src.indexer.pipeline import index_profile
            result = index_profile(mock_pg_conn, profile_name="test", max_workers=2)

        assert result["modules"] == 5
        mock_store.update_repo_status.assert_called_once_with(42, "indexed")


# ---------------------------------------------------------------------------
# Test 4: failure in one repo doesn't block others; outer call raises
# ---------------------------------------------------------------------------

class TestMaxWorkers2PartialFailure:
    def test_failure_in_one_repo_doesnt_block_others(self, mock_pg_conn, mock_writer):
        """With max_workers=2: repo[0] fails → repo[1] still indexed; outer raises."""
        repo0 = _make_repo(10)
        repo1 = _make_repo(11)
        repos = [repo0, repo1]

        update_calls: list[tuple] = []

        def fake_index_repo(repo, writer, pg_conn=None, embedder=None, gc=False,
                            progress=False, full_reindex=False, ancestor_profiles=None):
            if repo["id"] == 10:
                raise RuntimeError("simulated failure on repo 10")
            return _fake_counters(3)

        def fake_update_repo_status(repo_id, status, error_msg=None):
            update_calls.append((repo_id, status))

        def fake_open_pg():
            m = MagicMock()
            m.cursor.return_value.__enter__ = lambda s: s
            m.cursor.return_value.__exit__ = MagicMock(return_value=False)
            return m

        mock_store = MagicMock()
        mock_store.get_repos_for_profile.return_value = repos
        mock_store.get_ancestor_profile_names.return_value = ["test"]
        mock_store.update_repo_status.side_effect = fake_update_repo_status

        with (
            patch("src.indexer.pipeline.repo_store", return_value=mock_store),
            patch("src.indexer.pipeline.Neo4jWriter", return_value=mock_writer),
            patch("src.indexer.pipeline._neo4j_creds", return_value=("bolt://x", "u", "p")),
            patch("src.indexer.pipeline._index_repo", side_effect=fake_index_repo),
            patch("src.indexer.pipeline.open_production_pg", side_effect=fake_open_pg),
        ):
            from src.indexer.pipeline import index_profile
            with pytest.raises(RuntimeError, match="1 repo\\(s\\) failed"):
                index_profile(mock_pg_conn, profile_name="test", max_workers=2)

        # repo[1] should have been indexed despite repo[0] failing
        statuses = dict(update_calls)
        assert statuses.get(11) == "indexed", (
            "repo id=11 should be marked 'indexed' even when repo id=10 failed"
        )
        assert statuses.get(10) == "error", (
            "repo id=10 should be marked 'error'"
        )
