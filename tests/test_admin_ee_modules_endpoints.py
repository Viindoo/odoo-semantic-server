# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_admin_ee_modules_endpoints.py
"""Integration tests for /api/admin/ee-modules/* CRUD endpoints (WI-7, ADR-0042).

Covers:
1. test_list_active_modules_count_16          GET / returns 16 active entries.
2. test_get_single_by_id                      GET /{id} returns correct row.
3. test_create_new_module_unique_violation    POST duplicate name -> 409.
4. test_create_new_module_success             POST new name -> 200 + id field.
5. test_patch_module_invalidates_cache        PATCH -> invalidate_ee_modules_cache called.
6. test_soft_delete_excludes_from_list_default DELETE -> excluded from GET / by default.
7. test_soft_delete_included_when_query_param_true GET /?include_deprecated=true shows it.
8. test_non_admin_403                         require_admin dependency blocks non-admin.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
WEBUI_AUTH_DISABLED is managed by conftest autouse fixture; tests run with the
bypass active (not in real_auth_flow_files), so require_admin returns user_id=1.

Fixture strategy: module-scoped `migrated_pg` runs migrations once per module to
avoid the per-test yoyo re-migration issue (yoyo internal tables dropped by
`clean_pg` between tests; `run_migrations` would fail on second call with
UndefinedTable: _yoyo_migration).  Tests that insert data use unique names and
clean up after themselves.  pg_conn has autocommit=True (conftest) so no explicit
commit() calls are needed.
"""
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.data.ee_modules import invalidate_ee_modules_cache
from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_ee_cache():
    """Ensure in-process EE modules cache is clean before and after each test."""
    invalidate_ee_modules_cache()
    yield
    invalidate_ee_modules_cache()


@pytest.fixture(scope="module")
def migrated_pg(pg_conn):
    """Run migrations ONCE per module on the session-scoped DB connection.

    Module scope avoids repeated run_migrations calls that fail after clean_pg
    drops yoyo internal tables between tests (UndefinedTable: _yoyo_migration).
    Each test that mutates data is responsible for cleaning up its own rows.
    pg_conn has autocommit=True so no explicit commit() is needed.
    """
    run_migrations(pg_conn)
    yield pg_conn


