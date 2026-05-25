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


def _bool_flag(env_var: str, default: bool) -> bool:
    """Read a boolean env var. Returns ``default`` if the var is unset or empty."""
    val = os.getenv(env_var, "").strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# SIGNUP_ENABLED — controls whether public self-registration is allowed.
#
# Default: False (invite-only model, ADR-0034 / Wave 0 security hardening).
# To enable public signup (e.g. during a limited open-beta):
#   export SIGNUP_ENABLED=1
# ---------------------------------------------------------------------------
SIGNUP_ENABLED: bool = _bool_flag("SIGNUP_ENABLED", default=False)
