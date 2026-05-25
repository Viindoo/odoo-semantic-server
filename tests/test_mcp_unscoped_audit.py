# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for MCP unscoped-path audit logging (ADR-0034 §D4, ADR-0021 §3).

Business rules under test:
  R-1: Global/admin key (tenant_id IS NULL) calling one MCP tool
       → exactly 1 row with action='mcp.query.unscoped' in admin_audit_log.
  R-2: Tenant-scoped key (tenant_id IS NOT NULL) calling one MCP tool
       → zero rows with action='mcp.query.unscoped'.
  R-3: Row fields for R-1: actor starts with 'api_key:', target=tool_name,
       success=True, detail contains 'tool' key.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_audit_log_table(conn) -> None:
    """Ensure admin_audit_log table exists (idempotent)."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS admin_audit_log (
                id         BIGSERIAL PRIMARY KEY,
                actor      TEXT NOT NULL,
                action     TEXT NOT NULL,
                target     TEXT,
                success    BOOLEAN NOT NULL DEFAULT TRUE,
                detail     JSONB,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
    conn.commit()


def _count_unscoped_rows(conn) -> int:
    """Count rows where action = 'mcp.query.unscoped'."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM admin_audit_log WHERE action = %s",
            ("mcp.query.unscoped",),
        )
        return cur.fetchone()[0]


def _last_unscoped_row(conn) -> dict | None:
    """Return the most recent mcp.query.unscoped row or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT actor, action, target, success, detail "
            "FROM admin_audit_log WHERE action = %s "
            "ORDER BY id DESC LIMIT 1",
            ("mcp.query.unscoped",),
        )
        row = cur.fetchone()
    if row is None:
        return None
    detail = row[4]
    if isinstance(detail, str):
        detail = json.loads(detail)
    return {
        "actor": row[0],
        "action": row[1],
        "target": row[2],
        "success": row[3],
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _setup_audit_table(clean_pg):
    """Ensure migrations + audit table exist before each test."""
    from src.db.migrate import run_migrations
    run_migrations(clean_pg)
    _ensure_audit_log_table(clean_pg)


# ---------------------------------------------------------------------------
# R-1 / R-3: global/admin key emits exactly 1 unscoped audit row
# ---------------------------------------------------------------------------


class TestUnscopedAuditEmitted:
    """R-1 + R-3: admin/global key (tenant_id=None) → 1 mcp.query.unscoped row."""

    @pytest.mark.asyncio
    async def test_global_key_emits_one_unscoped_row(self, clean_pg):
        """Calling _audit_unscoped_tool_call_async with key_prefix inserts 1 row."""
        from src.mcp.tool_log_middleware import _audit_unscoped_tool_call_async

        before = _count_unscoped_rows(clean_pg)

        await _audit_unscoped_tool_call_async(
            key_prefix="osm_abc12345",
            tool_name="model_inspect",
        )

        after = _count_unscoped_rows(clean_pg)
        assert after == before + 1, (
            f"Expected exactly 1 new mcp.query.unscoped row; got {after - before}"
        )

    @pytest.mark.asyncio
    async def test_global_key_row_fields(self, clean_pg):
        """R-3: actor, target, success, detail are correct."""
        from src.mcp.tool_log_middleware import _audit_unscoped_tool_call_async

        await _audit_unscoped_tool_call_async(
            key_prefix="osm_prefix99",
            tool_name="find_examples",
        )

        row = _last_unscoped_row(clean_pg)
        assert row is not None
        assert row["actor"] == "api_key:osm_prefix99", (
            f"actor mismatch: {row['actor']!r}"
        )
        assert row["action"] == "mcp.query.unscoped"
        assert row["target"] == "find_examples"
        assert row["success"] is True
        assert isinstance(row["detail"], dict)
        assert row["detail"].get("tool") == "find_examples", (
            f"detail['tool'] missing or wrong: {row['detail']}"
        )

    @pytest.mark.asyncio
    async def test_global_key_unknown_prefix_fallback(self, clean_pg):
        """key_prefix=None → actor='api_key:unknown' (stdio/no-HTTP transport)."""
        from src.mcp.tool_log_middleware import _audit_unscoped_tool_call_async

        await _audit_unscoped_tool_call_async(
            key_prefix=None,
            tool_name="lint_check",
        )

        row = _last_unscoped_row(clean_pg)
        assert row is not None
        assert row["actor"] == "api_key:unknown"


# ---------------------------------------------------------------------------
# R-2: tenant-scoped key → zero unscoped audit rows
# ---------------------------------------------------------------------------


class TestScopedKeyNoUnscopedAudit:
    """R-2: tenant_id IS NOT NULL path must NOT emit mcp.query.unscoped rows."""

    @pytest.mark.asyncio
    async def test_scoped_key_does_not_emit_unscoped_row(self, clean_pg):
        """Simulate on_call_tool for a tenant-scoped key; audit must not fire.

        We patch get_http_request so the middleware reads tenant_id=42 (non-None),
        then verify _audit_unscoped_tool_call_async is never scheduled.
        """
        import mcp.types as mt
        from fastmcp.server.middleware import MiddlewareContext

        from src.mcp.tool_log_middleware import UsageLogMiddleware

        before = _count_unscoped_rows(clean_pg)

        # Build a minimal fake request state: tenant_id set (scoped key)
        fake_state = MagicMock()
        fake_state.api_key_id = 1
        fake_state.tenant_id = 42        # <-- tenant-scoped: audit must NOT fire
        fake_state.key_prefix = "osm_tenantkey"

        fake_req = MagicMock()
        fake_req.state = fake_state

        fake_context = MagicMock(spec=MiddlewareContext)
        fake_context.message = MagicMock(spec=mt.CallToolRequestParams)
        fake_context.message.name = "model_inspect"

        dummy_result = MagicMock()
        call_next = AsyncMock(return_value=dummy_result)

        with patch(
            "src.mcp.tool_log_middleware.get_http_request", return_value=fake_req
        ):
            # Patch usage log to avoid DB interaction
            with patch(
                "src.mcp.tool_log_middleware._log_tool_call_async",
                new_callable=AsyncMock,
            ):
                mw = UsageLogMiddleware()
                await mw.on_call_tool(fake_context, call_next)

        # Allow any background tasks to complete
        await asyncio.sleep(0.1)

        after = _count_unscoped_rows(clean_pg)
        assert after == before, (
            f"Scoped key must NOT emit unscoped audit row; got {after - before} new rows"
        )