def _async_client(app):
    """Return an AsyncClient backed by the ASGI app (loopback-safe default)."""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _seed_ee_module(
    pg_conn,
    *,
    name: str,
    vt_equivalent: str | None = None,
    deprecated: bool = False,
) -> int:
    """Insert an ee_modules row and return its id.  pg_conn is autocommit=True."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ee_modules (name, vt_equivalent, deprecated) "
            "VALUES (%s, %s, %s) RETURNING id",
            (name, vt_equivalent, deprecated),
        )
        return cur.fetchone()[0]


def _delete_ee_module_by_name(pg_conn, name: str) -> None:
    """Remove an ee_modules row by name (cleanup helper).  Autocommit=True."""
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM ee_modules WHERE name = %s", (name,))


# ---------------------------------------------------------------------------
# 1. List active modules - count 16
# ---------------------------------------------------------------------------


class TestListActiveModules:
    @pytest.mark.asyncio
    async def test_list_active_modules_matches_canonical_set(self, migrated_pg):
        """GET /api/admin/ee-modules returns the canonical EE-module set.

        The m13_011 seed and the static fallback share one SSOT
        (_FALLBACK_EE_MODULES). Assert the endpoint returns exactly that set of
        module names — not a hardcoded count. This catches a seed that has the
        right cardinality but the wrong modules (dropped/renamed/duplicated
        row), which a `len == 16` mirror of the source constant cannot.
        """
        from src.data.ee_modules import _FALLBACK_EE_MODULES
        from src.web_ui.app import create_app

        expected_names = {m["name"] for m in _FALLBACK_EE_MODULES}

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/api/admin/ee-modules")

        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        returned_names = {row["name"] for row in data}
        assert returned_names == expected_names, (
            f"active EE-module set drifted from SSOT: "
            f"missing={expected_names - returned_names}, "
            f"unexpected={returned_names - expected_names}"
        )


# ---------------------------------------------------------------------------
# 2. Get single by id
# ---------------------------------------------------------------------------


class TestGetSingleById:
    @pytest.mark.asyncio
    async def test_get_single_by_id(self, migrated_pg):
        """GET /api/admin/ee-modules/{id} returns the matching row."""
        from src.web_ui.app import create_app

        app = create_app()
        # Fetch list to find a valid id
        async with _async_client(app) as client:
            list_resp = await client.get("/api/admin/ee-modules")
        assert list_resp.status_code == 200
        entries = list_resp.json()
        assert entries

        first = entries[0]
        target_id = first["id"]

        async with _async_client(app) as client:
            resp = await client.get(f"/api/admin/ee-modules/{target_id}")

        assert resp.status_code == 200
        row = resp.json()
        assert row["id"] == target_id
        assert row["name"] == first["name"]

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_404(self, migrated_pg):
        """GET /api/admin/ee-modules/99999 returns 404."""
        from src.web_ui.app import create_app

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/api/admin/ee-modules/99999")

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 3. Create - unique violation
# ---------------------------------------------------------------------------


class TestCreateNewModuleUniqueViolation:
    @pytest.mark.asyncio
    async def test_create_new_module_unique_violation(self, migrated_pg):
        """POST with duplicate name returns 409 Conflict.

        'knowledge' is seeded by m13_011 backfill.
        """
        from src.web_ui.app import create_app

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(
                "/api/admin/ee-modules",
                json={"name": "knowledge", "reason": "duplicate test"},
            )

        assert resp.status_code == 409, (
            f"Expected 409 for duplicate name, got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# 4. Create - success
# ---------------------------------------------------------------------------


_NEW_MODULE_NAME = "test_new_ee_module_wi7_unique"


class TestCreateNewModuleSuccess:
    @pytest.mark.asyncio
    async def test_create_new_module_success(self, migrated_pg):
        """POST with a unique name creates the entry and returns id."""
        from src.web_ui.app import create_app

        # Cleanup in case a prior failed run left this row
        _delete_ee_module_by_name(migrated_pg, _NEW_MODULE_NAME)
        invalidate_ee_modules_cache()

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(
                "/api/admin/ee-modules",
                json={
                    "name": _NEW_MODULE_NAME,
                    "vt_equivalent": "viin_test_wi7",
                    "reason": "WI-7 integration test create",
                },
            )

        assert resp.status_code == 200, (
            f"Expected 200 on create, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert "id" in body
        assert body["name"] == _NEW_MODULE_NAME
        assert body["created"] is True

        # Verify it shows up in the list
        invalidate_ee_modules_cache()
        async with _async_client(app) as client:
            list_resp = await client.get("/api/admin/ee-modules")
        names = [e["name"] for e in list_resp.json()]
        assert _NEW_MODULE_NAME in names

        # Cleanup
        _delete_ee_module_by_name(migrated_pg, _NEW_MODULE_NAME)
        invalidate_ee_modules_cache()


# ---------------------------------------------------------------------------
# 5. Patch - invalidates cache
# ---------------------------------------------------------------------------


class TestPatchModuleInvalidatesCache:
    @pytest.mark.asyncio
    async def test_patch_module_invalidates_get_ee_modules_cache(self, migrated_pg):
        """After PATCH, the cached get_ee_modules() consumer sees the new value.

        Business contract: get_ee_modules() caches rows 60s in-process and is the
        path MCP's check_module_exists reads. An admin PATCH must invalidate that
        cache so the next get_ee_modules() returns fresh DB rows, not the stale
        snapshot — otherwise an edit is invisible to MCP for up to 60s. This
        drives the real PATCH route and asserts the observable effect on the
        cached consumer (not that an internal helper was called). If the route's
        post-write invalidation regresses, get_ee_modules() serves the primed
        stale rows and this fails.
        """
        from src.data.ee_modules import get_ee_modules
        from src.web_ui.app import create_app

        app = create_app()
        new_description = "patched by WI-7 test"

        try:
            async with _async_client(app) as client:
                list_resp = await client.get("/api/admin/ee-modules")
            entries = {e["name"]: e for e in list_resp.json()}
            assert "knowledge" in entries, "Expected 'knowledge' in seeded EE modules"
            knowledge_id = entries["knowledge"]["id"]

            # Prime the in-process cache used by MCP's consumer path.
            primed = {m["name"]: m for m in get_ee_modules(migrated_pg)}
            assert primed["knowledge"].get("description") != new_description

            async with _async_client(app) as client:
                resp = await client.patch(
                    f"/api/admin/ee-modules/{knowledge_id}",
                    json={"description": new_description, "reason": "test PATCH"},
                )
            assert resp.status_code == 200, (
                f"Expected 200 on PATCH, got {resp.status_code}: {resp.text}"
            )

            # Cached read must reflect the patch — proving the route invalidated
            # the cache rather than leaving get_ee_modules() to serve stale rows.
            after = {m["name"]: m for m in get_ee_modules(migrated_pg)}
            assert after["knowledge"]["description"] == new_description, (
                "get_ee_modules() served a stale cached value — PATCH did not "
                "invalidate the cache"
            )
        finally:
            # Undo the patch to keep DB clean for other tests
            with migrated_pg.cursor() as cur:
                cur.execute(
                    "UPDATE ee_modules SET description = NULL WHERE name = 'knowledge'"
                )
            migrated_pg.commit()
            invalidate_ee_modules_cache()


# ---------------------------------------------------------------------------
# 6. Soft-delete - excluded from list by default
# ---------------------------------------------------------------------------


_SOFT_DELETE_MODULE = "wi7_test_soft_delete_excl"


class TestSoftDeleteExcludesFromListDefault:
    @pytest.mark.asyncio
    async def test_soft_delete_excludes_from_list_default(self, migrated_pg):
        """DELETE /{id} soft-deletes; the module no longer appears in default list."""
        from src.web_ui.app import create_app

        # Insert a fresh module (autocommit=True, no explicit commit needed)
        _delete_ee_module_by_name(migrated_pg, _SOFT_DELETE_MODULE)  # idempotent pre-clean
        module_id = _seed_ee_module(migrated_pg, name=_SOFT_DELETE_MODULE)
        invalidate_ee_modules_cache()

        app = create_app()
        async with _async_client(app) as client:
            del_resp = await client.delete(f"/api/admin/ee-modules/{module_id}")

        assert del_resp.status_code == 200
        body = del_resp.json()
        assert body["soft_deleted"] is True
        assert body["id"] == module_id

        # Must not appear in default list
        invalidate_ee_modules_cache()
        async with _async_client(app) as client:
            list_resp = await client.get("/api/admin/ee-modules")
        names = [e["name"] for e in list_resp.json()]
        assert _SOFT_DELETE_MODULE not in names

        # Cleanup
        _delete_ee_module_by_name(migrated_pg, _SOFT_DELETE_MODULE)
        invalidate_ee_modules_cache()


# ---------------------------------------------------------------------------
# 7. Soft-delete - included when query param true
# ---------------------------------------------------------------------------


_DEPRECATED_MODULE = "wi7_test_deprecated_vis"


class TestSoftDeleteIncludedWhenQueryParamTrue:
    @pytest.mark.asyncio
    async def test_soft_delete_included_when_query_param_true(self, migrated_pg):
        """GET /?include_deprecated=true returns deprecated entries."""
        from src.web_ui.app import create_app

        # Insert as already deprecated
        _delete_ee_module_by_name(migrated_pg, _DEPRECATED_MODULE)
        _seed_ee_module(migrated_pg, name=_DEPRECATED_MODULE, deprecated=True)
        invalidate_ee_modules_cache()

        app = create_app()
        # Default list must NOT include it
        async with _async_client(app) as client:
            default_resp = await client.get("/api/admin/ee-modules")
        default_names = [e["name"] for e in default_resp.json()]
        assert _DEPRECATED_MODULE not in default_names

        # include_deprecated=true must include it
        async with _async_client(app) as client:
            full_resp = await client.get(
                "/api/admin/ee-modules?include_deprecated=true"
            )
        full_names = [e["name"] for e in full_resp.json()]
        assert _DEPRECATED_MODULE in full_names

        # Cleanup
        _delete_ee_module_by_name(migrated_pg, _DEPRECATED_MODULE)
        invalidate_ee_modules_cache()


# ---------------------------------------------------------------------------
# 8. Non-admin 403
# ---------------------------------------------------------------------------


class TestNonAdmin403:
    @pytest.mark.asyncio
    async def test_non_admin_403(self, migrated_pg):
        """require_admin dependency returns 403 for a non-admin authenticated user.

        Uses the direct Depends test pattern (not full HTTP) to avoid needing
        a real session cookie: patches is_test_bypass_active + current_user_id,
        and mocks auth_store().get_user_field to return is_admin=False.
        """
        from fastapi import HTTPException
        from starlette.requests import Request as StarletteRequest

        import src.web_ui.auth as auth_mod

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/admin/ee-modules",
            "headers": [],
            "query_string": b"",
        }
        fake_request = StarletteRequest(scope)

        # Patch bypass OFF; mock auth_store (imported inside require_admin via
        # `from src.db.pg import auth_store`) to return is_admin=False.
        orig_bypass = auth_mod.is_test_bypass_active
        orig_cuid = auth_mod.current_user_id
        raised: HTTPException | None = None
        try:
            auth_mod.is_test_bypass_active = lambda: False
            auth_mod.current_user_id = lambda req: 42  # arbitrary non-admin user id

            mock_store = MagicMock()
            mock_store.get_user_field.return_value = False  # is_admin=False

            # auth_store is locally imported inside require_admin from src.db.pg
            with patch("src.db.pg.auth_store", return_value=mock_store):
                try:
                    await auth_mod.require_admin(fake_request)
                except HTTPException as exc:
                    raised = exc
        finally:
            auth_mod.is_test_bypass_active = orig_bypass
            auth_mod.current_user_id = orig_cuid

        assert raised is not None, "require_admin must raise HTTPException for non-admin"
        assert raised.status_code == 403
