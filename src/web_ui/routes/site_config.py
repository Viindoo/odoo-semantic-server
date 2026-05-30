# SPDX-License-Identifier: AGPL-3.0-or-later
"""Public site configuration endpoint — GET /api/site-config (no auth required).

Returns a small set of public-safe settings that Astro SSR pages need at
render time without a logged-in session.  Only values safe to expose to
anonymous visitors should appear here.

Response shape (WI-1):
    {
        "helpdesk_url": "https://viindoo.com/ticket/team/88",
        "site_version": "0.13.1"
    }

``helpdesk_url`` is read from ``support.helpdesk_url`` via the settings overlay
(admin-tunable, hot-reload ≤60s).  The fallback is the catalogue default so the
endpoint never returns an empty/null URL even when the DB is unavailable.

``site_version`` is the package version string from ``src._version``.  Astro
pages that need the version for footer display read it from here rather than
hard-coding a string.
"""
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from src.web_ui._json import _json_safe

logger = logging.getLogger(__name__)
router = APIRouter(tags=["site-config"])

_DEFAULT_HELPDESK_URL = "https://viindoo.com/ticket/team/88"


@router.get("/api/site-config")
async def get_site_config() -> dict:
    """Return public-safe runtime config for Astro SSR pages.

    No authentication required — only safe-to-expose values are included.
    Currently exposes:
    - ``helpdesk_url``: support ticket URL (admin-tunable via settings overlay).
    - ``site_version``: package version string from pyproject.toml metadata.
    """
    from src.settings import get_setting

    try:
        helpdesk_url = str(get_setting("support.helpdesk_url") or _DEFAULT_HELPDESK_URL)
    except Exception:
        helpdesk_url = _DEFAULT_HELPDESK_URL

    try:
        from src._version import __version__ as site_version
    except Exception:
        site_version = "unknown"

    return JSONResponse(_json_safe({
        "helpdesk_url": helpdesk_url,
        "site_version": site_version,
    }))
