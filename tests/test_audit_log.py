# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for src/db/audit.py — write_audit_log, resolve_actor, @audit_action, audit_cli.

These tests are fully unit-level (no Docker / no real DB). All DB calls are mocked.

Integration tests requiring PostgreSQL are in test_audit_log_integration.py.
"""

import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_pool():
    """Return a mock PgPool with a checkout() context manager and commit-able connection."""
    mock_cur = MagicMock()
    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    mock_pool = MagicMock()

    @contextmanager
    def _checkout():
        yield mock_conn

    mock_pool.checkout = _checkout
    return mock_pool, mock_conn, mock_cur


def _make_mock_auth_store(pool):
    """Return a mock auth_store() that returns an object with ._pool = pool."""
    mock_store = MagicMock()
    mock_store._pool = pool
    return mock_store


def _make_mock_request(user_id=None, username=None):
    """Return a mock Starlette Request with a session dict."""
    req = MagicMock()
    session = {}
    if user_id is not None:
        session["user_id"] = user_id
    if username is not None:
        session["username"] = username
    req.session = session
    req.headers = {}
    req.path_params = {}
    return req


# ---------------------------------------------------------------------------
# write_audit_log
# ---------------------------------------------------------------------------


class TestWriteAuditLog:
    def test_write_audit_log_inserts_row(self):
        """Direct call → cursor.execute is called with correct SQL + params."""
        pool, conn, cur = _make_mock_pool()
        store = _make_mock_auth_store(pool)

        # auth_store is imported lazily inside write_audit_log → patch at src.db.pg
        with patch("src.db.pg.auth_store", return_value=store):
            from src.db.audit import write_audit_log

            write_audit_log(
                actor="user:42",
                action="user.login",
                target=None,
                success=True,
                detail={"ip": "1.2.3.4"},
            )

        cur.execute.assert_called_once()
        call_args = cur.execute.call_args
        sql = call_args[0][0]
        params = call_args[0][1]
        assert "admin_audit_log" in sql
        assert params[0] == "user:42"
        assert params[1] == "user.login"
        assert params[2] is None
        assert params[3] is True
        conn.commit.assert_called_once()

    def test_write_audit_log_failure_swallowed(self):
        """If pool.checkout() raises, write_audit_log must NOT propagate — only log WARNING."""
        pool = MagicMock()
        pool.checkout.side_effect = RuntimeError("DB is down")
        store = MagicMock()
        store._pool = pool

        with patch("src.db.pg.auth_store", return_value=store):
            from src.db.audit import write_audit_log

            # Must not raise
            write_audit_log("user:1", "user.login", success=False, detail={})

    def test_write_audit_log_default_empty_detail(self):
        """detail=None → serialized as '{}'."""
        pool, conn, cur = _make_mock_pool()
        store = _make_mock_auth_store(pool)

        with patch("src.db.pg.auth_store", return_value=store):
            from src.db.audit import write_audit_log
            write_audit_log("anonymous", "user.login")

        params = cur.execute.call_args[0][1]
        import json
        assert json.loads(params[4]) == {}


# ---------------------------------------------------------------------------
# resolve_actor
# ---------------------------------------------------------------------------


class TestResolveActor:
    def test_resolve_actor_cli(self):
        """cli=True → 'cli:<os_user>'."""
        from src.db.audit import resolve_actor

        with patch.dict(os.environ, {"USER": "tuan"}, clear=False):
            with patch("os.getlogin", return_value="tuan"):
                result = resolve_actor(cli=True)
        assert result == "cli:tuan"

    def test_resolve_actor_cli_fallback_env(self):
        """cli=True and os.getlogin() raises OSError → fallback to USER env."""
        from src.db.audit import resolve_actor

        with patch.dict(os.environ, {"USER": "testuser"}, clear=False):
            with patch("os.getlogin", side_effect=OSError("no tty")):
                result = resolve_actor(cli=True)
        assert result == "cli:testuser"

    def test_resolve_actor_api_key(self):
        """api_key_prefix set → 'api_key:<prefix>'."""
        from src.db.audit import resolve_actor

        result = resolve_actor(api_key_prefix="osm_abc123")
        assert result == "api_key:osm_abc123"

    def test_resolve_actor_oauth(self):
        """oauth_provider set → 'oauth:<provider>'."""
        from src.db.audit import resolve_actor

        result = resolve_actor(oauth_provider="google")
        assert result == "oauth:google"

    def test_resolve_actor_session_user_id(self):
        """request.session['user_id'] present → 'user:<id>'."""
        from src.db.audit import resolve_actor

        req = _make_mock_request(user_id=42)
        result = resolve_actor(req)
        assert result == "user:42"

    def test_resolve_actor_session_username_fallback(self):
        """No user_id but username → 'user:<username>'."""
        from src.db.audit import resolve_actor

        req = _make_mock_request(username="admin")
        result = resolve_actor(req)
        assert result == "user:admin"

    def test_resolve_actor_anonymous(self):
        """No context → 'anonymous'."""
        from src.db.audit import resolve_actor

        result = resolve_actor(None)
        assert result == "anonymous"

    def test_resolve_actor_request_no_session(self):
        """Request with broken session (AssertionError) → 'anonymous'."""
        from src.db.audit import resolve_actor

        req = MagicMock()
        req.session = MagicMock()
        req.session.get.side_effect = AssertionError("no session middleware")
        result = resolve_actor(req)
        assert result == "anonymous"

    def test_resolve_actor_priority_cli_over_request(self):
        """cli=True takes priority over request session."""
        from src.db.audit import resolve_actor

        req = _make_mock_request(user_id=99)
        with patch("os.getlogin", return_value="root"):
            result = resolve_actor(req, cli=True)
        assert result.startswith("cli:")

    def test_resolve_actor_priority_api_key_over_request(self):
        """api_key_prefix takes priority over request session."""
        from src.db.audit import resolve_actor

        req = _make_mock_request(user_id=99)
        result = resolve_actor(req, api_key_prefix="osm_xyz")
        assert result == "api_key:osm_xyz"


# ---------------------------------------------------------------------------
# @audit_action decorator
# ---------------------------------------------------------------------------


class TestAuditActionDecorator:
    """Tests for the @audit_action decorator on async FastAPI handlers."""

    def _setup_write_mock(self):
        """Return (mock_write_audit_log, patch context)."""
        return patch("src.db.audit.write_audit_log")

    @pytest.mark.asyncio
    async def test_audit_action_decorator_success(self):
        """Handler returns 200 JSONResponse → write_audit_log called with success=True."""
        from fastapi.responses import JSONResponse

        from src.db.audit import audit_action

        @audit_action("user.login")
        async def handler(request):
            return JSONResponse({"ok": True}, status_code=200)  # noqa  - test stub (lint-json-response bypass: no datetime)

        req = _make_mock_request(user_id=5)
        req.headers = {}

        with patch("src.db.audit.write_audit_log") as mock_write:
            with patch("src.db.audit.resolve_actor", return_value="user:5"):
                result = await handler(request=req)

        assert result.status_code == 200
        mock_write.assert_called_once()
        call_kwargs = mock_write.call_args
        # Positional: actor, action, target, success, detail
        args = call_kwargs[0]
        assert args[1] == "user.login"
        assert args[3] is True  # success

    @pytest.mark.asyncio
    async def test_audit_action_decorator_http_exception(self):
        """Handler raises HTTPException 403 → write_audit_log called with success=False."""
        from fastapi import HTTPException

        from src.db.audit import audit_action

        @audit_action("user.deactivate")
        async def handler(request):
            raise HTTPException(status_code=403, detail="Forbidden")

        req = _make_mock_request(user_id=1)
        req.headers = {}

        with patch("src.db.audit.write_audit_log") as mock_write:
            with patch("src.db.audit.resolve_actor", return_value="user:1"):
                with pytest.raises(HTTPException) as exc_info:
                    await handler(request=req)

        assert exc_info.value.status_code == 403
        mock_write.assert_called_once()
        # Exception path calls: write_audit_log(actor, action, target, success=False, detail=...)
        call = mock_write.call_args
        # success is passed as keyword arg on the exception path
        assert call.kwargs.get("success") is False or (
            len(call.args) > 3 and call.args[3] is False
        )
        detail = call.kwargs.get("detail") or (call.args[4] if len(call.args) > 4 else None)
        assert detail is not None
        assert detail["status_code"] == 403
        assert "Forbidden" in detail["reason"]

    @pytest.mark.asyncio
    async def test_audit_action_decorator_unhandled_exception(self):
        """Handler raises ValueError → write_audit_log with success=False, ValueError propagates."""
        from src.db.audit import audit_action

        @audit_action("profile.delete")
        async def handler(request):
            raise ValueError("something broke")

        req = _make_mock_request(user_id=1)
        req.headers = {}

        with patch("src.db.audit.write_audit_log") as mock_write:
            with patch("src.db.audit.resolve_actor", return_value="user:1"):
                with pytest.raises(ValueError, match="something broke"):
                    await handler(request=req)

        mock_write.assert_called_once()
        call = mock_write.call_args
        # success is passed as keyword arg on the exception path
        assert call.kwargs.get("success") is False or (
            len(call.args) > 3 and call.args[3] is False
        )
        detail = call.kwargs.get("detail") or (call.args[4] if len(call.args) > 4 else None)
        assert detail is not None
        assert detail["error_type"] == "ValueError"
        assert "something broke" in detail["error_message"]

    @pytest.mark.asyncio
    async def test_audit_action_target_param(self):
        """target_param='user_id' extracts path param → audit row target='5'."""
        from fastapi.responses import JSONResponse

        from src.db.audit import audit_action

        @audit_action("user.delete", target_param="user_id")
        async def handler(request, user_id: int):
            return JSONResponse({"ok": True})  # noqa  - test stub (lint-json-response bypass: no datetime)

        req = _make_mock_request(user_id=1)
        req.headers = {}
        req.path_params = {"user_id": 5}

        with patch("src.db.audit.write_audit_log") as mock_write:
            with patch("src.db.audit.resolve_actor", return_value="user:1"):
                await handler(request=req, user_id=5)

        args = mock_write.call_args[0]
        # target should be "5" (str of path param)
        assert args[2] == "5"

    @pytest.mark.asyncio
    async def test_audit_action_target_param_from_kwargs(self):
        """target_param falls back to kwargs when not in path_params."""
        from fastapi.responses import JSONResponse

        from src.db.audit import audit_action

        @audit_action("repo.delete", target_param="repo_id")
        async def handler(request, repo_id: int):
            return JSONResponse({"ok": True})  # noqa  - test stub (lint-json-response bypass: no datetime)

        req = _make_mock_request()
        req.headers = {}
        req.path_params = {}  # not in path_params

        with patch("src.db.audit.write_audit_log") as mock_write:
            with patch("src.db.audit.resolve_actor", return_value="anonymous"):
                await handler(request=req, repo_id=99)

        args = mock_write.call_args[0]
        assert args[2] == "99"

    @pytest.mark.asyncio
    async def test_audit_action_preserves_return_value(self):
        """Decorator must not alter the handler's return value."""
        from fastapi.responses import JSONResponse

        from src.db.audit import audit_action

        @audit_action("api_key.create")
        async def handler(request):
            return JSONResponse({"key": "secret"}, status_code=201)  # noqa  - test stub (lint-json-response bypass: no datetime)

        req = _make_mock_request()
        req.headers = {}

        with patch("src.db.audit.write_audit_log"):
            with patch("src.db.audit.resolve_actor", return_value="anonymous"):
                result = await handler(request=req)

        assert result.status_code == 201


