# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/routes/versions.py
"""GET /api/versions — data-driven version list from bootstrap_versions.json (W4)."""
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from src.web_ui._json import _json_safe
from src.web_ui.auth import require_authenticated

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

# Canonical path to bootstrap_versions.json relative to this file's package root.
_BOOTSTRAP_JSON = (
    Path(__file__).parent.parent.parent / "indexer" / "spec_data" / "bootstrap_versions.json"
)


@router.get("/versions")
async def list_versions(_user_id: int = Depends(require_authenticated)):
    """Return Odoo versions supported by this server, sorted numerically ascending.

    Response: ``{"versions": ["8.0", "9.0", ..., "19.0"]}``

    Data source: ``src/indexer/spec_data/bootstrap_versions.json`` (key ``versions``).
    Returns 500 if the file cannot be read or parsed — never returns an empty list silently.
    """
    try:
        raw = _BOOTSTRAP_JSON.read_text(encoding="utf-8")
    except OSError as exc:
        _logger.error("Cannot read bootstrap_versions.json: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Cannot read version manifest: {exc}",
        ) from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _logger.error("bootstrap_versions.json is malformed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Version manifest is malformed JSON: {exc}",
        ) from exc

    versions_raw = data.get("versions")
    if not isinstance(versions_raw, dict):
        raise HTTPException(
            status_code=500,
            detail='bootstrap_versions.json missing or invalid "versions" key (expected object).',
        )

    # Sort numerically (toFloat equivalent): "10.0" > "9.0", not lexicographic.
    try:
        sorted_versions = sorted(versions_raw.keys(), key=lambda v: float(v))
    except (ValueError, TypeError) as exc:
        _logger.error("Cannot sort versions numerically: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Version sort failed (non-numeric key): {exc}",
        ) from exc

    return JSONResponse(_json_safe({"versions": sorted_versions}))
