# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_web_ui_core_symbol_counts.py
"""Tests for GET /api/repos/repos/{id}/core-symbol-counts (M10 WI-5).

The endpoint queries Neo4j for CoreSymbol counts grouped by version.
Tests cover:
- 404 for unknown repo_id
- Empty counts when Neo4j writer is unavailable (None)
- Counts returned correctly when Neo4j has CoreSymbol nodes
- 503 when Postgres is unreachable

Neo4j-dependent tests are marked neo4j and only run when Neo4j is available.
Mocked tests run with postgres only.
"""
import unittest.mock as mock

import httpx
import pytest

from src.db.migrate import run_migrations
from src.web_ui.app import create_app

pytestmark = pytest.mark.postgres


@pytest.fixture
def migrated_pg(clean_pg):
    run_migrations(clean_pg)
    return clean_pg


def _async_client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


class TestCoreSymbolCountsRoute:
    """GET /api/repos/repos/{id}/core-symbol-counts — unit tests (mocked Neo4j)."""

    @pytest.mark.asyncio
    async def test_unknown_repo_returns_404(self, migrated_pg):
        """Non-existent repo_id -> 404 with error key."""
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/api/repos/repos/999999/core-symbol-counts")
        assert resp.status_code == 404
        body = resp.json()
        assert "error" in body
        assert "not found" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_neo4j_unavailable_returns_empty_counts(self, migrated_pg):
        """When Neo4j writer is None (password missing), route returns empty dict."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile("core_counts_profile", "17.0", shared=True)
        rid = repo_store().add_repo(
            profile_id=pid,
            url="file://local",
            branch="17.0",
            local_path="/tmp/odoo_core_counts",
        )

        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_neo4j_writer", return_value=None
        ):
            async with _async_client(app) as client:
                resp = await client.get(f"/api/repos/repos/{rid}/core-symbol-counts")

        assert resp.status_code == 200
        body = resp.json()
        assert "counts" in body
        assert body["counts"] == {}

    @pytest.mark.asyncio
    async def test_neo4j_error_returns_empty_counts(self, migrated_pg):
        """When Neo4j session raises, route returns empty counts (not 500)."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile("core_counts_err_profile", "17.0", shared=True)
        rid = repo_store().add_repo(
            profile_id=pid,
            url="file://local",
            branch="17.0",
            local_path="/tmp/odoo_core_counts_err",
        )

        mock_writer = mock.MagicMock()
        mock_writer.driver.session.side_effect = RuntimeError("Neo4j down")

        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_neo4j_writer", return_value=mock_writer
        ):
            async with _async_client(app) as client:
                resp = await client.get(f"/api/repos/repos/{rid}/core-symbol-counts")

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("counts") == {}

    @pytest.mark.asyncio
    async def test_counts_returned_from_neo4j_query(self, migrated_pg):
        """Mocked Neo4j returning a count row -> route exposes it in counts dict."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile("core_counts_ok_profile", "17.0", shared=True)
        rid = repo_store().add_repo(
            profile_id=pid,
            url="file://local",
            branch="17.0",
            local_path="/tmp/odoo_core_counts_ok",
        )

        # Build a mock that mimics: session.run(...).  The route iterates result rows.
        mock_row = {"version": "17.0", "cnt": 1234}
        mock_result = mock.MagicMock()
        mock_result.__iter__ = mock.Mock(return_value=iter([mock_row]))

        mock_session = mock.MagicMock()
        mock_session.run.return_value = mock_result
        mock_session.__enter__ = mock.Mock(return_value=mock_session)
        mock_session.__exit__ = mock.Mock(return_value=False)

        mock_driver = mock.MagicMock()
        mock_driver.session.return_value = mock_session

        mock_writer = mock.MagicMock()
        mock_writer.driver = mock_driver

        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_neo4j_writer", return_value=mock_writer
        ):
            async with _async_client(app) as client:
                resp = await client.get(f"/api/repos/repos/{rid}/core-symbol-counts")

        assert resp.status_code == 200
        body = resp.json()
        assert "counts" in body
        assert body["counts"].get("17.0") == 1234

    @pytest.mark.asyncio
    async def test_zero_count_returned_when_no_core_symbols(self, migrated_pg):
        """When Neo4j returns count=0, route exposes it (admin can see not-indexed state)."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile("core_counts_zero_profile", "17.0", shared=True)
        rid = repo_store().add_repo(
            profile_id=pid,
            url="file://local",
            branch="17.0",
            local_path="/tmp/odoo_core_counts_zero",
        )

        mock_row = {"version": "17.0", "cnt": 0}
        mock_result = mock.MagicMock()
        mock_result.__iter__ = mock.Mock(return_value=iter([mock_row]))

        mock_session = mock.MagicMock()
        mock_session.run.return_value = mock_result
        mock_session.__enter__ = mock.Mock(return_value=mock_session)
        mock_session.__exit__ = mock.Mock(return_value=False)

        mock_driver = mock.MagicMock()
        mock_driver.session.return_value = mock_session

        mock_writer = mock.MagicMock()
        mock_writer.driver = mock_driver

        app = create_app()
        with mock.patch(
            "src.web_ui.routes.repos._get_neo4j_writer", return_value=mock_writer
        ):
            async with _async_client(app) as client:
                resp = await client.get(f"/api/repos/repos/{rid}/core-symbol-counts")

        assert resp.status_code == 200
        body = resp.json()
        assert body["counts"].get("17.0") == 0

    @pytest.mark.asyncio
    async def test_pg_unavailable_returns_503(self, migrated_pg):
        """When Postgres raises during repo lookup, route returns 503."""
        app = create_app()
        with mock.patch(
            "src.db.pg.repo_store",
            side_effect=RuntimeError("PG down"),
        ):
            async with _async_client(app) as client:
                resp = await client.get("/api/repos/repos/1/core-symbol-counts")

        assert resp.status_code == 503
        body = resp.json()
        assert "error" in body
