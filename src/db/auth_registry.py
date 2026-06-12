# SPDX-License-Identifier: AGPL-3.0-or-later
"""CRUD for api_keys, ssh_key_pairs, usage_log, webui_users tables via AuthStore.

Facade module. AuthStore is composed from the domain mixins in src.db.auth.*
(api_key / ssh / user / tenant / feedback). It is re-exported here together with
``reactivate_api_key`` and the shared auth exceptions so existing
``from src.db.auth_registry import AuthStore | reactivate_api_key |
LastAdminProtectedError | ...`` imports keep working unchanged.

The plan-assignment free functions (get_plan_by_id /
set_api_key_plan_and_overrides / bulk_set_plan_for_user) are NOT re-exported
here — import them directly from their home module
``src.db.auth_plans``. They were pure re-export shims with no patch surface, so
the indirection was removed in the consolidation pass.
"""
from src.db.auth._api_key import _ApiKeyMixin
from src.db.auth._feedback import _FeedbackMixin
from src.db.auth._shared import (
    VIINDOO_EMAIL_DOMAINS,
    KeyNotFoundError,
    LastAdminProtectedError,
    UserNotFoundError,
)
from src.db.auth._ssh import _SshKeyMixin
from src.db.auth._tenant import _TenantMixin
from src.db.auth._user import _UserMixin
from src.db.pg import PgPool

__all__ = [
    "VIINDOO_EMAIL_DOMAINS",
    "AuthStore",
    "KeyNotFoundError",
    "LastAdminProtectedError",
    "UserNotFoundError",
    "reactivate_api_key",
]


class AuthStore(
    _ApiKeyMixin,
    _SshKeyMixin,
    _UserMixin,
    _TenantMixin,
    _FeedbackMixin,
):
    """Encapsulates all auth / key / SSH / feedback SQL operations.

    The concrete operations live in the domain mixins (src.db.auth.*); this class
    only wires them together and owns the shared ``self._pool`` state. Cross-domain
    calls (e.g. set_user_admin -> resolve_default_mint_tenant_id) resolve through
    the composed MRO at runtime.
    """

    def __init__(self, pool: PgPool) -> None:
        self._pool = pool


# ---------------------------------------------------------------------------
# W-4 helper: reactivate an API key (symmetric counterpart of deactivate)
# Added by W-4 of PR feat/m10b-p0-rbac-quota-ui (M10B P0-ext).
# ---------------------------------------------------------------------------


def reactivate_api_key(pg_pool: "PgPool", key_id: int) -> dict | None:
    """Set api_keys.active = TRUE for the given key_id. Returns the updated
    row as dict, or None if the key does not exist. Idempotent — calling
    on an already-active key still returns the row without raising.

    SECURITY (ADR-0034, m13_019): delegates to ``AuthStore.reactivate_api_key``,
    which re-scopes the tenant when a non-admin, user-owned key would otherwise
    come back ``active=TRUE`` with ``tenant_id IS NULL`` (the unrestricted
    sentinel). This thin wrapper is kept for backward compatibility with callers
    that hold a bare pool. Fail-closed: a resolver failure propagates.

    Returns dict with keys: id, name, key_prefix, active, user_id, tenant_id,
    created_at, last_used_at, expires_at.

    Added by W-4 of PR feat/m10b-p0-rbac-quota-ui (M10B P0-ext).
    """
    return AuthStore(pg_pool).reactivate_api_key(key_id)
