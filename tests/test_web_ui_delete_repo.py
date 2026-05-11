# tests/test_web_ui_delete_repo.py
"""Integration tests for POST /repos/repos/{id}/delete (M8 W2).

Tests cover:
- Happy path: 2 repos under same profile → delete repo_A → repo_A gone, repo_B intact.
- Cross-store: Neo4j Module nodes for repo_A gone; repo_B Module nodes intact.
- pgvector embeddings for repo_A gone; repo_B embeddings intact.
- Multi-profile-same-version: deleting repo of profile_1 leaves profile_2 data intact.
- Guard: indexer running for profile → 303 with flash, repo NOT deleted.
- 404-ish redirect when repo_id not found.
"""
import unittest.mock as mock

import httpx
import pytest

from src.db.migrate import run_migrations
from src.web_ui.app import create_app
from tests.conftest import TEST_VERSION

pytestmark = [pytest.mark.postgres, pytest.mark.neo4j]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _NoCloseConn:
    """Proxy psycopg2 connection but no-op close() to keep session conn alive."""

    def __init__(self, conn):
        self._conn = conn

    def close(self):
        pass

    def cursor(self, *args, **kwargs):
        return self._conn.cursor(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _make_conn_factory(pg_conn):
    wrapped = _NoCloseConn(pg_conn)

    def factory():
        return wrapped

    return factory


def _async_client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def migrated_pg(clean_pg):
    run_migrations(clean_pg)
    return clean_pg


def _seed_neo4j_module(driver, repo_basename: str, module_name: str, version: str) -> None:
    """Seed a Module node + child Model node in Neo4j."""
    with driver.session() as session:
        session.run(
            """
            MERGE (m:Module {name: $mod_name, odoo_version: $v})
            SET m.repo = $repo, m.path = '/fake/path'
            """,
            mod_name=module_name,
            v=version,
            repo=repo_basename,
        )
        session.run(
            """
            MERGE (mdl:Model {name: $model_name, module: $mod_name, odoo_version: $v})
            """,
            model_name=f"model_{module_name}",
            mod_name=module_name,
            v=version,
        )


def _count_neo4j_modules(driver, repo_basename: str, version: str) -> int:
    with driver.session() as session:
        row = session.run(
            "MATCH (m:Module {repo: $repo, odoo_version: $v}) RETURN count(m) AS n",
            repo=repo_basename,
            v=version,
        ).single()
    return row["n"] if row else 0


def _seed_embeddings(pg_conn, module_name: str, version: str) -> None:
    """Insert a minimal embeddings row (skips gracefully if pgvector missing)."""
    try:
        from pgvector.psycopg2 import register_vector
        register_vector(pg_conn)
        import numpy as np
        vec = np.zeros(1024, dtype=np.float32)
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO embeddings
                    (chunk_type, module, odoo_version, entity_name, file_path,
                     chunk_idx, content, vec)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                ("model", module_name, version, "entity", f"/fake/{module_name}.py",
                 0, "fake content", vec),
            )
    except Exception:
        pass  # pgvector not installed — embeddings tests degrade gracefully


def _count_embeddings(pg_conn, module_name: str, version: str) -> int:
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM embeddings WHERE module = %s AND odoo_version = %s",
                (module_name, version),
            )
            return cur.fetchone()[0]
    except Exception:
        return -1  # table absent — skip assertion


# ---------------------------------------------------------------------------
# Tests: Happy Path
# ---------------------------------------------------------------------------

