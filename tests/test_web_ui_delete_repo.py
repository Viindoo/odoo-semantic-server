# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_web_ui_delete_repo.py
"""Integration tests for DELETE /api/repos/repos/{id} (M8 W1 pure JSON API).

Tests cover:
- Happy path: 2 repos under same profile -> delete repo_A -> repo_A gone, repo_B intact.
- Cross-store: Neo4j Module nodes only repo_A ships are gone; repo_B's intact.
- pgvector embeddings of repo_A's profile for those modules gone; repo_B's intact.
- Multi-profile-same-version: deleting repo of profile_1 leaves profile_2 data intact.
- Guard: indexer running for profile -> 409 JSON, repo NOT deleted.
- 404 JSON when repo_id not found.

ADR-0056 B10 rewrite (was: seeded Module nodes with only ``m.repo`` and patched
``repos._delete_neo4j_for_repos`` / ``_delete_embeddings_for_repos``). Those
helpers are gone: the removal now decides ownership from the lifecycle ledger,
and a node with no profile and no repo id (what the old seeds were) is a
dependency stub that is never deleted. The tests keep their protection with
realistic state: modules indexed under the repo's profile, ledger rows committed
by a scan, embeddings written under the owning profile. The full ledger rules
live in ``tests/test_web_ui_repo_removal_ledger.py``.
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
    """A repo whose one module is indexed under *profile* and observed in the ledger."""
    repo_dir = ls.write_repo(tmp_path, profile, basename, modules=(module,))
    rid = ls.add_repo(profile_id, repo_dir)
    ls.index_graph(writer, repo_dir, profile=profile, repo_id=rid)
    ls.observe(rid, profile, (module,), head=head)
    return rid


def _module_exists(driver, name: str) -> bool:
    return ls.module_node(driver, name) is not None


def _require_vector(conn) -> None:
    if not _vector_extension_available(conn):
        pytest.skip("pgvector extension not installed")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM embeddings WHERE odoo_version = %s", (TEST_VERSION,))


# ---------------------------------------------------------------------------
# Tests: Happy Path
# ---------------------------------------------------------------------------

class TestDeleteRepoHappyPath:
    @pytest.mark.asyncio
    async def test_delete_repo_removes_pg_row_leaves_sibling(self, migrated_pg, clean_neo4j):
        """DELETE repo_A -> repo_A gone from PG, 200 ok JSON; sibling repo_B intact."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile(name="parity_test_99", odoo_version=TEST_VERSION)
        rid_a = repo_store().add_repo(
            profile_id=pid,
            url="file://local/repo_a", branch=TEST_VERSION,
            local_path=f"/tmp/repo_a_{TEST_VERSION}",
        )
        rid_b = repo_store().add_repo(
            profile_id=pid,
            url="file://local/repo_b", branch=TEST_VERSION,
            local_path=f"/tmp/repo_b_{TEST_VERSION}",
        )

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.delete(f"/api/repos/repos/{rid_a}")

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True

        repos = repo_store().get_repos_for_profile("parity_test_99")
        repo_ids = [r["id"] for r in repos]
        assert rid_a not in repo_ids
        assert rid_b in repo_ids

    @pytest.mark.asyncio
    async def test_delete_repo_cleans_neo4j_scoped(self, migrated_pg, clean_neo4j, writer,
                                                    tmp_path):
        """Delete repo_A -> the module only repo_A ships is gone with its children;
        repo_B's module intact."""
        pid = ls.add_profile("neo4j_scope_99")
        rid_a = _indexed_repo(tmp_path, writer, pid, "neo4j_scope_99", "neo4j_repo_a", "mod_a")
        _indexed_repo(tmp_path, writer, pid, "neo4j_scope_99", "neo4j_repo_b", "mod_b")
        driver = clean_neo4j
        assert _module_exists(driver, "mod_a") and _module_exists(driver, "mod_b")
        assert ls.subtree(driver, "mod_a"), "positive control: mod_a has children"

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.delete(f"/api/repos/repos/{rid_a}")
        assert resp.status_code == 200, resp.text

        assert not _module_exists(driver, "mod_a")
        assert ls.subtree(driver, "mod_a") == {}
        assert _module_exists(driver, "mod_b")
        assert ls.subtree(driver, "mod_b") != {}

    @pytest.mark.asyncio
    async def test_delete_repo_cleans_embeddings_scoped(self, migrated_pg, clean_neo4j, writer,
                                                         tmp_path):
        """Delete repo_A -> its profile's embeddings of its module gone; repo_B's intact."""
        _require_vector(migrated_pg)
        pid = ls.add_profile("emb_scope_99")
        rid_a = _indexed_repo(tmp_path, writer, pid, "emb_scope_99", "emb_repo_a", "emb_mod_a")
        _indexed_repo(tmp_path, writer, pid, "emb_scope_99", "emb_repo_b", "emb_mod_b")
        ls.seed_embedding("emb_mod_a", "emb_scope_99")
        ls.seed_embedding("emb_mod_b", "emb_scope_99")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.delete(f"/api/repos/repos/{rid_a}")
        assert resp.status_code == 200, resp.text

        assert ls.embedding_groups(migrated_pg) == {("emb_mod_b", TEST_VERSION, "emb_scope_99"): 1}

    @pytest.mark.asyncio
    async def test_delete_repo_response_contains_basename(self, migrated_pg, clean_neo4j, writer,
                                                           tmp_path):
        """Response body contains the basename and what was actually deleted."""
        _require_vector(migrated_pg)
        pid = ls.add_profile("flash_repo_99")
        rid = _indexed_repo(tmp_path, writer, pid, "flash_repo_99", "my_flash_repo_99",
                            "flash_mod")
        ls.seed_embedding("flash_mod", "flash_repo_99", entity="flash_mod.a")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.delete(f"/api/repos/repos/{rid}")

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True
        assert body.get("basename") == "my_flash_repo_99"
        assert body.get("neo4j_modules") == 1
        assert body.get("neo4j_children") >= 1
        assert body.get("embeddings") == 1


