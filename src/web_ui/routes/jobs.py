# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/routes/jobs.py
"""Jobs router (M8 — extracted from repos.py per Phase 8 review).

Reason: client polls /api/jobs/{id}/status; original prefix "/api/repos"
caused 404 + stuck job-status banner. Moving to a dedicated router with
prefix="/api/jobs" fixes the URL mismatch without changing client code.
"""
import datetime as _dt
import logging
import os
import re

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from starlette.requests import Request

from src.db.audit import audit_action
from src.db.repo_registry import PROFILE_MISSING
from src.web_ui._json import _json_safe
from src.web_ui.auth import (
    read_access_allowed,
    require_admin,
    resolve_read_scope,
)

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _is_pid_alive(pid: int) -> bool:
    """Return True if process pid is still running."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists, different UID — assume alive


# Owner-facing error categories (#237 follow-up). A job owner (tenant) legitimately
# needs to know *why* their own index failed, but the raw error_msg is
# ``str(e)[:1000]`` from the pipeline (or ``str(e)[:500]`` from the cloner) and
# can carry server file paths, repo URLs, or stack traces. Each raw error maps to
# a FIXED, non-internal category string; the raw text is NEVER echoed.
#
# Patterns are whole-token / anchored regexes for the phrases each category is
# meant for, taken from what the producers actually write: git/ssh stderr
# ("fatal: could not read from remote repository", "Permission denied
# (publickey)", "Host key verification failed"), subprocess.CalledProcessError
# ("Command '['git', ...]' returned non-zero exit status 128"),
# subprocess.TimeoutExpired ("... timed out after N seconds"), psycopg2 / neo4j
# driver connection errors, OSError ENOSPC ("No space left on device"), the OOM
# killer (SIGKILL: "exit status -9", "Signals.SIGKILL", "Killed") and parser
# failures (SyntaxError "invalid syntax", manifest files). A bare substring match
# misclassified e.g. "boom" as out-of-memory and "last." as a parse error; an
# unrecognised error falls to the generic default, so the mapping stays
# exhaustive-by-construction and no raw detail can ever reach a non-admin owner.
_JOB_ERROR_CATEGORIES = [
    ((r"\bpermission denied\b", r"\bauthentication failed\b", r"\bauth(entication)? fail",
      r"\bpublickey\b", r"\bdeploy[ _-]key\b", r"\bcould not read from remote repository\b",
      r"\bhost key verification failed\b", r"\bhost key\b", r"\bssh\b"),
     "Repository access failed (authentication / deploy-key). "
     "Verify the repo URL and deploy key."),
    ((r"\[\s*'git'\s*,", r"\bgit (clone|fetch|pull|reset|checkout|rev-parse|ls-files|log)\b",
      r"^fatal:", r"\bfatal: ", r"\bnot a git repository\b", r"\bdubious ownership\b",
      r"\b(clone|fetch) failed\b", r"\bfailed to (clone|fetch)\b", r"\breset --hard\b",
      r"\bremote repository\b", r"\bunknown revision\b", r"\bbad revision\b"),
     "Git operation failed while cloning or updating the repository."),
    ((r"\btimed out\b", r"\btimeout\b", r"\btimeouterror\b", r"\btimeoutexpired\b"),
     "Indexing timed out — try again or narrow the scope."),
    ((r"\bneo4j\b", r"\bbolt://", r"\bpostgres(ql)?\b", r"\bpsycopg2?\b",
      r"\bconnection refused\b", r"\bcould not connect to server\b",
      r"\bcouldn't connect to\b", r"\bconnection pool\b", r"\bpoolerror\b",
      r"\bserviceunavailable\b", r"\bserver closed the connection\b", r"\bdatabase\b"),
     "The indexing backend was temporarily unavailable — please retry shortly."),
    ((r"\bno space left on device\b", r"\bdisk (is )?full\b", r"\bmemoryerror\b",
      r"\bout of memory\b", r"\boom\b", r"\boom-?kill(er|ed)?\b", r"\bsigkill\b",
      r"\bexit status -9\b", r"\bkilled\b"),
     "The server hit a resource limit during indexing. Contact support if it persists."),
    ((r"\bsyntaxerror\b", r"\binvalid syntax\b", r"\bparse ?error\b",
      r"\bfailed to parse\b", r"\bunparseable\b", r"__(manifest|openerp)__\.py\b",
      r"\bmanifest\b", r"\binvalid python\b"),
     "A module failed to parse during indexing."),
]
_JOB_ERROR_PATTERNS = [
    (re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE | re.MULTILINE), summary)
    for patterns, summary in _JOB_ERROR_CATEGORIES
]
_JOB_ERROR_DEFAULT = (
    "Indexing failed due to an internal error. Contact support if it persists."
)


def sanitize_job_error(error_msg):
    """Map a raw indexer error to a fixed owner-facing category (no internal leak).

    Returns ``None`` for an empty error. For a non-empty error, returns the
    fixed category summary of the first category whose whole-token pattern
    matches, NEVER the raw text - so server paths, repo URLs, and stack traces
    in ``error_msg`` cannot leak to a non-admin job owner (#237). Unrecognised
    errors return the generic default.
    """
    if not error_msg:
        return None
    text = str(error_msg)
    for pattern, summary in _JOB_ERROR_PATTERNS:
        if pattern.search(text):
            return summary
    return _JOB_ERROR_DEFAULT


@router.get("/{job_id}/status")
async def job_status(request: Request, job_id: int):
    """Return JSON status of a single indexer job.

    Security (IDOR fix #237):
    - Resolve job → profile_name → profiles.tenant_id.
    - Apply resolve_tenant_scope_web + is_in_scope: out-of-scope → 404 (no oracle).
    - Jobs with profile_name="all" (bulk admin jobs) or profiles missing from DB
      are admin-only; non-admin always gets 404 for those.
    - Non-admin callers never receive error_msg (may contain raw exceptions/paths).
    """
    try:
        from src.db.pg import job_store, repo_store

        job = job_store().get_job(job_id)
    except Exception as e:
        return JSONResponse(_json_safe({"error": str(e)}), status_code=503)

    # Unified 404 for not-found (same code path as out-of-scope — no oracle).
    if job is None:
        return JSONResponse(_json_safe({"error": "not found"}), status_code=404)

    # Single resolution: is_admin is derived from the same scope (no double DB read).
    is_admin, scope = resolve_read_scope(request)
    profile_name = job.get("profile_name") or ""

    # Bulk "all" jobs are admin-only; deny non-admin with 404.
    if not is_admin and profile_name == "all":
        return JSONResponse(_json_safe({"error": "not found"}), status_code=404)

    if not is_admin:
        # Resolve profile → tenant_id; 404 for orphan profiles (no DB row).
        try:
            profile_tenant_id = repo_store().get_profile_tenant_id(profile_name)
        except Exception as e:
            return JSONResponse(_json_safe({"error": str(e)}), status_code=503)

        if profile_tenant_id is PROFILE_MISSING:
            # Profile row doesn't exist — deny with 404 (no existence oracle).
            return JSONResponse(_json_safe({"error": "not found"}), status_code=404)

        # profile_tenant_id is None → shared/global (read_access_allowed → True for all).
        # profile_tenant_id is int → must be in caller's scope.
        if not read_access_allowed(is_admin, scope, profile_tenant_id):
            return JSONResponse(_json_safe({"error": "not found"}), status_code=404)

    pid = job.get("pid")
    is_alive: bool | None = None
    if pid is not None and job.get("status") == "running":
        is_alive = _is_pid_alive(pid)

    # Three-way error disclosure (#237 + follow-up):
    #   - admin           → full raw error_msg (operators need the detail)
    #   - non-admin owner → a sanitized category summary (in-scope by the time we
    #     reach here; out-of-scope callers already returned 404 above) so the
    #     owner learns *why* their own index failed without leaking server paths /
    #     repo URLs / stack traces. pid stays admin-only.
    return JSONResponse(_json_safe({
        "id": job["id"],
        "profile_name": job["profile_name"],
        "status": job["status"],
        "pid": job["pid"] if is_admin else None,
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "error_msg": job["error_msg"] if is_admin else sanitize_job_error(job["error_msg"]),
        "created_at": job["created_at"],
        "is_alive": is_alive,
    }))


@router.post("/{job_id}/reset")
@audit_action("jobs.reset", target_param="job_id")
async def reset_stuck_job(request: Request, job_id: int, _user_id: int = Depends(require_admin)):
    """Force-mark a stuck running job as error when its PID is dead."""
    try:
        from src.db.pg import job_store

        job = job_store().get_job(job_id)
        if job is None:
            return JSONResponse(_json_safe({"error": f"Job {job_id} not found."}), status_code=404)
        elif job["status"] != "running":
            error_msg = (
                f"Job {job_id} is not in 'running' state (current: {job['status']})."
            )
            return JSONResponse(
                _json_safe({"error": error_msg}),
                status_code=409,
            )
        else:
            pid = job.get("pid")
            if pid is not None and _is_pid_alive(pid):
                error_msg = f"Job {job_id} process (PID {pid}) is still alive — cannot reset."
                return JSONResponse(
                    _json_safe({"error": error_msg}),
                    status_code=409,
                )
            else:
                job_store().update_job(
                    job_id,
                    status="error",
                    finished_at=_dt.datetime.now(_dt.UTC),
                    error_msg="Reset by admin (process not found)",
                )
                msg = f"Job {job_id} has been reset to error state."
                return JSONResponse(
                    _json_safe({"ok": True, "message": msg})
                )
    except Exception as e:
        _logger.warning("Reset job %d failed: %s", job_id, e)
        return JSONResponse(_json_safe({"error": f"Reset failed: {e}"}), status_code=500)
