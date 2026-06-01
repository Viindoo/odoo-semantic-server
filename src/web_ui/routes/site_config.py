# SPDX-License-Identifier: AGPL-3.0-or-later
"""Public site configuration endpoint — GET /api/site-config (no auth required).

Returns a small set of public-safe settings that Astro SSR pages need at
render time without a logged-in session.  Only values safe to expose to
anonymous visitors should appear here.

Response shape (WI-1):
    {
        "helpdesk_url": "https://viindoo.com/ticket/team/88",
        "site_version": "0.13.1",
        "paid_checkout_enabled": false,
        "checkout_url_map": {"<plan_slug>": "<url>"},
        "ga_measurement_id": ""
    }

``helpdesk_url`` is read from ``support.helpdesk_url`` via the settings overlay
(admin-tunable, hot-reload ≤60s).  The fallback is the catalogue default so the
endpoint never returns an empty/null URL even when the DB is unavailable.

``site_version`` is the package version string from ``src._version``.  Astro
pages that need the version for footer display read it from here rather than
hard-coding a string.

``paid_checkout_enabled`` gates the public paid-checkout CTA on the pricing page
(``billing.paid_checkout_enabled`` setting, default False).  False = waitlist only.

``checkout_url_map`` maps plan slug → Polar checkout URL for the pricing-page CTA
(``billing.polar_checkout_url_map`` setting, default {{}}).

``ga_measurement_id`` is the GA4 measurement ID injected into public pages
(``analytics.ga_measurement_id`` setting, default "").  Empty = analytics disabled.
"""
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from src.web_ui._json import _json_safe

logger = logging.getLogger(__name__)
router = APIRouter(tags=["site-config"])


def _helpdesk_url_catalogue_default() -> str:
    """Return the catalogue default for ``support.helpdesk_url``.

    The literal URL lives exactly once in :data:`src.settings_registry.SETTINGS_CATALOGUE`.
    This helper is the fallback for when the DB is unavailable so the endpoint
    never returns null.  The catalogue default is also the value that a fresh
    install returns before any admin override is written.
    """
    from src.settings_registry import SETTINGS_CATALOGUE
    for sdef in SETTINGS_CATALOGUE:
        if sdef.key == "support.helpdesk_url":
            return str(sdef.default_value)
    return "https://viindoo.com/ticket/team/88"  # last-resort if catalogue missing entry


@router.get("/api/site-config")
async def get_site_config() -> dict:
    """Return public-safe runtime config for Astro SSR pages.

    No authentication required — only safe-to-expose values are included.
    Exposes:
    - ``helpdesk_url``: support ticket URL (admin-tunable via settings overlay,
      live on the pricing page within the 60s settings TTL; static pages use the
      build-time default and require a site rebuild to reflect a change).
    - ``site_version``: package version string from pyproject.toml metadata.
    - ``paid_checkout_enabled``: bool — gates paid-checkout CTA on pricing page
      (``billing.paid_checkout_enabled``, default False).
    - ``checkout_url_map``: dict — plan slug → Polar checkout URL
      (``billing.polar_checkout_url_map``, default {}). Returned ONLY when
      ``paid_checkout_enabled`` is true; otherwise an empty dict so unreleased
      checkout URLs are not exposed pre-launch.
    - ``ga_measurement_id``: str — GA4 measurement ID for public pages
      (``analytics.ga_measurement_id``, default ""; empty = disabled).
    """
    from src.settings import get_setting

    _default = _helpdesk_url_catalogue_default()
    try:
        helpdesk_url = str(get_setting("support.helpdesk_url") or _default)
    except Exception:
        helpdesk_url = _default

    try:
        from src._version import __version__ as site_version
    except Exception:
        site_version = "unknown"

    try:
        paid_checkout_enabled = bool(get_setting("billing.paid_checkout_enabled"))
    except Exception:
        paid_checkout_enabled = False

    # Only expose the Polar buy-links once paid checkout is actually enabled.
    # While the flag is off (pre-launch), there is no public CTA that uses them,
    # so withholding the map keeps unreleased checkout URLs off the public
    # endpoint (preserves the server-side gating the pricing page used to have).
    if paid_checkout_enabled:
        try:
            _url_map = get_setting("billing.polar_checkout_url_map")
            checkout_url_map = _url_map if isinstance(_url_map, dict) else {}
        except Exception:
            checkout_url_map = {}
    else:
        checkout_url_map = {}

    try:
        ga_measurement_id = str(get_setting("analytics.ga_measurement_id") or "")
    except Exception:
        ga_measurement_id = ""

    return JSONResponse(_json_safe({
        "helpdesk_url": helpdesk_url,
        "site_version": site_version,
        "paid_checkout_enabled": paid_checkout_enabled,
        "checkout_url_map": checkout_url_map,
        "ga_measurement_id": ga_measurement_id,
    }))