# ---------------------------------------------------------------------------
# audit_cli context manager
# ---------------------------------------------------------------------------


class TestAuditCli:
    def test_audit_cli_context_manager_success(self):
        """Normal exit → write_audit_log called with success=True."""
        with patch("src.db.audit.write_audit_log") as mock_write:
            with patch("src.db.audit.resolve_actor", return_value="cli:tuan"):
                from src.db.audit import audit_cli

                with audit_cli("profile.delete", target="odoo17") as ctx:
                    ctx.detail["profile_id"] = 1

        mock_write.assert_called_once()
        args = mock_write.call_args[0]
        assert args[0] == "cli:tuan"
        assert args[1] == "profile.delete"
        assert args[2] == "odoo17"
        assert args[3] is True  # success

    def test_audit_cli_context_manager_exception(self):
        """Exception inside block → write_audit_log with success=False, exception propagates."""
        with patch("src.db.audit.write_audit_log") as mock_write:
            with patch("src.db.audit.resolve_actor", return_value="cli:root"):
                from src.db.audit import audit_cli

                with pytest.raises(ValueError, match="oops"):
                    with audit_cli("repo.delete", target="42"):
                        raise ValueError("oops")

        mock_write.assert_called_once()
        call = mock_write.call_args
        # audit_cli exception path: write_audit_log(..., success=False, detail=...)
        assert call.kwargs.get("success") is False or (
            len(call.args) > 3 and call.args[3] is False
        )
        detail = call.kwargs.get("detail") or (call.args[4] if len(call.args) > 4 else None)
        assert detail is not None
        assert detail["error_type"] == "ValueError"
        assert "oops" in detail["error_message"]

    def test_audit_cli_default_no_target(self):
        """audit_cli without target → target=None in write_audit_log."""
        with patch("src.db.audit.write_audit_log") as mock_write:
            with patch("src.db.audit.resolve_actor", return_value="cli:tuan"):
                from src.db.audit import audit_cli

                with audit_cli("fernet.rotate"):
                    pass

        args = mock_write.call_args[0]
        assert args[2] is None  # target

    def test_audit_cli_custom_success_false(self):
        """ctx.success = False manually sets success=False without exception."""
        with patch("src.db.audit.write_audit_log") as mock_write:
            with patch("src.db.audit.resolve_actor", return_value="cli:tuan"):
                from src.db.audit import audit_cli

                with audit_cli("profile.delete") as ctx:
                    ctx.success = False
                    ctx.detail["reason"] = "dry_run"

        args = mock_write.call_args[0]
        assert args[3] is False  # success=False as set manually
