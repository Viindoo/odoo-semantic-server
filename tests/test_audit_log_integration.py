# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for admin_audit_log via src/db/audit.py (M9 W-AL).

Requires PostgreSQL (pytestmark = pytest.mark.postgres).
Tests verify that write_audit_log actually inserts rows and that Web UI
login/CLI user-delete routes produce the correct audit trail.
"""

import os

import httpx
import pytest

pytestmark = pytest.mark.postgres


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------


def _ensure_audit_log_table(conn) -> None:
    """Ensure admin_audit_log table exists with canonical columns."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS admin_audit_log (
                id         BIGSERIAL PRIMARY KEY,
                actor      TEXT NOT NULL,
                action     TEXT NOT NULL,
                target     TEXT,
                success    BOOLEAN NOT NULL DEFAULT TRUE,
                detail     JSONB,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                -- legacy W-UM columns (kept for rollback safety, deprecated per ADR-0021)
                actor_id   INTEGER,
                target_id  INTEGER,
                detail_text TEXT
            )
        """)
        # Ensure canonical columns exist on pre-existing tables (idempotent)
        cur.execute("ALTER TABLE admin_audit_log ADD COLUMN IF NOT EXISTS actor TEXT")
        cur.execute("ALTER TABLE admin_audit_log ADD COLUMN IF NOT EXISTS action TEXT")
        cur.execute("ALTER TABLE admin_audit_log ADD COLUMN IF NOT EXISTS target TEXT")
        cur.execute("ALTER TABLE admin_audit_log ADD COLUMN IF NOT EXISTS success BOOLEAN")
        cur.execute("ALTER TABLE admin_audit_log ADD COLUMN IF NOT EXISTS detail JSONB")
        cur.execute("ALTER TABLE admin_audit_log ADD COLUMN IF NOT EXISTS created_at TIMESTAMP")
    conn.commit()


def _count_audit_rows(conn, action: str | None = None) -> int:
    """Count rows in admin_audit_log, optionally filtered by action."""
    with conn.cursor() as cur:
        if action:
            cur.execute(
                "SELECT COUNT(*) FROM admin_audit_log WHERE action = %s",
                (action,),
            )
        else:
            cur.execute("SELECT COUNT(*) FROM admin_audit_log")
        return cur.fetchone()[0]


def _last_audit_row(conn, action: str) -> dict | None:
    """Return the most recent audit row for given action, or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT actor, action, target, success, detail, created_at "
            "FROM admin_audit_log WHERE action = %s "
            "ORDER BY id DESC LIMIT 1",
            (action,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "actor": row[0],
        "action": row[1],
        "target": row[2],
        "success": row[3],
        "detail": row[4],
        "created_at": row[5],
    }


# ---------------------------------------------------------------------------
# Direct write_audit_log tests
# ---------------------------------------------------------------------------


class TestWriteAuditLogIntegration:
    """Test write_audit_log against a real PostgreSQL database."""

    @pytest.fixture(autouse=True)
    def _setup_table(self, clean_pg):
        """Ensure admin_audit_log exists before each test."""
        from src.db.migrate import run_migrations
        run_migrations(clean_pg)
        _ensure_audit_log_table(clean_pg)
        self.conn = clean_pg

    def test_write_audit_log_inserts_row(self):
        """Direct call → row appears in admin_audit_log."""
        from src.db.audit import write_audit_log

        before = _count_audit_rows(self.conn)
        write_audit_log(
            actor="user:99",
            action="user.login",
            target=None,
            success=True,
            detail={"ip": "10.0.0.1"},
        )
        after = _count_audit_rows(self.conn)
        assert after == before + 1

        row = _last_audit_row(self.conn, "user.login")
        assert row is not None
        assert row["actor"] == "user:99"
        assert row["success"] is True

    def test_write_audit_log_detail_jsonb(self):
        """detail dict is stored as JSONB and retrievable."""
        import json

        from src.db.audit import write_audit_log

        write_audit_log(
            actor="cli:tuan",
            action="profile.delete",
            target="odoo17",
            success=True,
            detail={"profile_id": 42, "yes_flag": True},
        )

        row = _last_audit_row(self.conn, "profile.delete")
        assert row is not None
        assert row["target"] == "odoo17"
        # detail may be returned as dict (psycopg2 JSONB) or string
        detail = row["detail"]
        if isinstance(detail, str):
            detail = json.loads(detail)
        assert detail.get("profile_id") == 42

    def test_write_audit_log_success_false(self):
        """success=False is stored correctly."""
        from src.db.audit import write_audit_log

        write_audit_log(
            actor="anonymous",
            action="user.login",
            target=None,
            success=False,
            detail={"reason": "invalid_credentials"},
        )

        row = _last_audit_row(self.conn, "user.login")
        assert row is not None
        assert row["success"] is False

    def test_write_audit_log_multiple_rows(self):
        """Multiple calls produce multiple independent rows."""
        from src.db.audit import write_audit_log

        write_audit_log("user:1", "user.login", success=True)
        write_audit_log("user:2", "user.login", success=True)
        write_audit_log("user:3", "user.logout", success=True)

        login_count = _count_audit_rows(self.conn, "user.login")
        logout_count = _count_audit_rows(self.conn, "user.logout")
        assert login_count >= 2
        assert logout_count >= 1


