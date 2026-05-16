"""Admin audit log helper + decorator (M9 W-AL — ADR-0021).

Single source of truth for writing to admin_audit_log table.
Supports both Web UI session actors (user:<id>), CLI actors (cli:<os_user>),
MCP API key actors (api_key:<prefix>), and OAuth callbacks (oauth:<provider>).

Use cases:
  - As decorator on route handlers: @audit_action("user.delete")
  - As context manager: with audit_cli("operations.backup") as ctx: ctx.detail[...] = ...
  - Direct call: write_audit_log(actor, action, target, success, detail)

Failures are logged at WARNING and swallowed — audit log must not break the
main request flow per ADR-0021 §Failure Mode.

Canonical actor formats:
  - "user:<id>"          Web UI session (numeric user ID)
  - "cli:<os_user>"      Manager CLI commands / cli.py
  - "api_key:<prefix>"   MCP requests authenticated via API key
  - "oauth:<provider>"   OAuth callback handlers (google, github)
  - "anonymous"          No session / unresolvable context

Action taxonomy — see ADR-0021 for full list:
  user.*         Login, logout, register, reset_password, delete, deactivate, reactivate
  profile.*      Create, update, delete, clone, set_parent, clone_all
  repo.*         Create, update, delete, clone
  api_key.*      Create, deactivate
  ssh_key.*      Create, import, delete
  oauth.*        login.google, login.github
  totp.*         Setup, verify, disable
  operations.*   Backup, restore, apply_preset, index_repo, index_core, seed_patterns, reset_embed
  fernet.*       Rotate
"""

import json
import logging
import os
from collections.abc import Callable
from contextlib import contextmanager
from functools import wraps

from starlette.requests import Request

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core INSERT function
# ---------------------------------------------------------------------------