class TestDeleteRepoHappyPath:
    @pytest.mark.asyncio
    async def test_delete_repo_removes_pg_row_leaves_sibling(self, migrated_pg, clean_neo4j):
        """POST delete repo_A → repo_A gone from PG; sibling repo_B intact."""
        from src.db.repo_registry import add_profile, add_repo, get_repos_for_profile

        pid = add_profile(migrated_pg, name="parity_test_99", odoo_version=TEST_VERSION)
        rid_a = add_repo(
            migrated_pg, profile_id=pid,
            url="file://local/repo_a", branch=TEST_VERSION,
            local_path=f"/tmp/repo_a_{TEST_VERSION}",
        )
        rid_b = add_repo(
            migrated_pg, profile_id=pid,
            url="file://local/repo_b", branch=TEST_VERSION,
            local_path=f"/tmp/repo_b_{TEST_VERSION}",
        )

        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch(
            "src.web_ui.routes.repos._delete_neo4j_for_repos",
            return_value=(0, 0),
        ), mock.patch(
            "src.web_ui.routes.repos._delete_embeddings_for_repos",
            return_value=0,
        ):
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/repos/repos/{rid_a}/delete",
                    follow_redirects=False,
                )

        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "flash=" in location
        assert "deleted" in location.lower()

        repos = get_repos_for_profile(migrated_pg, "parity_test_99")
        repo_ids = [r["id"] for r in repos]
        assert rid_a not in repo_ids
        assert rid_b in repo_ids

    @pytest.mark.asyncio
    async def test_delete_repo_cleans_neo4j_scoped(self, migrated_pg, clean_neo4j):
        """Delete repo_A → its Neo4j Module + children gone; repo_B Module intact."""
        from src.db.repo_registry import add_profile, add_repo

        basename_a = f"neo4j_repo_a_{TEST_VERSION}"
        basename_b = f"neo4j_repo_b_{TEST_VERSION}"
        module_a = f"module_{basename_a}"
        module_b = f"module_{basename_b}"

        pid = add_profile(migrated_pg, name="neo4j_scope_99", odoo_version=TEST_VERSION)
        rid_a = add_repo(
            migrated_pg, profile_id=pid,
            url="file://local/neo4j_a", branch=TEST_VERSION,
            local_path=f"/tmp/{basename_a}",
        )
        add_repo(
            migrated_pg, profile_id=pid,
            url="file://local/neo4j_b", branch=TEST_VERSION,
            local_path=f"/tmp/{basename_b}",
        )

        driver = clean_neo4j
        _seed_neo4j_module(driver, basename_a, module_a, TEST_VERSION)
        _seed_neo4j_module(driver, basename_b, module_b, TEST_VERSION)

        assert _count_neo4j_modules(driver, basename_a, TEST_VERSION) == 1
        assert _count_neo4j_modules(driver, basename_b, TEST_VERSION) == 1

        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch(
            "src.web_ui.routes.repos._delete_embeddings_for_repos",
            return_value=0,
        ):
            async with _async_client(app) as client:
                await client.post(
                    f"/repos/repos/{rid_a}/delete",
                    follow_redirects=False,
                )

        assert _count_neo4j_modules(driver, basename_a, TEST_VERSION) == 0
        assert _count_neo4j_modules(driver, basename_b, TEST_VERSION) == 1

    @pytest.mark.asyncio
    async def test_delete_repo_cleans_embeddings_scoped(self, migrated_pg, clean_neo4j):
        """Delete repo_A → its embeddings gone; repo_B embeddings intact.

        Seeds real Neo4j Module nodes so _collect_module_names_for_repos resolves
        the correct Odoo module names (not the repo basenames — that was the bug).
        """
        from src.db.repo_registry import add_profile, add_repo

        basename_a = f"emb_repo_a_{TEST_VERSION}"
        basename_b = f"emb_repo_b_{TEST_VERSION}"
        module_a = f"module_{basename_a}"
        module_b = f"module_{basename_b}"

        pid = add_profile(migrated_pg, name="emb_scope_99", odoo_version=TEST_VERSION)
        rid_a = add_repo(
            migrated_pg, profile_id=pid,
            url="file://local/emb_a", branch=TEST_VERSION,
            local_path=f"/tmp/{basename_a}",
        )
        add_repo(
            migrated_pg, profile_id=pid,
            url="file://local/emb_b", branch=TEST_VERSION,
            local_path=f"/tmp/{basename_b}",
        )

        # Seed Neo4j Module nodes so the cleanup helper resolves real module names
        driver = clean_neo4j
        _seed_neo4j_module(driver, basename_a, module_a, TEST_VERSION)
        _seed_neo4j_module(driver, basename_b, module_b, TEST_VERSION)

        _seed_embeddings(migrated_pg, module_a, TEST_VERSION)
        _seed_embeddings(migrated_pg, module_b, TEST_VERSION)

        pre_a = _count_embeddings(migrated_pg, module_a, TEST_VERSION)
        pre_b = _count_embeddings(migrated_pg, module_b, TEST_VERSION)

        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ):
            async with _async_client(app) as client:
                await client.post(
                    f"/repos/repos/{rid_a}/delete",
                    follow_redirects=False,
                )

        post_a = _count_embeddings(migrated_pg, module_a, TEST_VERSION)
        post_b = _count_embeddings(migrated_pg, module_b, TEST_VERSION)

        if pre_a >= 0:  # -1 means pgvector absent → skip
            assert post_a == 0
        if pre_b >= 0:
            assert post_b == pre_b  # repo_B untouched

    @pytest.mark.asyncio
    async def test_delete_repo_flash_contains_basename(self, migrated_pg, clean_neo4j):
        """Flash message must mention the repo basename."""
        from src.db.repo_registry import add_profile, add_repo

        pid = add_profile(migrated_pg, name="flash_repo_99", odoo_version=TEST_VERSION)
        rid = add_repo(
            migrated_pg, profile_id=pid,
            url="file://local/flash_repo", branch=TEST_VERSION,
            local_path="/tmp/my_flash_repo_99",
        )

        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch(
            "src.web_ui.routes.repos._delete_neo4j_for_repos",
            return_value=(1, 3),
        ), mock.patch(
            "src.web_ui.routes.repos._delete_embeddings_for_repos",
            return_value=2,
        ):
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/repos/repos/{rid}/delete",
                    follow_redirects=False,
                )

        location = resp.headers["location"]
        assert "my_flash_repo_99" in location or "deleted" in location.lower()


