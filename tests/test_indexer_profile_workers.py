"""Cross-profile parallel indexing via --profile-workers / ThreadPoolExecutor (M6 W2-8)."""
import threading
from unittest.mock import MagicMock, patch

import pytest

pytestmark = [pytest.mark.postgres, pytest.mark.neo4j]

TEST_VERSION = "99.0"


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

        def fake_get_repos(pg_conn, profile_name):
            return repos_alpha if profile_name == "alpha" else repos_beta

        def fake_index_repo(
            repo, writer, pg_conn=None, embedder=None, progress=False, full_reindex=False,
        ):
            # repo id 1 → 3 modules, repo id 2 → 5 modules
            return _fake_counters(modules=3 if repo["id"] == 1 else 5)

        with (
            patch("src.indexer.pipeline.list_profiles", return_value=profiles),
            patch("src.indexer.pipeline.get_repos_for_profile", side_effect=fake_get_repos),
            patch("src.indexer.pipeline.update_repo_status"),
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

        with (
            patch("src.indexer.pipeline.list_profiles", return_value=profiles),
            patch("src.indexer.pipeline.get_repos_for_profile", return_value=repos),
            patch("src.indexer.pipeline.update_repo_status"),
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

        def fake_get_repos(pg_conn, profile_name):
            return repos_good if profile_name == "good" else repos_bad

        def fake_index_repo(
            repo, writer, pg_conn=None, embedder=None, progress=False, full_reindex=False,
        ):
            if repo["id"] == 2:
                raise RuntimeError("simulated failure for profile 'bad'")
            good_indexed.set()
            return _fake_counters(modules=4)

        with (
            patch("src.indexer.pipeline.list_profiles", return_value=profiles),
            patch("src.indexer.pipeline.get_repos_for_profile", side_effect=fake_get_repos),
            patch("src.indexer.pipeline.update_repo_status"),
            patch("src.indexer.pipeline.Neo4jWriter", return_value=mock_writer),
            patch("src.indexer.pipeline._neo4j_creds", return_value=("bolt://x", "u", "p")),
            patch("src.indexer.pipeline._index_repo", side_effect=fake_index_repo),
            patch("src.indexer.pipeline.open_production_pg", side_effect=_make_thread_pg),
        ):
            from src.indexer.pipeline import index_all
            with pytest.raises(RuntimeError, match="simulated failure for profile 'bad'"):
                index_all(mock_pg_conn, profile_workers=2)

        # 'good' profile should have been indexed
        assert good_indexed.is_set(), (
            "profile 'good' should complete even when profile 'bad' failed"
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
        ):
            call_order.append(profile_name)
            return _fake_counters(modules=2)

        with (
            patch("src.indexer.pipeline.list_profiles", return_value=profiles),
            patch("src.indexer.pipeline.index_profile", side_effect=fake_index_profile),
        ):
            from src.indexer.pipeline import index_all
            result = index_all(mock_pg_conn, profile_workers=1)

        assert call_order == ["p1", "p2"], "Sequential: profiles must be processed in list order"
        assert result["profiles_ok"] == 2
        assert result["modules"] == 4
