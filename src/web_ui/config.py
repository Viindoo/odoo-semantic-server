# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/config.py
"""Web UI runtime configuration flags.

Resolution order (per src/config.py convention):
  1. Environment variable (env var wins over everything)
  2. INI file via from_env_or_ini
  3. Hardcoded default

Boolean flags are read via ``_bool_flag(env_var, default)`` which treats "1",
"true", "yes" (case-insensitive) as True, everything else as False.
"""
import os

from src import config as _config


def _bool_flag(env_var: str, section: str, key: str, default: bool) -> bool:
    """Read a boolean flag with ``env var → INI → default`` precedence.

    Source resolution is delegated to :func:`src.config.from_env_or_ini` (env
    var wins, then the ``[section] key`` entry in ``odoo-semantic.conf``), then
    the string is coerced to bool: "1", "true", "yes" (case-insensitive) are
    True; any other non-empty value is False; an unset/empty source falls
    through to ``default``.
    """
    raw = _config.from_env_or_ini(env_var, section, key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# SIGNUP_ENABLED — controls whether public self-registration is allowed.
#
# Default: False (invite-only model, ADR-0034 / Wave 0 security hardening).
# To enable public signup (e.g. during a limited open-beta), use either:
#   - env var:  export SIGNUP_ENABLED=1
#   - INI:      signup_enabled = true   under [webui] in odoo-semantic.conf
# Read once at import — changing it requires a service restart.
#
# WI-9 (ADR-0042): the constant remains the import-time floor.  New callers
# should prefer :func:`signup_enabled` which adds a leading
# DB-overlay layer so an admin can flip the gate live without a deploy.
# ---------------------------------------------------------------------------
SIGNUP_ENABLED: bool = _bool_flag(
    "SIGNUP_ENABLED", section="webui", key="signup_enabled", default=False
)


def signup_enabled() -> bool:
    """Return True iff public signup is currently open.

    Resolution order (WI-RV F-A / ADR-0042):
      1. ``app_settings`` overlay row for ``signup.enabled`` — DB-sourced live
         toggle.  When an admin has explicitly written a row (system scope,
         tenant_id IS NULL) via PATCH /api/admin/settings/signup.enabled, that
         row **wins** over both the env var and the INI file because runtime
         overlay is the source of truth for tunables intended to flip without
         a deploy.
      2. :data:`SIGNUP_ENABLED` — the import-time constant which itself folds
         env var > INI > hardcoded default (False).  This is what tests
         monkeypatch when they need to flip the gate at the source — it is
         intentionally read from the :mod:`src.web_ui.config` module (NOT the
         caller's module) so a single override point switches both
         ``signup.py`` and ``oauth.py`` simultaneously.

    The DB lookup uses :func:`get_overlay_only` (NOT ``get_setting``) so a
    missing row falls through to the constant instead of being silently
    overridden by the catalogue default (False).  This preserves the
    monkeypatch-the-constant test contract used by ``test_signup.py``,
    ``test_oauth.py``, and ``test_wave0_admin_gate.py`` while still honouring
    a live DB overlay when admins set one.

    The overlay lookup is wrapped in a broad ``except`` so a transient DB
    outage cannot make signup mysteriously open (or closed); on failure we
    fall back to the import-time constant which encodes the operator's last
    chosen posture from the deploy environment.
    """
    try:
        from src.settings import get_overlay_only
        overlay = get_overlay_only("signup.enabled")
        if overlay is not None:
            return bool(overlay)
    except Exception:
        pass
    # Resolve the module-level constant by attribute lookup so monkeypatch
    # of src.web_ui.config.SIGNUP_ENABLED takes effect inside this function
    # (importing at module top would close over the boot-time value only).
    import sys
    _mod = sys.modules[__name__]
    return bool(getattr(_mod, "SIGNUP_ENABLED", False))


# ---------------------------------------------------------------------------
# POLAR_WEBHOOK_SECRET — Standard-Webhooks HMAC signing secret from Polar.sh.
#
# Read once at import.  The webhook route (POST /api/webhooks/polar) reads this
# module attribute at request time and fails-closed with HTTP 503 when it is
# None — unknown/unsigned webhooks are NEVER processed.
#
# In production set via systemd EnvironmentFile= (webui.env, mode 600) or
# LoadCredential=.  In local dev leave unset; the route will reject calls
# unless you export a matching test secret.
#
# Value: the raw secret string as provided by Polar ("whsec_..." base64 form
# or a plain hex token).  The verify_signature() helper in src/billing/polar.py
# handles both encodings.
# ---------------------------------------------------------------------------
POLAR_WEBHOOK_SECRET: str | None = os.environ.get("POLAR_WEBHOOK_SECRET")


# ---------------------------------------------------------------------------
# POLAR_API_KEY — outbound Polar REST API token (Organization Access Token).
#
# Read once at import.  The outbound cancel client (src/billing/polar_api.py)
# reads this module attribute at call time and fails-closed by raising
# ``PolarApiNotConfigured`` when it is None — the in-app cancel endpoint maps
# that to HTTP 503 and points the user at the Polar customer portal instead.
# An in-app "cancel" is NEVER reported as successful unless Polar confirmed it,
# so a missing key can never tell a paying user they were cancelled while Polar
# keeps charging them.
#
# In production set via systemd EnvironmentFile= (webui.env, mode 600) or
# LoadCredential=.  In local dev leave unset; the cancel endpoint then returns
# 503 with the portal link until a real token is provided.
#
# Value: the raw Polar token string ("polar_oat_..." form) used as a Bearer
# credential against ``billing.polar_api_base``.
# ---------------------------------------------------------------------------
POLAR_API_KEY: str | None = os.environ.get("POLAR_API_KEY")
