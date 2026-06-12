# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web-UI user domain methods for AuthStore (user/password/admin/active flags +
password-reset tokens + session revocation)."""
import datetime as _datetime_mod
import hashlib
import secrets
from datetime import timedelta

from src.db.auth._shared import LastAdminProtectedError, UserNotFoundError


class _UserMixin:
    """Web-UI user + password-reset SQL operations.

    Composed into AuthStore; relies on ``self._pool`` set by AuthStore.__init__.
    """

    # ------------------------------------------------------------------
    # Web UI users
    # ------------------------------------------------------------------

    def get_user_password_hash(self, username: str) -> str | None:
        """Return password_hash for username, or None if not found.

        Args:
            username: The web UI username to look up.
        """
        with self._pool.checkout() as conn:
            row = self._pool.fetch_one(
                conn,
                "SELECT password_hash FROM webui_users WHERE username = %s",
                (username,),
            )
        return row["password_hash"] if row is not None else None

    def set_user_password(self, username: str, password_hash: str, is_admin: bool = False) -> None:
        """Insert or update password_hash for username.

        Args:
            username: The web UI username.
            password_hash: bcrypt hash of the new password.
            is_admin: If True, set is_admin=TRUE on creation (ignored on update).
        """
        with self._pool.checkout() as conn:
            exists = self._pool.fetch_one(
                conn,
                "SELECT 1 FROM webui_users WHERE username = %s",
                (username,),
            )
            if exists:
                self._pool.execute(
                    conn,
                    "UPDATE webui_users SET password_hash = %s WHERE username = %s",
                    (password_hash, username),
                )
            else:
                self._pool.execute(
                    conn,
                    (
                        "INSERT INTO webui_users "
                        "(username, password_hash, is_admin) VALUES (%s, %s, %s)"
                    ),
                    (username, password_hash, is_admin),
                )

    def delete_user(self, username: str) -> bool:
        """Delete a Web UI user by username.

        Returns True if the user was deleted, False if not found.
        """
        with self._pool.checkout() as conn:
            rowcount = self._pool.execute(
                conn,
                "DELETE FROM webui_users WHERE username = %s",
                (username,),
            )
        return rowcount > 0

    def list_users(self) -> list[dict]:
        """List all Web UI users (W-CP CLI — minimal columns)."""
        with self._pool.checkout() as conn:
            return self._pool.fetch_all(
                conn,
                (
                    "SELECT username, is_admin, is_active, created_at "
                    "FROM webui_users ORDER BY username"
                ),
            )

    # ------------------------------------------------------------------
    # M9: User management (list/deactivate/reactivate + get_user_field)
    # ------------------------------------------------------------------

    def list_webui_users(self) -> list[dict]:
        """Return all webui_users rows (no password_hash) ordered by id/username."""
        with self._pool.checkout() as conn:
            rows = self._pool.fetch_all(
                conn,
                "SELECT id, username, email, is_admin, is_active, mfa_enabled, created_at "
                "FROM webui_users ORDER BY id NULLS LAST, username",
            )
        result = []
        for r in rows:
            result.append({
                "id": r["id"],
                "username": r["username"],
                "email": r["email"],
                "is_admin": bool(r["is_admin"]),
                "is_active": bool(r["is_active"]),
                "mfa_enabled": bool(r["mfa_enabled"]),
                "created_at": str(r["created_at"]) if r["created_at"] else None,
            })
        return result

    def get_user_by_id(self, user_id: int) -> dict | None:
        """Return webui_users row by id (no password_hash), or None."""
        with self._pool.checkout() as conn:
            row = self._pool.fetch_one(
                conn,
                "SELECT id, username, email, is_admin, is_active, mfa_enabled, created_at "
                "FROM webui_users WHERE id = %s",
                (user_id,),
            )
        if row is None:
            return None
        return {
            "id": row["id"],
            "username": row["username"],
            "email": row["email"],
            "is_admin": bool(row["is_admin"]),
            "is_active": bool(row["is_active"]),
            "mfa_enabled": bool(row["mfa_enabled"]),
            "created_at": str(row["created_at"]) if row["created_at"] else None,
        }

    def get_user_field(self, user_id: int, field: str) -> object | None:
        """Return a single field value from webui_users by id.

        Only allows safe whitelisted field names to prevent SQL injection.

        Args:
            user_id: The webui_users id.
            field: Column name to fetch ('is_admin', 'is_active', 'username', 'email').

        Returns:
            Field value, or None if user not found.

        Raises:
            ValueError: If field is not in the whitelist.
        """
        _ALLOWED = {"is_admin", "is_active", "username", "email", "mfa_enabled"}
        if field not in _ALLOWED:
            raise ValueError(f"get_user_field: field '{field}' not allowed")
        with self._pool.checkout() as conn:
            row = self._pool.fetch_one(
                conn,
                f"SELECT {field} FROM webui_users WHERE id = %s",  # noqa: S608 — field whitelisted
                (user_id,),
            )
        return row[field] if row is not None else None

    def get_user_id_by_username(self, username: str) -> int | None:
        """Return the id column for a given username, or None."""
        with self._pool.checkout() as conn:
            row = self._pool.fetch_one(
                conn,
                "SELECT id FROM webui_users WHERE username = %s",
                (username,),
            )
        return row["id"] if row is not None else None


    def set_user_active(self, user_id: int, *, is_active: bool) -> None:
        """Set is_active flag for a user by id.

        When deactivating an admin, verifies that at least one other active admin
        remains. Raises LastAdminProtectedError if the user is the last active admin.

        TOCTOU safety: the target row is locked with SELECT ... FOR UPDATE before the
        admin-count check, matching the pattern in set_user_admin().

        Raises:
            LastAdminProtectedError: Deactivating the last active admin is blocked.
        """
        with self._pool.checkout() as conn:
            conn.autocommit = False
            try:
                # Last-admin protection when deactivating
                if not is_active:
                    # Lock the target row to serialise concurrent deactivations.
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT is_admin FROM webui_users WHERE id = %s FOR UPDATE",
                            (user_id,),
                        )
                        row = cur.fetchone()
                    is_this_user_admin = row is not None and bool(row[0])
                    if is_this_user_admin:
                        with conn.cursor() as cur:
                            cur.execute(
                                "SELECT count(*) FROM webui_users "
                                "WHERE is_admin = TRUE AND is_active = TRUE AND id != %s",
                                (user_id,),
                            )
                            other_admin_count = cur.fetchone()[0]
                        if other_admin_count == 0:
                            raise LastAdminProtectedError(
                                "Cannot deactivate the last active admin"
                            )
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE webui_users SET is_active = %s WHERE id = %s",
                        (is_active, user_id),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.autocommit = True

    def set_user_admin(self, user_id: int, is_admin: bool) -> list[int]:
        """Set is_admin flag for a user by id.

        When demoting (is_admin=False), verifies that at least one other active admin
        remains. Raises LastAdminProtectedError if this user is the last active admin.

        TOCTOU safety: the target row is locked with SELECT ... FOR UPDATE before the
        admin-count check. This serialises concurrent demote calls so two callers
        cannot both pass the guard simultaneously and leave 0 admins.

        SECURITY (ADR-0034, m13_019 — privilege-persistence gap): demoting an admin
        does NOT touch their API keys, so an admin who minted an unrestricted
        ``tenant_id IS NULL`` key keeps cross-tenant read access after losing admin
        rights (the read-side guard `_is_null_tenant_escalation` in
        `src/mcp/middleware.py` only fires once the key is non-admin-owned AND the
        per-key owner cache has refreshed). To close that gap on the WRITE side, a
        demote re-scopes every ACTIVE, ``tenant_id IS NULL`` key the user owns to a
        concrete tenant (public / viindoo via :meth:`resolve_default_mint_tenant_id`)
        in the SAME transaction as the is_admin flip — the key downgrades to scoped
        access instead of relying solely on the read-side guard. Already-scoped keys,
        inactive keys, and promote (is_admin=True) leave keys untouched.

        Fail-closed ordering: the target tenant is resolved BEFORE any UPDATE runs.
        A resolver failure (e.g. tenants missing) raises and rolls back the whole
        transaction — the is_admin flip is never committed without the matching
        re-scope, so the demotion fails rather than leaving keys unrestricted.

        Returns:
            The list of api_keys.id owned by ``user_id`` (active + inactive). The
            route layer uses this to invalidate the per-key MCP middleware cache so
            the is_admin / tenant_id change takes effect immediately instead of after
            the 300 s cache TTL. Cache invalidation is a middleware concern and is
            deliberately left to the caller — the store layer does not import
            `src.mcp.middleware`.

        Raises:
            LastAdminProtectedError: Demoting the last active admin is blocked.
            UserNotFoundError: user_id does not exist.
        """
        with self._pool.checkout() as conn:
            conn.autocommit = False
            try:
                if not is_admin:
                    # Lock the target row first — prevents concurrent demotes from
                    # both passing the "other admin count > 0" guard simultaneously.
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT id FROM webui_users WHERE id = %s FOR UPDATE",
                            (user_id,),
                        )
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT count(*) FROM webui_users "
                            "WHERE is_admin = TRUE AND is_active = TRUE AND id != %s",
                            (user_id,),
                        )
                        other_admin_count = cur.fetchone()[0]
                    if other_admin_count == 0:
                        raise LastAdminProtectedError(
                            "Cannot demote the last active admin"
                        )

                # Resolve the re-scope tenant BEFORE any UPDATE (fail-closed): if
                # the user has active, unrestricted (tenant_id IS NULL) keys and is
                # being demoted, they must be bound to a concrete tenant. A resolver
                # failure here raises and aborts the whole txn — is_admin is never
                # flipped without the matching re-scope.
                scoped_tenant_id: int | None = None
                if not is_admin:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT EXISTS (SELECT 1 FROM api_keys "
                            "WHERE user_id = %s AND active = TRUE "
                            "AND tenant_id IS NULL)",
                            (user_id,),
                        )
                        has_unrestricted = bool(cur.fetchone()[0])
                    if has_unrestricted:
                        # Demotion target is a non-admin → always a concrete tenant.
                        # resolve_default_mint_tenant_id reads tenants/membership on
                        # its own connection; the demote txn does not mutate those,
                        # so there is no read conflict.
                        scoped_tenant_id = self.resolve_default_mint_tenant_id(user_id)

                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE webui_users SET is_admin = %s WHERE id = %s",
                        (is_admin, user_id),
                    )
                    rowcount = cur.rowcount
                if rowcount == 0:
                    raise UserNotFoundError(f"User id={user_id} not found")

                # Re-scope active, unrestricted keys in the SAME transaction.
                if scoped_tenant_id is not None:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE api_keys SET tenant_id = %s "
                            "WHERE user_id = %s AND active = TRUE "
                            "AND tenant_id IS NULL",
                            (scoped_tenant_id, user_id),
                        )

                # Enumerate ALL of the user's keys (active + inactive) for cache
                # invalidation by the caller. Done in-txn so the list reflects the
                # committed post-update ownership state.
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id FROM api_keys WHERE user_id = %s",
                        (user_id,),
                    )
                    affected_key_ids = [r[0] for r in cur.fetchall()]

                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.autocommit = True
        return affected_key_ids

    def revoke_all_sessions(self, user_id: int) -> None:
        """Delete all active_sessions for user_id (instant logout).

        Also clears the Starlette session cache if active_sessions table is
        used; this method is the single place to invalidate all logins.
        """
        with self._pool.checkout() as conn:
            self._pool.execute(
                conn,
                "DELETE FROM active_sessions WHERE user_id = %s",
                (user_id,),
            )

    def get_user_password_hash_by_id(self, user_id: int) -> str | None:
        """Return password_hash for user_id, or None if not found."""
        with self._pool.checkout() as conn:
            row = self._pool.fetch_one(
                conn,
                "SELECT password_hash FROM webui_users WHERE id = %s",
                (user_id,),
            )
        return row["password_hash"] if row is not None else None

    # ------------------------------------------------------------------
    # M9: Email verifications (password reset tokens)
    # ------------------------------------------------------------------

    def create_password_reset_token(self, user_id: int, ttl_seconds: int = 3600) -> str:
        """Create a single-use password reset token for user_id.

        Returns:
            The raw 256-bit token (hex string). Only the SHA-256 hash is stored.
            Caller must transmit the raw token to the user (email link).
        """
        raw_token = secrets.token_hex(32)  # 256-bit entropy
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        now_utc = _datetime_mod.datetime.now(_datetime_mod.UTC)
        expires_at = now_utc + timedelta(seconds=ttl_seconds)
        with self._pool.checkout() as conn:
            # Try inserting with token_hash only (fresh 9001 schema).
            # Fall back to also setting `token` if the legacy NOT NULL column exists
            # (live DB from other M9 worktrees that have token TEXT NOT NULL PK).
            try:
                self._pool.execute(
                    conn,
                    "INSERT INTO email_verifications"
                    " (user_id, purpose, token_hash, expires_at)"
                    " VALUES (%s, %s, %s, %s)",
                    (user_id, "password_reset", token_hash, expires_at),
                )
            except Exception:
                # Retry including the legacy 'token' column (satisfies NOT NULL constraint)
                self._pool.execute(
                    conn,
                    "INSERT INTO email_verifications"
                    " (user_id, purpose, token_hash, expires_at, token)"
                    " VALUES (%s, %s, %s, %s, %s)",
                    (user_id, "password_reset", token_hash, expires_at, token_hash),
                )
        return raw_token

    def verify_password_reset_token(self, raw_token: str) -> int | None:
        """Verify a password reset token WITHOUT consuming it (peek).

        Lets a caller surface token-validity errors (not_found / expired / used)
        before running other gates (e.g. password policy) so a rejected attempt
        does not burn the single-use token. Use :meth:`consume_password_reset_token`
        to atomically burn the token once all other gates pass.

        Returns:
            user_id if token is valid + unused + not expired.
            None if token not found.

        Raises:
            ValueError: with code 'expired' if token found but expired.
            ValueError: with code 'used' if token already consumed.
        """
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        with self._pool.checkout() as conn:
            row = self._pool.fetch_one(
                conn,
                "SELECT user_id, expires_at, used_at FROM email_verifications "
                "WHERE token_hash = %s AND purpose = %s",
                (token_hash, "password_reset"),
            )
        if row is None:
            return None
        if row["used_at"] is not None:
            raise ValueError("used")
        # expires_at may be offset-aware or naive; compare consistently
        now = _datetime_mod.datetime.now(_datetime_mod.UTC)
        exp = row["expires_at"]
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=_datetime_mod.UTC)
        if now > exp:
            raise ValueError("expired")
        return row["user_id"]

    def consume_password_reset_token(self, raw_token: str) -> int | None:
        """Verify + consume a password reset token.

        Returns:
            user_id if token is valid + unused + not expired.
            None if token not found or already used.

        Raises:
            ValueError: with code 'expired' if token found but expired.
            ValueError: with code 'used' if token already consumed.

        TOCTOU safety: the token row is locked with SELECT ... FOR UPDATE and the
        check + UPDATE run in a single transaction (autocommit disabled — the pool
        yields autocommit=True, so without this the lock would release immediately
        after the SELECT). Two concurrent requests for the same token serialise: the
        second blocks on FOR UPDATE until the first commits used_at, then sees
        used_at != NULL and raises 'used' — no double-spend. Mirrors delete_tenant().
        """
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        with self._pool.checkout() as conn:
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT user_id, expires_at, used_at FROM email_verifications "
                        "WHERE token_hash = %s AND purpose = %s FOR UPDATE",
                        (token_hash, "password_reset"),
                    )
                    row = cur.fetchone()
                if row is None:
                    conn.commit()
                    return None
                user_id, expires_at, used_at = row[0], row[1], row[2]
                if used_at is not None:
                    raise ValueError("used")
                # expires_at may be offset-aware or naive; compare consistently
                now = _datetime_mod.datetime.now(_datetime_mod.UTC)
                exp = expires_at
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=_datetime_mod.UTC)
                if now > exp:
                    raise ValueError("expired")
                # Mark as used — identify row by token_hash (unique column; no id needed)
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE email_verifications SET used_at = NOW() "
                        "WHERE token_hash = %s",
                        (token_hash,),
                    )
                conn.commit()
                return user_id
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.autocommit = True
