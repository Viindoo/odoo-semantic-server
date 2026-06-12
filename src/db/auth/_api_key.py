# SPDX-License-Identifier: AGPL-3.0-or-later
"""API-key domain methods for AuthStore (create / verify / list / (de)activate /
ownership / usage-log)."""
import logging
import secrets

from src.auth import hash_key, hash_key_legacy_sha256
from src.db.auth._shared import KeyNotFoundError, UserNotFoundError

logger = logging.getLogger(__name__)


class _ApiKeyMixin:
    """API key + usage-log SQL operations.

    Composed into AuthStore; relies on ``self._pool`` set by AuthStore.__init__.
    """

    # ------------------------------------------------------------------
    # API keys
    # ------------------------------------------------------------------

    def create_api_key(
        self,
        name: str,
        user_id: int | None = None,
        expires_at=None,
        tenant_id: int | None = None,
    ) -> tuple[str, str, int]:
        """Create API key with HMAC-SHA256 hash, optional user ownership and expiry.

        Args:
            name: Descriptive name for the key (e.g. 'claude-code-laptop').
            user_id: Optional webui_users.id FK.  None = global/admin key
                (CLI-created keys, backward compat).
            expires_at: Optional datetime (or None for eternal).  When set,
                the key is rejected after this timestamp.
            tenant_id: Optional tenants.id FK (ADR-0034 D1).  None = global/admin key
                that bypasses tenant isolation.  New keys created for a specific tenant
                must pass the corresponding tenant_id so the key is bound to that tenant.

        Returns:
            Tuple of (raw_key_string, key_prefix_12chars, key_id).
            raw_key is ephemeral and should be displayed to user exactly once.
        """
        raw = "osm_" + secrets.token_urlsafe(32)
        key_hash = hash_key(raw)
        key_prefix = raw[:12]  # bumped 8 → 12 chars for new keys
        with self._pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO api_keys"
                    " (name, key_hash, key_prefix, user_id, expires_at, tenant_id)"
                    " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                    (name, key_hash, key_prefix, user_id, expires_at, tenant_id),
                )
                row_id = cur.fetchone()[0]
            conn.commit()
        return raw, key_prefix, row_id

    def verify_api_key(self, raw_key: str) -> int | None:
        """Return api_key_id if active + valid (not expired). Update last_used_at.

        Lookup order:
          1. HMAC-SHA256 (primary — M9+ keys).
          2. SHA-256 plain (legacy fallback — keys created before M9 HMAC upgrade).
             Logs a warning on match; fallback expires on LEGACY_HASH_DEADLINE.

        Both paths enforce ``active = TRUE`` and ``expires_at`` filter.

        Args:
            raw_key: The full API key string (starts with 'osm_').

        Returns:
            Integer key_id if found and active/unexpired, None otherwise.
            Side effect: updates last_used_at timestamp on successful verification.
        """
        hmac_hash = hash_key(raw_key)
        expires_filter = "AND (expires_at IS NULL OR expires_at > NOW())"
        base_query = (
            "SELECT id FROM api_keys "
            f"WHERE key_hash = %s AND active = TRUE {expires_filter}"
        )

        with self._pool.checkout() as conn:
            row = self._pool.fetch_one(conn, base_query, (hmac_hash,))

        if row is None:
            # Backward-compat: try plain SHA-256 (until LEGACY_HASH_DEADLINE)
            sha_hash = hash_key_legacy_sha256(raw_key)
            with self._pool.checkout() as conn:
                row = self._pool.fetch_one(conn, base_query, (sha_hash,))
            if row is not None:
                logger.warning(
                    "API key id=%s matched via legacy SHA-256 hash. "
                    "Please rotate this key before %s.",
                    row["id"],
                    "2026-06-15",  # LEGACY_HASH_DEADLINE
                )

        if row is None:
            return None

        key_id = row["id"]
        with self._pool.checkout() as conn:
            self._pool.execute(
                conn,
                "UPDATE api_keys SET last_used_at = NOW() WHERE id = %s",
                (key_id,),
            )
        return key_id

    def verify_api_key_tenant(self, raw_key: str) -> tuple[int, int | None] | None:
        """Return (api_key_id, tenant_id) if active + valid (not expired). Update last_used_at.

        Extended variant of verify_api_key that additionally returns the tenant_id bound
        to the key (ADR-0034 D4.1 — P1 plumbing).  A NULL tenant_id means the key is a
        global/admin key that bypasses tenant isolation.

        Lookup order and active/expiry enforcement are identical to verify_api_key().
        last_used_at is updated on success (same side effect).

        Args:
            raw_key: The full API key string (starts with 'osm_').

        Returns:
            (key_id, tenant_id) if found and active/unexpired; None otherwise.
            tenant_id is None for legacy/global keys (tenant_id IS NULL in DB).
        """
        hmac_hash = hash_key(raw_key)
        expires_filter = "AND (expires_at IS NULL OR expires_at > NOW())"
        base_query = (
            "SELECT id, tenant_id FROM api_keys "
            f"WHERE key_hash = %s AND active = TRUE {expires_filter}"
        )

        with self._pool.checkout() as conn:
            row = self._pool.fetch_one(conn, base_query, (hmac_hash,))

        if row is None:
            # Backward-compat: try plain SHA-256 (until LEGACY_HASH_DEADLINE)
            sha_hash = hash_key_legacy_sha256(raw_key)
            with self._pool.checkout() as conn:
                row = self._pool.fetch_one(conn, base_query, (sha_hash,))
            if row is not None:
                logger.warning(
                    "API key id=%s matched via legacy SHA-256 hash. "
                    "Please rotate this key before %s.",
                    row["id"],
                    "2026-06-15",  # LEGACY_HASH_DEADLINE
                )

        if row is None:
            return None

        key_id = row["id"]
        tenant_id: int | None = row["tenant_id"]
        with self._pool.checkout() as conn:
            self._pool.execute(
                conn,
                "UPDATE api_keys SET last_used_at = NOW() WHERE id = %s",
                (key_id,),
            )
        return key_id, tenant_id

    def verify_api_key_full(
        self, raw_key: str
    ) -> tuple[int, int | None, int | None, bool] | None:
        """Return (key_id, tenant_id, user_id, owner_is_admin) if active + valid.

        Read-side authorization variant (defense-in-depth, ADR-0034 follow-up).
        Superset of verify_api_key_tenant: in addition to (key_id, tenant_id) it
        returns the owning user_id and the owner's is_admin flag, resolved via a
        single LEFT JOIN to webui_users so the call stays one DB round-trip.

        The owner metadata lets the MCP auth choke-point enforce the read-side
        invariant: a user-owned (user_id IS NOT NULL), non-admin
        (is_admin = false) key MUST NOT carry tenant_id IS NULL — that is the
        "unrestricted" sentinel reserved for system/CLI keys (user_id IS NULL)
        and admin-owned keys. See AuthMiddleware._is_null_tenant_escalation.

        Lookup order, active/expiry enforcement, and the last_used_at side effect
        are identical to verify_api_key() / verify_api_key_tenant().

        Fail-closed note: when the key has an owner row, owner_is_admin reflects
        webui_users.is_admin coerced to a strict bool (NULL → False) so an absent
        flag is treated as non-admin, never as admin.

        Args:
            raw_key: The full API key string (starts with 'osm_').

        Returns:
            (key_id, tenant_id, user_id, owner_is_admin) if found and
            active/unexpired; None otherwise.
              - tenant_id is None for unscoped/global keys.
              - user_id is None for system/CLI keys (no webui_users owner).
              - owner_is_admin is False when user_id is None (no owner row).
        """
        hmac_hash = hash_key(raw_key)
        expires_filter = "AND (k.expires_at IS NULL OR k.expires_at > NOW())"
        base_query = (
            "SELECT k.id, k.tenant_id, k.user_id, "
            "COALESCE(u.is_admin, FALSE) AS owner_is_admin "
            "FROM api_keys k "
            "LEFT JOIN webui_users u ON u.id = k.user_id "
            f"WHERE k.key_hash = %s AND k.active = TRUE {expires_filter}"
        )

        with self._pool.checkout() as conn:
            row = self._pool.fetch_one(conn, base_query, (hmac_hash,))

        if row is None:
            # Backward-compat: try plain SHA-256 (until LEGACY_HASH_DEADLINE)
            sha_hash = hash_key_legacy_sha256(raw_key)
            with self._pool.checkout() as conn:
                row = self._pool.fetch_one(conn, base_query, (sha_hash,))
            if row is not None:
                logger.warning(
                    "API key id=%s matched via legacy SHA-256 hash. "
                    "Please rotate this key before %s.",
                    row["id"],
                    "2026-06-15",  # LEGACY_HASH_DEADLINE
                )

        if row is None:
            return None

        key_id = row["id"]
        tenant_id: int | None = row["tenant_id"]
        user_id: int | None = row["user_id"]
        owner_is_admin = bool(row["owner_is_admin"])
        with self._pool.checkout() as conn:
            self._pool.execute(
                conn,
                "UPDATE api_keys SET last_used_at = NOW() WHERE id = %s",
                (key_id,),
            )
        return key_id, tenant_id, user_id, owner_is_admin

    def list_api_keys(self, user_id: int | None = None, admin: bool = False) -> list[dict]:
        """List API keys (without key_hash for security).

        Filtering rules:
          - ``admin=True`` (or ``user_id=None and admin=True``): returns all keys.
          - ``user_id`` set and ``admin=False``: returns only keys for that user
            (``user_id = %s``).  Admin keys (``user_id IS NULL``) are excluded.
          - Default (no args): returns all keys (backward compat for CLI/admin paths
            that don't carry a session).

        Args:
            user_id: Filter to this user's keys.  None = no user filter.
            admin: If True, override user_id filter and return all keys.

        Returns:
            List of dicts with keys: id, name, key_prefix, active, created_at,
            last_used_at, user_id, expires_at, owner_username (None for system keys),
            plan_id, rate_limit_override, quota_override.  The last three power the
            admin UI: the plan dropdown prefill and the per-key override modal.  Each
            is NULL when unset; NULL means "no override" and is distinct from 0, which
            means a zero-valued override (ADR-0041).
        """
        select = (
            "SELECT k.id, k.name, k.key_prefix, k.active, k.created_at, k.last_used_at, "
            "k.user_id, k.expires_at, u.username AS owner_username, "
            "k.plan_id, k.rate_limit_override, k.quota_override "
            "FROM api_keys k "
            "LEFT JOIN webui_users u ON u.id = k.user_id"
        )
        if admin or user_id is None:
            query = select + " ORDER BY k.id"
            params: tuple = ()
        else:
            query = select + " WHERE k.user_id = %s ORDER BY k.id"
            params = (user_id,)

        with self._pool.checkout() as conn:
            return self._pool.fetch_all(conn, query, params)

    def deactivate_api_key(self, key_id: int) -> None:
        """Deactivate an API key by id.

        Args:
            key_id: The api_key id to deactivate.
        """
        with self._pool.checkout() as conn:
            self._pool.execute(
                conn,
                "UPDATE api_keys SET active = FALSE WHERE id = %s",
                (key_id,),
            )

    def deactivate_api_key_for_user(self, key_id: int, user_id: int) -> int:
        """Deactivate an API key only if it belongs to the given user.

        Ownership-safe variant: adds ``AND user_id = %s`` so a non-admin user
        cannot deactivate keys they do not own.

        Args:
            key_id: The api_key id to deactivate.
            user_id: The webui_users.id that must own the key.

        Returns:
            Number of rows updated (1 if found + owned, 0 if not found or not owned).
        """
        with self._pool.checkout() as conn:
            return self._pool.execute(
                conn,
                "UPDATE api_keys SET active = FALSE WHERE id = %s AND user_id = %s",
                (key_id, user_id),
            )

    # ------------------------------------------------------------------
    # Usage log
    # ------------------------------------------------------------------

    def log_usage(self, api_key_id: int | None, tool_name: str, response_ms: int) -> None:
        """Log tool usage. Fire-and-forget — swallows exceptions silently.

        Args:
            api_key_id: Integer key_id or None for anonymous requests.
            tool_name: Name of the MCP tool invoked (e.g. 'resolve_model').
            response_ms: Response time in milliseconds.
        """
        try:
            with self._pool.checkout() as conn:
                self._pool.execute(
                    conn,
                    "INSERT INTO usage_log (api_key_id, tool_name, response_ms)"
                    " VALUES (%s, %s, %s)",
                    (api_key_id, tool_name, response_ms),
                )
        except Exception:
            pass  # best-effort

    def assign_key_owner(self, key_id: int, new_user_id: int | None) -> None:
        """Reassign ownership of an API key to a different user (or clear it).

        Args:
            key_id: The api_keys.id to reassign.
            new_user_id: The webui_users.id to assign, or None to clear ownership
                (system/global key).

        SECURITY (ADR-0034, m13_019): reassigning a key to a non-admin user must
        preserve the isolation invariant — a non-admin, user-owned key may never
        be ``active=TRUE`` with ``tenant_id IS NULL`` (the unrestricted sentinel).
        If the key is active + unrestricted and the new owner is a non-admin user,
        the tenant is re-scoped (public / viindoo) in the SAME UPDATE. Clearing
        ownership (new_user_id=None → system/CLI key) leaves tenant_id untouched.

        Fail-closed: a tenant-resolver failure raises (no UPDATE is performed)
        rather than leaving the key active + unrestricted under a non-admin owner.

        Raises:
            UserNotFoundError: new_user_id is not None and the user does not exist.
            KeyNotFoundError: key_id does not exist in api_keys.
        """
        with self._pool.checkout() as conn:
            if new_user_id is not None:
                row = self._pool.fetch_one(
                    conn,
                    "SELECT 1 FROM webui_users WHERE id = %s",
                    (new_user_id,),
                )
                if row is None:
                    raise UserNotFoundError(f"User id={new_user_id} not found")

            # Re-scope the tenant when the reassignment would otherwise leave the
            # key active + unrestricted under a non-admin owner.
            set_parts = ["user_id = %s"]
            params: list = [new_user_id]
            key_row = self._pool.fetch_one(
                conn,
                "SELECT active, tenant_id FROM api_keys WHERE id = %s",
                (key_id,),
            )
            if key_row is None:
                raise KeyNotFoundError(f"API key id={key_id} not found")
            if key_row["active"] and key_row["tenant_id"] is None:
                scoped = self._scope_tenant_for_reactivation(new_user_id)
                if scoped is not None:
                    set_parts.append("tenant_id = %s")
                    params.append(scoped)
            params.append(key_id)

            rowcount = self._pool.execute(
                conn,
                f"UPDATE api_keys SET {', '.join(set_parts)} WHERE id = %s",  # noqa: S608 — set_parts is static SQL
                tuple(params),
            )
            if rowcount == 0:
                raise KeyNotFoundError(f"API key id={key_id} not found")

    def count_api_keys_per_user(self) -> dict[int | None, int]:
        """Return count of active API keys grouped by user_id.

        Returns:
            Dict mapping user_id (int or None for system/global keys) to
            the number of active keys owned by that user.
        """
        with self._pool.checkout() as conn:
            rows = self._pool.fetch_all(
                conn,
                "SELECT user_id, count(*) AS cnt FROM api_keys "
                "WHERE active GROUP BY user_id",
            )
        return {r["user_id"]: int(r["cnt"]) for r in rows}

    def _scope_tenant_for_reactivation(self, user_id: int | None) -> int | None:
        """Resolve the tenant a key must be (re-)bound to when it is about to
        become ``active`` with ``tenant_id IS NULL``.

        SECURITY INVARIANT (ADR-0034, m13_019): a non-admin, user-owned key must
        NEVER be ``active=TRUE`` while ``tenant_id IS NULL`` — that NULL is the
        unrestricted sentinel and would re-open the free-signup data-exposure
        hole that m13_019 closed.

        Returns:
            - ``None`` when the key may legitimately keep ``tenant_id IS NULL``:
              the key is unowned (``user_id IS NULL`` = system/CLI key) or its
              owner is an admin. The caller leaves ``tenant_id`` untouched.
            - a non-NULL tenant id (via ``resolve_default_mint_tenant_id``) when
              the owner is a non-admin user; the caller MUST write it in the same
              UPDATE so the key never surfaces unrestricted.

        Fail-closed: if the resolver raises (e.g. tenants missing because m13_019
        is unapplied), the exception propagates so the caller aborts rather than
        completing the operation with an unrestricted key.
        """
        if user_id is None:
            # System/CLI key (no owner) — stays unrestricted by design.
            return None
        if bool(self.get_user_field(user_id, "is_admin")):
            # Admin-owned key — unrestricted by design.
            return None
        # Non-admin owner → must be scoped to a concrete tenant, never NULL.
        return self.resolve_default_mint_tenant_id(user_id)

    def reactivate_api_key(self, key_id: int) -> dict | None:
        """Set ``api_keys.active = TRUE`` for ``key_id``, re-scoping the tenant
        when required to preserve the m13_019 isolation invariant.

        If the key's ``tenant_id IS NULL`` and its owner is a non-admin user, the
        tenant is re-scoped (public / viindoo) in the SAME UPDATE so the key never
        comes back unrestricted. Admin-owned and system/CLI (``user_id IS NULL``)
        keys keep ``tenant_id IS NULL``.

        Idempotent — reactivating an already-active key returns the row. Returns
        ``None`` if the key does not exist.

        Fail-closed: a resolver failure raises (no UPDATE is performed) rather
        than reactivating an unrestricted key.

        Returns dict with keys: id, name, key_prefix, active, user_id, tenant_id,
        created_at, last_used_at, expires_at.
        """
        with self._pool.checkout() as conn:
            existing = self._pool.fetch_one(
                conn,
                "SELECT user_id, tenant_id FROM api_keys WHERE id = %s",
                (key_id,),
            )
            if existing is None:
                return None

            set_parts = ["active = TRUE"]
            params: list = []
            # Only re-scope when the key would otherwise come back unrestricted.
            if existing["tenant_id"] is None:
                # Resolver runs OUTSIDE-but-before the UPDATE; a raise here aborts
                # the whole operation (fail-closed) — no partial reactivation.
                scoped = self._scope_tenant_for_reactivation(existing["user_id"])
                if scoped is not None:
                    set_parts.append("tenant_id = %s")
                    params.append(scoped)
            params.append(key_id)

            row = self._pool.fetch_one(
                conn,
                f"UPDATE api_keys SET {', '.join(set_parts)} WHERE id = %s "  # noqa: S608 — set_parts is static SQL
                "RETURNING id, name, key_prefix, active, user_id, tenant_id, "
                "created_at, last_used_at, expires_at",
                tuple(params),
            )
        if row is None:
            return None
        return {
            "id": row["id"],
            "name": row["name"],
            "key_prefix": row["key_prefix"],
            "active": bool(row["active"]),
            "user_id": row["user_id"],
            "tenant_id": row["tenant_id"],
            "created_at": row["created_at"],
            "last_used_at": row["last_used_at"],
            "expires_at": row["expires_at"],
        }
