# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for /api/admin/patterns CRUD endpoints (WI-8).

10 test cases covering list, get, create, patch, soft-delete, sentinel bump,
language filter, 403 for non-admin, manual sentinel recompute, and pattern_id
regex validation.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
from __future__ import annotations

import json
import unittest.mock as mock

import httpx
import pytest

from src.db.migrate import run_migrations
from src.web_ui.app import create_app

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_pg(clean_pg):
    run_migrations(clean_pg)
    return clean_pg


def _client():
    """Factory: create a fresh AsyncClient per request block (httpx best practice)."""
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _seed_pattern(conn, *, pattern_id: str, language: str = "python") -> None:
    """Insert one minimal pattern row for testing."""
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO patterns
                 (pattern_id, intent_keywords, file_ref, snippet_text,
                  gotchas, odoo_version_min, language, core_symbol_names)
               VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s)
               ON CONFLICT (pattern_id) DO NOTHING""",
            (
                pattern_id,
                ["test", "example"],
                "addons/test/models/test.py:10",
                "# test snippet",
                json.dumps([{"text": "watch out for this"}, {"text": "also this"}]),
                "17.0",
                language,
                [],
            ),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Test 1: list_paginated
# ---------------------------------------------------------------------------


class TestListPaginated:
    @pytest.mark.asyncio
    async def test_list_paginated(self, migrated_pg):
        """GET /api/admin/patterns returns paginated list with total."""
        _seed_pattern(migrated_pg, pattern_id="test-list-a")
        _seed_pattern(migrated_pg, pattern_id="test-list-b")

        async with _client() as client:
            resp = await client.get(
                "/api/admin/patterns",
                params={"limit": 1, "offset": 0},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] >= 2
        assert body["limit"] == 1
        assert len(body["patterns"]) == 1

        # Second page uses a fresh client
        async with _client() as client:
            resp2 = await client.get(
                "/api/admin/patterns",
                params={"limit": 1, "offset": 1},
            )
        assert resp2.status_code == 200
        body2 = resp2.json()
        assert len(body2["patterns"]) == 1
        # Verify different pattern returned
        assert body2["patterns"][0]["pattern_id"] != body["patterns"][0]["pattern_id"]


# ---------------------------------------------------------------------------
# Test 2: get_single
# ---------------------------------------------------------------------------


class TestGetSingle:
    @pytest.mark.asyncio
    async def test_get_single(self, migrated_pg):
        """GET /api/admin/patterns/{id} returns the correct pattern."""
        _seed_pattern(migrated_pg, pattern_id="test-get-single")

        async with _client() as client:
            resp = await client.get("/api/admin/patterns/test-get-single")
        assert resp.status_code == 200
        body = resp.json()
        assert body["pattern_id"] == "test-get-single"
        assert body["language"] == "python"
        assert "snippet_text" in body

    @pytest.mark.asyncio
    async def test_get_single_not_found(self, migrated_pg):
        """GET /api/admin/patterns/{id} returns 404 for unknown pattern_id."""
        async with _client() as client:
            resp = await client.get("/api/admin/patterns/does-not-exist-xyz")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Test 3: create_unique_violation
# ---------------------------------------------------------------------------


class TestCreateUniqueViolation:
    @pytest.mark.asyncio
    async def test_create_unique_violation(self, migrated_pg):
        """POST with duplicate pattern_id returns 409 Conflict."""
        _seed_pattern(migrated_pg, pattern_id="test-dup-create")

        payload = {
            "pattern_id": "test-dup-create",
            "intent_keywords": ["test"],
            "file_ref": "addons/foo/bar.py:1",
            "snippet_text": "x = 1",
            "gotchas": [{"text": "gotcha"}],
            "odoo_version_min": "17.0",
            "language": "python",
            "reason": "test conflict check",
        }
        with mock.patch(
            "src.indexer.seed_patterns.recompute_sentinel_sha",
            return_value="a" * 64,
        ):
            async with _client() as client:
                resp = await client.post("/api/admin/patterns", json=payload)
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Test 4: create_bumps_sentinel
# ---------------------------------------------------------------------------


class TestCreateBumpsSentinel:
    @pytest.mark.asyncio
    async def test_create_bumps_sentinel(self, migrated_pg):
        """POST /api/admin/patterns returns sentinel_sha in response."""
        payload = {
            "pattern_id": "test-new-for-sentinel",
            "intent_keywords": ["sentinel", "test"],
            "file_ref": "addons/sale/models/order.py:42",
            "snippet_text": "# new pattern",
            "gotchas": [{"text": "important"}],
            "odoo_version_min": "17.0",
            "language": "python",
            "reason": "test sentinel bump",
        }
        fake_sha = "c" * 64

        with mock.patch(
            "src.indexer.seed_patterns.recompute_sentinel_sha",
            return_value=fake_sha,
        ) as mock_bump:
            async with _client() as client:
                resp = await client.post("/api/admin/patterns", json=payload)

        assert resp.status_code == 200
        body = resp.json()
        assert body["created"] is True
        assert body["pattern_id"] == "test-new-for-sentinel"
        assert "sentinel_sha" in body
        # Sentinel recompute was called
        mock_bump.assert_called_once()
        # Verify row exists in DB
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT pattern_id FROM patterns WHERE pattern_id = %s",
                ("test-new-for-sentinel",),
            )
            assert cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Test 5: patch_bumps_sentinel
# ---------------------------------------------------------------------------


class TestPatchBumpsSentinel:
    @pytest.mark.asyncio
    async def test_patch_bumps_sentinel(self, migrated_pg):
        """PATCH /api/admin/patterns/{id} updates row and bumps sentinel."""
        _seed_pattern(migrated_pg, pattern_id="test-patch-sentinel")

        patch_payload = {
            "snippet_text": "# patched snippet",
            "reason": "test patch bump",
        }
        fake_sha = "d" * 64

        with mock.patch(
            "src.indexer.seed_patterns.recompute_sentinel_sha",
            return_value=fake_sha,
        ) as mock_bump:
            async with _client() as client:
                resp = await client.patch(
                    "/api/admin/patterns/test-patch-sentinel",
                    json=patch_payload,
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body["updated"] is True
        assert "sentinel_sha" in body
        mock_bump.assert_called_once()

        # Verify DB update
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT snippet_text FROM patterns WHERE pattern_id = %s",
                ("test-patch-sentinel",),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "# patched snippet"


# ---------------------------------------------------------------------------
# Test 6: soft_delete_excludes_default
# ---------------------------------------------------------------------------


class TestSoftDeleteExcludesDefault:
    @pytest.mark.asyncio
    async def test_soft_delete_excludes_default(self, migrated_pg):
        """DELETE soft-deletes and excluded from default list, visible with include_deleted."""
        _seed_pattern(migrated_pg, pattern_id="test-soft-del")

        with mock.patch(
            "src.indexer.seed_patterns.recompute_sentinel_sha",
            return_value="e" * 64,
        ):
            async with _client() as client:
                del_resp = await client.delete("/api/admin/patterns/test-soft-del")

        assert del_resp.status_code == 200
        assert del_resp.json()["soft_deleted"] is True

        # Default list excludes soft-deleted
        async with _client() as client:
            list_resp = await client.get("/api/admin/patterns")
        ids_visible = [p["pattern_id"] for p in list_resp.json()["patterns"]]
        assert "test-soft-del" not in ids_visible

        # include_deleted=true shows it
        async with _client() as client:
            list_resp2 = await client.get(
                "/api/admin/patterns", params={"include_deleted": "true"}
            )
        ids_all = [p["pattern_id"] for p in list_resp2.json()["patterns"]]
        assert "test-soft-del" in ids_all


# ---------------------------------------------------------------------------
# Test 7: filter_by_language
# ---------------------------------------------------------------------------


class TestFilterByLanguage:
    @pytest.mark.asyncio
    async def test_filter_by_language(self, migrated_pg):
        """GET with language=xml returns only xml patterns."""
        _seed_pattern(migrated_pg, pattern_id="test-lang-py", language="python")
        _seed_pattern(migrated_pg, pattern_id="test-lang-xml", language="xml")

        async with _client() as client:
            resp = await client.get("/api/admin/patterns", params={"language": "xml"})
        assert resp.status_code == 200
        body = resp.json()
        returned_ids = [p["pattern_id"] for p in body["patterns"]]
        assert "test-lang-xml" in returned_ids
        assert "test-lang-py" not in returned_ids
        # All returned patterns have language=xml
        for p in body["patterns"]:
            assert p["language"] == "xml"


# ---------------------------------------------------------------------------
# Test 8: non_admin_403
# ---------------------------------------------------------------------------


class TestNonAdmin403:
    @pytest.mark.asyncio
    async def test_non_admin_403(self, migrated_pg):
        """Routes require admin — 401/403 when auth bypass is disabled."""
        import os

        # Remove auth bypass so middleware checks session properly
        old_val = os.environ.pop("WEBUI_AUTH_DISABLED", None)
        try:
            async with _client() as client:
                resp = await client.get("/api/admin/patterns")
            # Without a valid session, should get 401 or 403
            assert resp.status_code in (401, 403)
        finally:
            if old_val is not None:
                os.environ["WEBUI_AUTH_DISABLED"] = old_val


# ---------------------------------------------------------------------------
# Test 9: manual_sentinel_recompute
# ---------------------------------------------------------------------------


class TestManualSentinelRecompute:
    @pytest.mark.asyncio
    async def test_manual_sentinel_recompute(self, migrated_pg):
        """POST /api/admin/patterns/sentinel/recompute returns new sentinel_sha."""
        fake_sha = "f" * 64

        with mock.patch(
            "src.indexer.seed_patterns.recompute_sentinel_sha",
            return_value=fake_sha,
        ) as mock_bump:
            async with _client() as client:
                resp = await client.post("/api/admin/patterns/sentinel/recompute")

        assert resp.status_code == 200
        body = resp.json()
        assert body["manual_recompute"] is True
        assert body["sentinel_sha"] == fake_sha
        mock_bump.assert_called_once()


# ---------------------------------------------------------------------------
# Test 10: invalid_pattern_id_format_rejected
# ---------------------------------------------------------------------------


class TestInvalidPatternIdFormat:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_id",
        [
            "UPPERCASE",           # must be lowercase
            "-starts-with-dash",   # must start with [a-z0-9]
            "ab",                  # too short (min_length=3)
        ],
    )
    async def test_invalid_pattern_id_format_rejected(self, migrated_pg, bad_id):
        """POST with invalid pattern_id format returns 422 Unprocessable Entity."""
        payload = {
            "pattern_id": bad_id,
            "intent_keywords": ["test"],
            "file_ref": "addons/foo/bar.py:1",
            "snippet_text": "x = 1",
            "gotchas": [{"text": "gotcha"}],
            "odoo_version_min": "17.0",
            "language": "python",
            "reason": "test invalid id",
        }
        async with _client() as client:
            resp = await client.post("/api/admin/patterns", json=payload)
        assert resp.status_code == 422, (
            f"Expected 422 for pattern_id={bad_id!r}, got {resp.status_code}: "
            f"{resp.text[:200]}"
        )