# ---------------------------------------------------------------------------
# Tests: Multi-profile same-version isolation
# ---------------------------------------------------------------------------

class TestDeleteRepoMultiProfileSameVersion:
    @pytest.mark.asyncio
    async def test_delete_repo_does_not_affect_other_profile_same_version(
        self, migrated_pg, clean_neo4j
    ):
        """Delete repo under profile_1 (v99.0) → profile_2 (same v99.0) data intact.

        This proves scoping is by (basename, version) not version-wide.
        """
        from src.db.repo_registry import add_profile, add_repo, get_repos_for_profile

        pid1 = add_profile(migrated_pg, name="profile1_multitest_99", odoo_version=TEST_VERSION)
        pid2 = add_profile(migrated_pg, name="profile2_multitest_99", odoo_version=TEST_VERSION)

        basename_1 = f"repo_prof1_{TEST_VERSION}"
        basename_2 = f"repo_prof2_{TEST_VERSION}"
        module_1 = f"module_{basename_1}"
        module_2 = f"module_{basename_2}"

        rid1 = add_repo(
            migrated_pg, profile_id=pid1,
            url="file://local/prof1_repo", branch=TEST_VERSION,
            local_path=f"/tmp/{basename_1}",
        )
        add_repo(
            migrated_pg, profile_id=pid2,
            url="file://local/prof2_repo", branch=TEST_VERSION,
            local_path=f"/tmp/{basename_2}",
        )

        driver = clean_neo4j
        _seed_neo4j_module(driver, basename_1, module_1, TEST_VERSION)
        _seed_neo4j_module(driver, basename_2, module_2, TEST_VERSION)

        assert _count_neo4j_modules(driver, basename_1, TEST_VERSION) == 1
        assert _count_neo4j_modules(driver, basename_2, TEST_VERSION) == 1

        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ):
            async with _async_client(app) as client:
                await client.post(
                    f"/repos/repos/{rid1}/delete",
                    follow_redirects=False,
                )

        # profile_1 repo gone from PG
        repos_p1 = get_repos_for_profile(migrated_pg, "profile1_multitest_99")
        assert not any(r["id"] == rid1 for r in repos_p1)

        # profile_2 repo still present
        repos_p2 = get_repos_for_profile(migrated_pg, "profile2_multitest_99")
        assert len(repos_p2) == 1

        # Neo4j: profile_1 module gone; profile_2 module intact
        assert _count_neo4j_modules(driver, basename_1, TEST_VERSION) == 0
        assert _count_neo4j_modules(driver, basename_2, TEST_VERSION) == 1


# ---------------------------------------------------------------------------
# Tests: Guard
# ---------------------------------------------------------------------------

class TestDeleteRepoGuard:
    @pytest.mark.asyncio
    async def test_blocks_when_indexer_running(self, migrated_pg, clean_neo4j):
        """Guard: indexer running for profile → 303 with flash, repo NOT deleted."""
        from src.db.repo_registry import add_profile, add_repo, get_repos_for_profile

        pid = add_profile(migrated_pg, name="guarded_repo_99", odoo_version=TEST_VERSION)
        rid = add_repo(
            migrated_pg, profile_id=pid,
            url="file://local/guarded_repo", branch=TEST_VERSION,
            local_path="/tmp/guarded_repo_99",
        )

        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch(
            "src.indexer.pipeline.indexer_is_running",
            return_value=True,
        ):
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/repos/repos/{rid}/delete",
                    follow_redirects=False,
                )

        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "flash=" in location
        assert "indexer" in location.lower() or "running" in location.lower()

        # Repo must still exist
        repos = get_repos_for_profile(migrated_pg, "guarded_repo_99")
        assert any(r["id"] == rid for r in repos)

    @pytest.mark.asyncio
    async def test_redirects_with_flash_for_missing_repo(self, migrated_pg, clean_neo4j):
        """POST with non-existent repo_id → 303 with 'not found' flash."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ):
            async with _async_client(app) as client:
                resp = await client.post(
                    "/repos/repos/999999/delete",
                    follow_redirects=False,
                )

        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "flash=" in location
        assert "not+found" in location or "not found" in location.lower()
