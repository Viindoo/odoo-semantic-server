# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared low-level symbols for the AuthStore mixins.

Holds the auth exceptions and the Viindoo email-domain tuple in a leaf module so
both the domain mixins (src.db.auth._*) and the AuthStore facade
(src.db.auth_registry) can import them without forming a parent<->child import
cycle. `auth_registry` re-exports the three exceptions, so existing
`from src.db.auth_registry import LastAdminProtectedError` imports keep working.
"""

# Email domains whose users are scoped to the Viindoo tenant at mint time
# (ADR-0034). Matched case-insensitively against the part after '@'.
VIINDOO_EMAIL_DOMAINS = ("viindoo.com",)


class LastAdminProtectedError(Exception):
    """Raised when an operation would remove the last active admin."""


class UserNotFoundError(Exception):
    """Raised when a user_id does not exist in webui_users."""


class KeyNotFoundError(Exception):
    """Raised when a key_id does not exist in api_keys."""
