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

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from starlette.requests import Request

from src.web_ui._json import _json_safe

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


@router.get("/{job_id}/status")
async def job_status(request: Request, job_id: int):
    """Return JSON status of a single indexer job."""
    try:
        from src.db.pg import job_store

        job = job_store().get_job(job_id)
    except Exception as e:
        return JSONResponse(_json_safe({"error": str(e)}), status_code=503)
    if job is None:
        return JSONResponse(_json_safe({"error": "job not found"}), status_code=404)

    pid = job.get("pid")
    is_alive: bool | None = None
    if pid is not None and job.get("status") == "running":
        is_alive = _is_pid_alive(pid)

    return JSONResponse(_json_safe({
        "id": job["id"],
        "profile_name": job["profile_name"],
        "status": job["status"],
        "pid": job["pid"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "error_msg": job["error_msg"],
        "created_at": job["created_at"],
        "is_alive": is_alive,
    }))


@router.post("/{job_id}/reset")
async def reset_stuck_job(request: Request, job_id: int):
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
