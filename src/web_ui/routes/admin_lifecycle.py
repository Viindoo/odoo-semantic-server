# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/routes/admin_lifecycle.py
"""Admin module lifecycle page API (#380 item 4, ADR-0056 / ADR-0057).

Visibility only. The index run decides every retirement and every prune from
git evidence (ADR-0057: nobody approves anything); these routes show what it
decided and what the next run will do. There is no approve / retire action.

Routes
------
GET  /api/admin/lifecycle                  per-repo ledger view + recent retirements
                                           per version (Postgres only, cheap)
GET  /api/admin/lifecycle/audit/{version}  state + last report of the version's
                                           dry-run audit (never starts one)
POST /api/admin/lifecycle/audit/{version}  start that audit (``?force=true``
                                           ignores a fresh report)

The per-version findings (parser gaps, unproven drops, kept orphans, the
shared-module bootstrap backlog, ...) are the report of ``lifecycle-audit``
itself, run in a child process and cached (see
``src.web_ui.helpers.lifecycle_audit_runner`` for the bounds); nothing here
re-computes them.

Auth: global admins only (``webui_users.is_admin``), read from the database by
``is_admin_session`` on every request (ADR-0011 / ADR-0026; never the session
cookie). The data spans every tenant (repo paths, URLs, commit subjects), so a
non-admin gets 403 and nothing else: there is no tenant-scoped variant
(ADR-0034 fail-closed).
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from starlette.requests import Request

from src.constants import (
    WEBUI_LIFECYCLE_AUDIT_TIMEOUT_SECONDS,
    WEBUI_LIFECYCLE_AUDIT_TTL_SECONDS,
)
from src.db.audit import audit_action
from src.web_ui import auth as _auth
from src.web_ui._json import _json_safe
from src.web_ui.helpers.lifecycle_audit_runner import (
    START_BUSY,
    START_FRESH,
    LifecycleAuditCache,
    run_audit_process,
)

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/lifecycle", tags=["admin"])

# Recent retirements listed per Odoo version (the ledger keeps every one; the
# page shows the latest and the total).
RECENT_RETIREMENTS_PER_VERSION = 20

_VERSION_RE = re.compile(r"^\d{1,2}\.\d+\Z")

# One process-wide cache: at most one audit runs at a time (module docstring).
_CACHE = LifecycleAuditCache(
    run_audit_process,
    ttl=WEBUI_LIFECYCLE_AUDIT_TTL_SECONDS,
    timeout=WEBUI_LIFECYCLE_AUDIT_TIMEOUT_SECONDS,
)


async def require_global_admin(request: Request) -> int:
    """401 without a session, 403 unless ``is_admin_session`` (DB-sourced) says admin."""
    user_id = _auth.current_user_id(request)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not _auth.is_admin_session(request):
        raise HTTPException(status_code=403, detail="Admin privilege required")
    return user_id


def _version_sort_key(v: str) -> tuple:
    try:
        return (0, *(-int(p) for p in v.split(".")))
    except ValueError:
        return (1, v)


def _checked_version(version: str) -> str:
    if not _VERSION_RE.match(version):
        raise HTTPException(status_code=422, detail="version must look like 17.0")
    return version


def _repo_view(repo: dict) -> dict:
    local_path = repo.get("local_path") or ""
    on_disk = bool(local_path) and Path(local_path).is_dir()
    head = repo.get("head_sha")
    presence = repo.get("presence_head_sha")
    return {
        "id": repo["id"],
        "profile": repo.get("profile_name"),
        "odoo_version": repo.get("odoo_version"),
        "url": repo.get("url"),
        "branch": repo.get("branch"),
        "basename": Path(local_path).name if local_path else None,
        "local_path": local_path or None,
        "status": repo.get("status"),
        "error_msg": repo.get("error_msg"),
        "last_indexed_at": repo.get("last_indexed_at"),
        "clone_status": repo.get("clone_status") or "manual",
        "clone_error_msg": repo.get("clone_error_msg"),
        "checkout_on_disk": on_disk,
        "head_sha": head,
        "presence_head_sha": presence,
        # The ledger reflects the last indexed HEAD (ADR-0056); a lag means the
        # last run could not finish the repo's lifecycle (see the attention).
        "synced": head is not None and head == presence,
        "lifecycle_attention": repo.get("lifecycle_attention"),
        "lifecycle_attention_at": repo.get("lifecycle_attention_at"),
        "lifecycle_counts": repo.get("lifecycle_counts"),
    }


def _audit_status(version: str) -> dict:
    snap = _CACHE.snapshot(version)
    snap.pop("report")
    return snap


def build_overview() -> dict:
    """Every repo with its ledger view, and per version the latest retirements."""
    from src.db.module_presence import ModulePresenceStore
    from src.db.pg import get_pool, repo_store
    from src.web_ui.routes import repos as repos_module

    repos = repos_module._attach_lifecycle([dict(r) for r in repo_store().list_repos()])
    views = [_repo_view(r) for r in repos]

    retirements: dict[str, dict] = {}
    retirements_error = None
    try:
        retirements = ModulePresenceStore(get_pool()).recent_retirements(
            per_version=RECENT_RETIREMENTS_PER_VERSION,
        )
    except Exception as exc:  # noqa: BLE001 - the repo view must still render
        _logger.warning("lifecycle page: retirements unavailable: %s", exc)
        retirements_error = "the retirement history could not be read"

    versions = {v["odoo_version"] for v in views if v["odoo_version"]} | set(retirements)
    version_rows = []
    for v in sorted(versions, key=_version_sort_key):
        block = retirements.get(v) or {"total": 0, "rows": []}
        version_rows.append({
            "odoo_version": v,
            "repo_ids": [r["id"] for r in views if r["odoo_version"] == v],
            "retired_total": block["total"],
            "recent_retirements": block["rows"],
            "audit": _audit_status(v) if _VERSION_RE.match(v) else None,
        })
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "repos": views,
        "versions": version_rows,
        "retirements_error": retirements_error,
        "audit": {
            "ttl_seconds": WEBUI_LIFECYCLE_AUDIT_TTL_SECONDS,
            "timeout_seconds": WEBUI_LIFECYCLE_AUDIT_TIMEOUT_SECONDS,
            "running_version": _CACHE.running_version,
        },
    }


@router.get("")
async def lifecycle_overview(request: Request, _admin: int = Depends(require_global_admin)):
    """Per-repo lifecycle view (ledger counts, HEAD vs ledger HEAD, attention,
    clone state, checkout on disk) and the latest retirements per version."""
    try:
        return JSONResponse(_json_safe(build_overview()))
    except Exception as exc:  # noqa: BLE001
        _logger.error("lifecycle page overview failed: %s", exc)
        return JSONResponse(_json_safe({"error": "the repository registry could not be read"}),
                            status_code=503)


@router.get("/audit/{version}")
async def lifecycle_audit_status(
    request: Request, version: str, _admin: int = Depends(require_global_admin),
):
    """State of the version's dry-run audit and its last report; starts nothing."""
    return JSONResponse(_json_safe(_CACHE.snapshot(_checked_version(version))))


@router.post("/audit/{version}")
@audit_action("lifecycle.audit_run", target_param="version")
async def lifecycle_audit_start(
    request: Request,
    version: str,
    force: bool = False,
    _admin: int = Depends(require_global_admin),
):
    """Start the version's dry-run audit (writes nothing but the admin audit-log
    row of this request; ADR-0056 D11).

    200 with the report when one younger than the TTL exists (unless
    ``force``), 202 while it runs, 409 while another version's audit runs
    (one at a time; retry when it ends).
    """
    outcome = _CACHE.start(_checked_version(version), force=force)
    body = _CACHE.snapshot(version)
    if outcome == START_BUSY:
        body["busy"] = (
            f"the audit of {_CACHE.running_version} is running; one audit runs at a time"
        )
        return JSONResponse(_json_safe(body), status_code=409)
    return JSONResponse(_json_safe(body), status_code=200 if outcome == START_FRESH else 202)