def write_audit_log(
    actor: str,
    action: str,
    target: str | None = None,
    success: bool = True,
    detail: dict | None = None,
) -> None:
    """Synchronously INSERT into admin_audit_log. Failure → WARNING log only.

    Uses a dedicated pool.checkout() connection that is independent of any
    caller transaction — so a caller ROLLBACK does not lose the audit row.

    Args:
        actor: Canonical actor string (e.g. "user:42", "cli:tuan").
        action: Canonical action name (e.g. "user.login", "profile.delete").
        target: Optional target identifier (user_id, profile_name, etc.).
        success: True if the action succeeded; False on failure/rejection.
        detail: Optional JSONB-serializable dict for forensic context.
    """
    from src.db.pg import auth_store

    try:
        pool = auth_store()._pool
        with pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO admin_audit_log "
                    "(actor, action, target, success, detail, created_at) "
                    "VALUES (%s, %s, %s, %s, %s::jsonb, NOW())",
                    (actor, action, target, success, json.dumps(detail or {})),
                )
            conn.commit()
    except Exception as exc:
        logger.warning("admin_audit_log INSERT failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Actor resolution
# ---------------------------------------------------------------------------


def resolve_actor(
    request: Request | None = None,
    *,
    cli: bool = False,
    api_key_prefix: str | None = None,
    oauth_provider: str | None = None,
) -> str:
    """Resolve canonical actor string from context.

    Priority order:
      1. cli=True  →  "cli:<os_user>" (manager CLI commands)
      2. api_key_prefix  →  "api_key:<prefix>" (MCP requests)
      3. oauth_provider  →  "oauth:<provider>" (OAuth callback handlers)
      4. request session  →  "user:<id>" (Web UI requests)
      5. fallback  →  "anonymous"

    Args:
        request: Optional Starlette/FastAPI Request object.
        cli: Set True for CLI context (reads os.getlogin / USER env).
        api_key_prefix: Short key prefix if call is from an API key context.
        oauth_provider: OAuth provider name ("google", "github") for callbacks.

    Returns:
        Canonical actor string.
    """
    if cli:
        try:
            return f"cli:{os.getlogin()}"
        except OSError:
            return f"cli:{os.environ.get('USER', 'unknown')}"

    if api_key_prefix:
        return f"api_key:{api_key_prefix}"

    if oauth_provider:
        return f"oauth:{oauth_provider}"

    if request is not None:
        try:
            user_id = request.session.get("user_id")
            if user_id:
                return f"user:{user_id}"
            username = request.session.get("username")
            if username:
                return f"user:{username}"
        except (AssertionError, AttributeError):
            pass

    return "anonymous"


# ---------------------------------------------------------------------------
# Decorator for FastAPI async route handlers
# ---------------------------------------------------------------------------


def audit_action(action: str, *, target_param: str | None = None) -> Callable:
    """Decorator: wrap a FastAPI async handler to write an audit log entry.

    Wraps the handler's success/failure and writes one row to admin_audit_log
    after the handler returns or raises. The decorator is *non-intrusive*:
    the original return value and exceptions are always propagated unchanged.

    Args:
        action: Canonical action name (e.g. 'user.login', 'profile.delete').
        target_param: Optional path/query parameter name to extract as audit
            target. Looks first in request.path_params, then in kwargs.
            Example: target_param='user_id' on a route  /users/{user_id}
            → audit row target = str(path_params['user_id']).

    Behavior:
        - Normal return  →  success=True, detail={ip, user_agent, status_code}.
        - HTTPException  →  success=False, detail={status_code, reason}.
        - Other exception  →  success=False, detail={error_type, error_message}.
        - Re-raises original exception always.

    Extra forensic detail (before/after snapshots):
        Handlers can enrich the audit record by calling
        ``request.state.audit_detail.update({...})`` before returning.
        The decorator merges request.state.audit_detail into the detail dict.
        Only safe, non-sensitive fields should be placed there (url, branch,
        ssh_key_id, profile name/version — NOT passwords or private keys).

    Note:
        Only supports async def handlers (all M9 FastAPI routes are async).
        Sync handlers are not supported and will raise TypeError at decoration time
        if wrapped — do not use on sync functions.
    """
    def decorator(handler: Callable) -> Callable:
        @wraps(handler)
        async def wrapper(*args, **kwargs):
            # Locate Request object — FastAPI injects it as a keyword or positional arg
            request: Request | None = kwargs.get("request")
            if request is None:
                for arg in args:
                    if isinstance(arg, Request):
                        request = arg
                        break

            actor = resolve_actor(request)

            # Resolve target from path params if requested
            target: str | None = None
            if target_param and request is not None:
                val = (
                    request.path_params.get(target_param)
                    or kwargs.get(target_param)
                )
                if val is not None:
                    target = str(val)

            # Build base detail dict
            detail: dict = {}
            if request is not None:
                try:
                    from src.web_ui.login_attempts import get_client_ip
                    detail["ip"] = get_client_ip(request)
                except Exception:
                    pass
                ua = request.headers.get("user-agent", "")
                if ua:
                    detail["user_agent"] = ua[:200]
                # Prepare state slot for handler-injected forensic detail
                try:
                    request.state.audit_detail = {}
                except Exception:
                    pass

            try:
                result = await handler(*args, **kwargs)
                # Best-effort: extract HTTP status_code from JSONResponse
                status = getattr(result, "status_code", None)
                if status is not None:
                    detail["status_code"] = status
                # Merge handler-injected forensic detail (before/after snapshots)
                if request is not None:
                    try:
                        handler_extra = getattr(request.state, "audit_detail", {})
                        if handler_extra:
                            detail.update(handler_extra)
                    except Exception:
                        pass
                # success criterion: no status_code (treated as 200) OR status < 400
                ok = (status is None) or (status < 400)
                write_audit_log(actor, action, target, ok, detail)
                return result

            except Exception as exc:
                from fastapi import HTTPException

                # Merge any partial forensic detail written before the exception
                if request is not None:
                    try:
                        handler_extra = getattr(request.state, "audit_detail", {})
                        if handler_extra:
                            detail.update(handler_extra)
                    except Exception:
                        pass
                if isinstance(exc, HTTPException):
                    detail["status_code"] = exc.status_code
                    detail["reason"] = str(exc.detail)
                else:
                    detail["error_type"] = type(exc).__name__
                    detail["error_message"] = str(exc)[:500]
                write_audit_log(actor, action, target, success=False, detail=detail)
                raise

        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Context manager for CLI commands
# ---------------------------------------------------------------------------


@contextmanager
def audit_cli(action: str, target: str | None = None):
    """Context manager for CLI commands (manager/__main__.py + cli.py).

    Writes one audit row on context exit (success or exception). CLI actor is
    resolved from os.getlogin() / USER env var.

    Usage:
        with audit_cli("profile.delete", target=profile_name) as ctx:
            store.delete_profile(profile_id)
            ctx.detail["row_count"] = 1

    On exception inside the block:
        - detail["error_type"] and detail["error_message"] are set automatically.
        - success=False is recorded.
        - The exception is re-raised unchanged.
    """
    class _Ctx:
        def __init__(self):
            self.detail: dict = {}
            self.success: bool = True

    ctx = _Ctx()
    actor = resolve_actor(cli=True)
    try:
        yield ctx
        write_audit_log(actor, action, target, ctx.success, ctx.detail)
    except Exception as exc:
        ctx.detail["error_type"] = type(exc).__name__
        ctx.detail["error_message"] = str(exc)[:500]
        write_audit_log(actor, action, target, success=False, detail=ctx.detail)
        raise
