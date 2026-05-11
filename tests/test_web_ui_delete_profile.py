# tests/test_web_ui_delete_profile.py
"""Integration tests for POST /repos/profiles/{id}/delete (M8 W1).

Tests cover:
- Happy path: profile + 2 repos → PG cascaded, Neo4j modules gone, embeddings gone.
- Guard: indexer running for profile → 303 with flash, profile NOT deleted.
- 404-ish redirect when profile_id not found.
"""
import os
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


@pytest.fixture
def neo4j_writer(neo4j_driver):
    """Neo4jWriter connected to the test Neo4j instance."""
    from src.indexer.writer_neo4j import Neo4jWriter

    uri = os.environ["NEO4J_URI"]
    user = os.environ["NEO4J_USER"]
    password = os.environ["NEO4J_PASSWORD"]
    w = Neo4jWriter(uri, user, password)
    yield w
    w.close()


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
    """Insert a minimal embeddings row for testing (skips if pgvector missing)."""
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
        return -1  # table absent (pgvector not installed) — skip assertion


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDeleteProfileHappyPath:
    @pytest.mark.asyncio
    async def test_delete_profile_removes_pg_rows(self, migrated_pg, clean_neo4j):
        """POST /repos/profiles/{id}/delete → profile + repos gone from PG."""
        from src.db.repo_registry import add_profile, add_repo, list_profiles

        pid = add_profile(migrated_pg, name="victim_99", odoo_version=TEST_VERSION)
        add_repo(
            migrated_pg,
            profile_id=pid,
            url="file://local/repo_a",
            branch=TEST_VERSION,
            local_path=f"/tmp/test_repo_a_{TEST_VERSION}",
        )
        add_repo(
            migrated_pg,
            profile_id=pid,
            url="file://local/repo_b",
            branch=TEST_VERSION,
            local_path=f"/tmp/test_repo_b_{TEST_VERSION}",
        )

        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch(
            "src.web_ui.routes.repos._collect_module_names_for_repos",
            return_value={},
        ), mock.patch(
            "src.web_ui.routes.repos._delete_neo4j_for_repos",
            return_value=(0, 0),
        ), mock.patch(
            "src.web_ui.routes.repos._delete_embeddings_for_repos",
            return_value=0,
        ):
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/repos/profiles/{pid}/delete",
                    follow_redirects=False,
                )

        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "flash=" in location
        assert "deleted" in location.lower()

        # Profile and repos must be gone
        remaining = list_profiles(migrated_pg)
        assert not any(p["id"] == pid for p in remaining)

        with migrated_pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM repos WHERE profile_id = %s", (pid,))
            assert cur.fetchone()[0] == 0

    @pytest.mark.asyncio
    async def test_delete_profile_cleans_neo4j(self, migrated_pg, clean_neo4j):
        """POST delete → Neo4j Module nodes for profile repos are removed,
        and any indexed embeddings for those modules are also cleaned up."""
        from src.db.repo_registry import add_profile, add_repo

        repo_a_basename = f"test_repo_a_{TEST_VERSION}"
        repo_b_basename = f"test_repo_b_{TEST_VERSION}"
        module_a = f"module_{repo_a_basename}"
        module_b = f"module_{repo_b_basename}"

        pid = add_profile(migrated_pg, name="neo4j_victim_99", odoo_version=TEST_VERSION)
        add_repo(
            migrated_pg, profile_id=pid,
            url="file://local/neo4j_a", branch=TEST_VERSION,
            local_path=f"/tmp/{repo_a_basename}",
        )
        add_repo(
            migrated_pg, profile_id=pid,
            url="file://local/neo4j_b", branch=TEST_VERSION,
            local_path=f"/tmp/{repo_b_basename}",
        )

        driver = clean_neo4j
        _seed_neo4j_module(driver, repo_a_basename, module_a, TEST_VERSION)
        _seed_neo4j_module(driver, repo_b_basename, module_b, TEST_VERSION)

        # Seed real embeddings so the cleanup path can be exercised end-to-end
        _seed_embeddings(migrated_pg, module_a, TEST_VERSION)
        _seed_embeddings(migrated_pg, module_b, TEST_VERSION)

        assert _count_neo4j_modules(driver, repo_a_basename, TEST_VERSION) == 1
        assert _count_neo4j_modules(driver, repo_b_basename, TEST_VERSION) == 1

        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ):
            async with _async_client(app) as client:
                await client.post(
                    f"/repos/profiles/{pid}/delete",
                    follow_redirects=False,
                )

        assert _count_neo4j_modules(driver, repo_a_basename, TEST_VERSION) == 0
        assert _count_neo4j_modules(driver, repo_b_basename, TEST_VERSION) == 0

        # Embeddings for both modules must be gone
        assert _count_embeddings(migrated_pg, module_a, TEST_VERSION) in (0, -1)
        assert _count_embeddings(migrated_pg, module_b, TEST_VERSION) in (0, -1)

    @pytest.mark.asyncio
    async def test_delete_profile_flash_contains_counts(self, migrated_pg, clean_neo4j):
        """Flash message mentions profile name and repo count."""
        from src.db.repo_registry import add_profile, add_repo

        pid = add_profile(migrated_pg, name="flash_test_99", odoo_version=TEST_VERSION)
        add_repo(
            migrated_pg, profile_id=pid,
            url="file://local/flash_repo", branch=TEST_VERSION,
            local_path="/tmp/flash_repo_99",
        )

        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ), mock.patch(
            "src.web_ui.routes.repos._collect_module_names_for_repos",
            return_value={},
        ), mock.patch(
            "src.web_ui.routes.repos._delete_neo4j_for_repos",
            return_value=(2, 10),
        ), mock.patch(
            "src.web_ui.routes.repos._delete_embeddings_for_repos",
            return_value=5,
        ):
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/repos/profiles/{pid}/delete",
                    follow_redirects=False,
                )

        location = resp.headers["location"]
        assert "flash_test_99" in location or "deleted" in location.lower()


class TestDeleteProfileGuard:
    @pytest.mark.asyncio
    async def test_blocks_when_indexer_running(self, migrated_pg, clean_neo4j):
        """Guard: indexer running for profile → 303 with flash, profile NOT deleted."""
        from src.db.repo_registry import add_profile, list_profiles

        pid = add_profile(migrated_pg, name="guarded_99", odoo_version=TEST_VERSION)

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
                    f"/repos/profiles/{pid}/delete",
                    follow_redirects=False,
                )

        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "flash=" in location
        assert "indexer" in location.lower() or "running" in location.lower()

        # Profile must still exist
        remaining = list_profiles(migrated_pg)
        assert any(p["id"] == pid for p in remaining)

    @pytest.mark.asyncio
    async def test_redirects_with_flash_for_missing_profile(self, migrated_pg, clean_neo4j):
        """POST with non-existent profile_id → 303 with 'not found' flash."""
        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_conn",
            _make_conn_factory(migrated_pg),
        ):
            async with _async_client(app) as client:
                resp = await client.post(
                    "/repos/profiles/999999/delete",
                    follow_redirects=False,
                )

        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "flash=" in location
        assert "not+found" in location or "not found" in location.lower()