# ---------------------------------------------------------------------------
# Web UI route integration tests
# ---------------------------------------------------------------------------


def _make_web_app(pg_conn):
    """Create app + patch pool to point at test DB."""
    from src.db.migrate import run_migrations
    from src.web_ui.app import create_app

    run_migrations(pg_conn)
    _ensure_audit_log_table(pg_conn)

    os.environ.setdefault("WEBUI_SESSION_SECRET", "test-secret-for-audit-tests-32bytes!!")
    os.environ.setdefault("WEBUI_AUTH_DISABLED", "1")
    return create_app()


class TestLoginAuditIntegration:
    """POST /api/auth/login → admin_audit_log row."""

    @pytest.fixture(autouse=True)
    def _setup(self, clean_pg):
        from src.db.migrate import run_migrations
        run_migrations(clean_pg)
        _ensure_audit_log_table(clean_pg)
        self.conn = clean_pg

    @pytest.mark.asyncio
    async def test_login_failure_writes_audit_row(self):
        """POST /api/auth/login with bad credentials → audit row success=False."""
        import unittest.mock as mock

        from src.web_ui.app import create_app

        os.environ["WEBUI_SESSION_SECRET"] = "test-secret-32bytes-for-audit-test!!"

        app = create_app()

        # Patch _lookup_user to return None (user not found)
        import src.web_ui.routes.login as login_mod

        with mock.patch.object(login_mod, "_lookup_user", return_value=None):
            with mock.patch.object(
                login_mod, "check_rate_limit", return_value=False
            ):
                with mock.patch.object(
                    login_mod, "record_login_attempt", return_value=None
                ):
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url="http://test"
                    ) as client:
                        resp = await client.post(
                            "/api/auth/login",
                            json={"username": "noone", "password": "badpassword123"},
                        )

        assert resp.status_code == 401

        # Verify audit row was written
        row = _last_audit_row(self.conn, "user.login")
        assert row is not None
        assert row["success"] is False


# ---------------------------------------------------------------------------
# CLI audit_cli context manager integration test
# ---------------------------------------------------------------------------


class TestAuditCliIntegration:
    """audit_cli context manager writes to real DB."""

    @pytest.fixture(autouse=True)
    def _setup(self, clean_pg):
        from src.db.migrate import run_migrations
        run_migrations(clean_pg)
        _ensure_audit_log_table(clean_pg)
        self.conn = clean_pg

    def test_audit_cli_writes_row_on_success(self):
        """with audit_cli(...) → row in admin_audit_log on normal exit."""
        import unittest.mock as mock

        from src.db.audit import audit_cli

        with mock.patch("os.getlogin", return_value="tuan"):
            with audit_cli("profile.delete", target="myprofile") as ctx:
                ctx.detail["profile_id"] = 7

        row = _last_audit_row(self.conn, "profile.delete")
        assert row is not None
        assert "cli:" in row["actor"]
        assert row["target"] == "myprofile"
        assert row["success"] is True

    def test_audit_cli_writes_row_on_exception(self):
        """Exception inside audit_cli block → success=False row + exception propagates."""
        import unittest.mock as mock

        from src.db.audit import audit_cli

        with mock.patch("os.getlogin", return_value="root"):
            with pytest.raises(RuntimeError, match="DB error"):
                with audit_cli("repo.delete", target="99"):
                    raise RuntimeError("DB error")

        row = _last_audit_row(self.conn, "repo.delete")
        assert row is not None
        assert row["success"] is False
        import json
        detail = row["detail"]
        if isinstance(detail, str):
            detail = json.loads(detail)
        assert detail.get("error_type") == "RuntimeError"
