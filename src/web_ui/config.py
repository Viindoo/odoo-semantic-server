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
# ---------------------------------------------------------------------------
SIGNUP_ENABLED: bool = _bool_flag(
    "SIGNUP_ENABLED", section="webui", key="signup_enabled", default=False
)