# ---------------------------------------------------------------------------
# Tests: Multi-profile same-version isolation
# ---------------------------------------------------------------------------

class TestDeleteRepoMultiProfileSameVersion:
    @pytest.mark.asyncio
    async def test_delete_repo_does_not_affect_other_profile_same_version(
        self, migrated_pg, clean_neo4j, writer, tmp_path,
    ):
        """Delete repo under profile_1 (v99.0) -> profile_2 (same v99.0) data intact."""
        from src.db.pg import repo_store

        pid1 = ls.add_profile("profile1_multitest_99")
        pid2 = ls.add_profile("profile2_multitest_99")
        rid1 = _indexed_repo(tmp_path, writer, pid1, "profile1_multitest_99", "repo_prof1",
                             "module_prof1")
        _indexed_repo(tmp_path, writer, pid2, "profile2_multitest_99", "repo_prof2",
                      "module_prof2")
        driver = clean_neo4j

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.delete(f"/api/repos/repos/{rid1}")
        assert resp.status_code == 200, resp.text

        # profile_1 repo gone from PG
        repos_p1 = repo_store().get_repos_for_profile("profile1_multitest_99")
        assert not any(r["id"] == rid1 for r in repos_p1)

        # profile_2 repo still present
        repos_p2 = repo_store().get_repos_for_profile("profile2_multitest_99")
        assert len(repos_p2) == 1

        # Neo4j: profile_1 module gone; profile_2 module intact
        assert not _module_exists(driver, "module_prof1")
        assert _module_exists(driver, "module_prof2")
        assert ls.attributed_profiles(driver, "module_prof2") == {("profile2_multitest_99",)}


# ---------------------------------------------------------------------------
# Tests: Guard
# ---------------------------------------------------------------------------

class TestDeleteRepoGuard:
    @pytest.mark.asyncio
    async def test_blocks_when_indexer_running(self, migrated_pg, clean_neo4j):
        """Guard: indexer running for profile → 409 JSON, repo NOT deleted."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile(name="guarded_repo_99", odoo_version=TEST_VERSION)
        rid = repo_store().add_repo(
            profile_id=pid,
            url="file://local/guarded_repo", branch=TEST_VERSION,
            local_path="/tmp/guarded_repo_99",
        )

        app = create_app()
        with mock.patch(
            "src.indexer.pipeline.indexer_is_running",
            return_value=True,
        ):
            async with _async_client(app) as client:
                resp = await client.delete(f"/api/repos/repos/{rid}")

        assert resp.status_code == 409
        body = resp.json()
        assert "error" in body
        assert "indexer" in body["error"].lower() or "running" in body["error"].lower()

        # Repo must still exist
        repos = repo_store().get_repos_for_profile("guarded_repo_99")
        assert any(r["id"] == rid for r in repos)

    @pytest.mark.asyncio
    async def test_returns_404_for_missing_repo(self, migrated_pg, clean_neo4j):
        """DELETE with non-existent repo_id → 404 JSON."""
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.delete("/api/repos/repos/999999")

        assert resp.status_code == 404
        body = resp.json()
        assert "error" in body
        assert "not found" in body["error"].lower()
