# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/routes/versions.py
"""GET /api/versions - the Odoo versions registered on this server (data-driven).

Source of truth: the ``profiles`` registry (``profiles.odoo_version``). Every
indexed node comes from a profile's repo, so the registered profile versions
ARE the server's supported series - a new series (e.g. 20.0) appears as soon
as its first profile exists, with no code or curated-data change.

This route used to read the keys of
``src/indexer/spec_data/bootstrap_versions.json``. That file is curated
Bootstrap-CSS reference data (ADR-0055 obligations: every entry needs verified
facts), so it can lag a new Odoo series by design; using it as the version list
kept 20.0 out of the admin dropdowns and pre-filled new repos with 19.0. The
public /bootstrap page still reads that file for what it is: the Bootstrap
table.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from starlette.requests import Request

from src.db.repo_registry import ODOO_VERSION_RE
from src.web_ui._json import _json_safe
from src.web_ui.auth import is_in_scope, require_authenticated, resolve_read_scope

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


def _version_key(version: str) -> tuple[int, int]:
    """Numeric (major, minor) sort key: 9.0 < 10.0 < 20.0 (never lexicographic)."""
    major, minor = version.split(".", 1)
    return int(major), int(minor)


def registered_versions(profiles: list[dict], scope) -> list[str]:
    """Distinct well-formed ``odoo_version`` of the profiles visible in *scope*, ascending.

    Pure (no I/O). A tenant member sees only the versions of profiles it may
    read (own tenant + shared), like ``GET /api/repos/profiles``; a malformed
    legacy value is skipped rather than breaking the sort.
    """
    versions = {
        p["odoo_version"] for p in profiles
        if is_in_scope(scope, p.get("tenant_id"))
        and isinstance(p.get("odoo_version"), str)
        and ODOO_VERSION_RE.match(p["odoo_version"])
    }
    return sorted(versions, key=_version_key)


@router.get("/versions")
async def list_versions(request: Request, _user_id: int = Depends(require_authenticated)):
    """Return the Odoo versions of the registered profiles, sorted numerically ascending.

    Response: ``{"versions": ["8.0", "9.0", ..., "20.0"]}``

    Returns 500 if the profile registry cannot be read - never an empty list
    silently. An empty list means no profile is registered yet (fresh install):
    the admin profile form then accepts a typed version.
    """
    _is_admin, scope = resolve_read_scope(request)
    try:
        from src.db.pg import repo_store

        profiles = repo_store().list_profiles()
    except Exception as exc:
        _logger.error("Cannot read the profile registry for /api/versions: %s", exc)
        raise HTTPException(
            status_code=500, detail="Cannot read the profile registry",
        ) from exc
    return JSONResponse(_json_safe({"versions": registered_versions(profiles, scope)}))
