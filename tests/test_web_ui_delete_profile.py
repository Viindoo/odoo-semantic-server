# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_web_ui_delete_profile.py
"""Integration tests for DELETE /api/repos/profiles/{id} (M8 W1 pure JSON API).

Tests cover:
- Happy path: profile + 2 repos -> PG cascaded, Neo4j modules gone, embeddings gone.
- Guard: indexer running for profile -> 409 JSON, profile NOT deleted.
- 404 JSON when profile_id not found.

ADR-0056 B10 rewrite (was: seeded Module nodes with only ``m.repo`` and patched
the removed ``repos._collect_module_names_for_repos`` / ``_delete_neo4j_for_repos``
/ ``_delete_embeddings_for_repos``). Ownership is now decided from the lifecycle
ledger and an unattributed node is a dependency stub that is never deleted, so
the tests seed what an index run leaves: modules indexed under the profile,
ledger rows committed by a scan, embeddings under the owning profile (the old
seed wrote them under an unrelated ``test_profile``). Full ledger rules:
``tests/test_web_ui_repo_removal_ledger.py``.
"""
import unittest.mock as mock

import httpx
import pytest

from src.db.migrate import _vector_extension_available, run_migrations
from src.web_ui.app import create_app
from tests import _ledger_seed as ls
from tests.conftest import TEST_VERSION

pytestmark = [pytest.mark.postgres, pytest.mark.neo4j]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
def writer(migrated_pg, clean_neo4j):
    w = ls.open_writer()
    yield w
    w.close()


def _indexed_repo(tmp_path, writer, profile_id: int, profile: str, basename: str,
                  module: str, head: str = "h1") -> int:
    repo_dir = ls.write_repo(tmp_path, profile, basename, modules=(module,))
    rid = ls.add_repo(profile_id, repo_dir)
    ls.index_graph(writer, repo_dir, profile=profile, repo_id=rid)
    ls.observe(rid, profile, (module,), head=head)
    return rid


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDeleteProfileHappyPath:
    @pytest.mark.asyncio
    async def test_delete_profile_removes_pg_rows(self, migrated_pg, clean_neo4j):
        """DELETE /api/repos/profiles/{id} -> profile + repos gone from PG, 200 ok JSON."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile(name="victim_99", odoo_version=TEST_VERSION)
        repo_store().add_repo(
            profile_id=pid,
            url="file://local/repo_a",
            branch=TEST_VERSION,
            local_path=f"/tmp/test_repo_a_{TEST_VERSION}",
        )
        repo_store().add_repo(
            profile_id=pid,
            url="file://local/repo_b",
            branch=TEST_VERSION,
            local_path=f"/tmp/test_repo_b_{TEST_VERSION}",
        )

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.delete(f"/api/repos/profiles/{pid}")

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True
        assert body.get("profile_name") == "victim_99"
        assert body.get("repo_count") == 2

        # Profile and repos must be gone
        remaining = repo_store().list_profiles()
        assert not any(p["id"] == pid for p in remaining)

        with migrated_pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM repos WHERE profile_id = %s", (pid,))
            assert cur.fetchone()[0] == 0

    @pytest.mark.asyncio
    async def test_delete_profile_cleans_neo4j(self, migrated_pg, clean_neo4j, writer,
                                               tmp_path):
        """DELETE -> Neo4j Module nodes of the profile's repos are removed,
        and the profile's embeddings for those modules are also cleaned up."""
        if not _vector_extension_available(migrated_pg):
            pytest.skip("pgvector extension not installed")
        pid = ls.add_profile("neo4j_victim_99")
        _indexed_repo(tmp_path, writer, pid, "neo4j_victim_99", "test_repo_a", "module_a")
        _indexed_repo(tmp_path, writer, pid, "neo4j_victim_99", "test_repo_b", "module_b")
        ls.seed_embedding("module_a", "neo4j_victim_99")
        ls.seed_embedding("module_b", "neo4j_victim_99")
        driver = clean_neo4j
        assert ls.module_node(driver, "module_a") and ls.module_node(driver, "module_b")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.delete(f"/api/repos/profiles/{pid}")
        assert resp.status_code == 200, resp.text

        assert ls.module_node(driver, "module_a") is None
        assert ls.module_node(driver, "module_b") is None
        assert ls.subtree(driver, "module_a") == ls.subtree(driver, "module_b") == {}
        assert ls.embedding_groups(migrated_pg) == {}

    @pytest.mark.asyncio
    async def test_delete_profile_response_contains_counts(self, migrated_pg, clean_neo4j,
                                                           writer, tmp_path):
        """Response body mentions profile name and what was actually deleted."""
        if not _vector_extension_available(migrated_pg):
            pytest.skip("pgvector extension not installed")
        pid = ls.add_profile("flash_test_99")
        _indexed_repo(tmp_path, writer, pid, "flash_test_99", "flash_repo_a", "flash_a")
        _indexed_repo(tmp_path, writer, pid, "flash_test_99", "flash_repo_b", "flash_b")
        ls.seed_embedding("flash_a", "flash_test_99")
        driver = clean_neo4j
        children = sum(ls.subtree(driver, "flash_a").values()) + sum(
            ls.subtree(driver, "flash_b").values())

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.delete(f"/api/repos/profiles/{pid}")

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True
        assert body.get("profile_name") == "flash_test_99"
        assert body.get("neo4j_modules") == 2
        assert body.get("neo4j_children") == children
        assert body.get("embeddings") == 1


class TestDeleteProfileGuard:
    @pytest.mark.asyncio
    async def test_blocks_when_indexer_running(self, migrated_pg, clean_neo4j):
        """Guard: indexer running for profile → 409 JSON, profile NOT deleted."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile(name="guarded_99", odoo_version=TEST_VERSION)

        app = create_app()
        with mock.patch(
            "src.indexer.pipeline.indexer_is_running",
            return_value=True,
        ):
            async with _async_client(app) as client:
                resp = await client.delete(f"/api/repos/profiles/{pid}")

        assert resp.status_code == 409
        body = resp.json()
        assert "error" in body
        assert "indexer" in body["error"].lower() or "running" in body["error"].lower()

        # Profile must still exist
        remaining = repo_store().list_profiles()
        assert any(p["id"] == pid for p in remaining)

    @pytest.mark.asyncio
    async def test_returns_404_for_missing_profile(self, migrated_pg, clean_neo4j):
        """DELETE with non-existent profile_id → 404 JSON."""
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.delete("/api/repos/profiles/999999")

        assert resp.status_code == 404
        body = resp.json()
        assert "error" in body
        assert "not found" in body["error"].lower()
