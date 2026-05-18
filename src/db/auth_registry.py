"""CRUD for api_keys, ssh_key_pairs, usage_log, webui_users tables via AuthStore."""
import datetime as _datetime_mod
import hashlib
import logging
import secrets
from datetime import timedelta

from src.auth import hash_key, hash_key_legacy_sha256
from src.db.pg import PgPool

logger = logging.getLogger(__name__)


class LastAdminProtectedError(Exception):
    """Raised when an operation would remove the last active admin."""


class UserNotFoundError(Exception):
    """Raised when a user_id does not exist in webui_users."""


class KeyNotFoundError(Exception):
    """Raised when a key_id does not exist in api_keys."""


class AuthStore:
    """Encapsulates all auth / key / SSH / feedback SQL operations."""

    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------
    # API keys
    # ------------------------------------------------------------------

    def create_api_key(
        self,
        name: str,
        user_id: int | None = None,
        expires_at=None,
    ) -> tuple[str, str, int]:
        """Create API key with HMAC-SHA256 hash, optional user ownership and expiry.

        Args:
            name: Descriptive name for the key (e.g. 'claude-code-laptop').
            user_id: Optional webui_users.id FK.  None = global/admin key
                (CLI-created keys, backward compat).
            expires_at: Optional datetime (or None for eternal).  When set,
                the key is rejected after this timestamp.

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
                    "INSERT INTO api_keys (name, key_hash, key_prefix, user_id, expires_at)"
                    " VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    (name, key_hash, key_prefix, user_id, expires_at),
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
            last_used_at, user_id, expires_at, owner_username (None for system keys).
        """
        select = (
            "SELECT k.id, k.name, k.key_prefix, k.active, k.created_at, k.last_used_at, "
            "k.user_id, k.expires_at, u.username AS owner_username "
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

    # ------------------------------------------------------------------
    # SSH key pairs
    # ------------------------------------------------------------------

    def list_ssh_keys(self) -> list[dict]:
        """List SSH key pairs (without private key for security).

        Returns:
            List of dicts with keys: id, name, public_key, key_version, created_at.
        """
        with self._pool.checkout() as conn:
            return self._pool.fetch_all(
                conn,
                "SELECT id, name, public_key, key_version, created_at"
                " FROM ssh_key_pairs ORDER BY id",
            )

    def get_ssh_key_by_id(self, key_id: int) -> dict | None:
        """Return ssh_key_pairs row (including private_key_encrypted) or None."""
        with self._pool.checkout() as conn:
            return self._pool.fetch_one(
                conn,
                "SELECT id, name, public_key, private_key_encrypted, key_version, created_at "
                "FROM ssh_key_pairs WHERE id = %s",
                (key_id,),
            )

    def save_ssh_key(
        self,
        name: str,
        public_key: str,
        private_key_encrypted: str,
        key_version: int = 1,
    ) -> int:
        """Save SSH key pair. Return id.

        Args:
            name: Descriptive name for this key pair.
            public_key: The full public key string (e.g. 'ssh-ed25519 AAAA...').
            private_key_encrypted: Private key encrypted with a master key (not stored raw).
            key_version: Version of the encryption key used (default 1).

        Returns:
            Integer id of the newly created ssh_key_pair row.
        """
        with self._pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ssh_key_pairs"
                    " (name, public_key, private_key_encrypted, key_version)"
                    " VALUES (%s, %s, %s, %s) RETURNING id",
                    (name, public_key, private_key_encrypted, key_version),
                )
                row_id = cur.fetchone()[0]
            conn.commit()
        return row_id

    def delete_ssh_key(self, key_id: int) -> None:
        """Delete SSH key pair by id.

        Args:
            key_id: The ssh_key_pairs id to delete.
        """
        with self._pool.checkout() as conn:
            self._pool.execute(
                conn,
                "DELETE FROM ssh_key_pairs WHERE id = %s",
                (key_id,),
            )

    # ------------------------------------------------------------------
    # Pattern feedback
    # ------------------------------------------------------------------

    def create_feedback(
        self,
        *,
        pattern_node_id: str,
        api_key_id: int | None,
        rating: str,
        comment: str | None = None,
    ) -> int:
        """Store a thumbs-up/down rating for a PatternExample node.

        Args:
            pattern_node_id: Neo4j node id or pattern_id string
                (e.g. 'python__write-read-before-super').
            api_key_id: Authenticated API key id (or None for anonymous).
            rating: 'up' or 'down'.
            comment: Optional free-text comment from the user.

        Returns:
            Integer id of the newly created pattern_feedback row.
        """
        with self._pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO pattern_feedback (pattern_node_id, api_key_id, rating, comment) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
                    (pattern_node_id, api_key_id, rating, comment),
                )
                row_id = cur.fetchone()[0]
            conn.commit()
        return row_id

    def list_feedback(self, pattern_node_id: str) -> list[dict]:
        """Return all feedback entries for a given pattern node, newest first.

        Args:
            pattern_node_id: The pattern id to filter on.

        Returns:
            List of dicts with keys: id, api_key_id, rating, comment, created_at.
        """
        with self._pool.checkout() as conn:
            rows = self._pool.fetch_all(
                conn,
                "SELECT id, api_key_id, rating, comment, created_at "
                "FROM pattern_feedback "
                "WHERE pattern_node_id = %s "
                "ORDER BY created_at DESC",
                (pattern_node_id,),
            )
        return [
            {
                "id": r["id"],
                "api_key_id": r["api_key_id"],
                "rating": r["rating"],
                "comment": r["comment"],
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ]

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

    def set_user_admin(self, user_id: int, is_admin: bool) -> None:
        """Set is_admin flag for a user by id.

        When demoting (is_admin=False), verifies that at least one other active admin
        remains. Raises LastAdminProtectedError if this user is the last active admin.

        TOCTOU safety: the target row is locked with SELECT ... FOR UPDATE before the
        admin-count check. This serialises concurrent demote calls so two callers
        cannot both pass the guard simultaneously and leave 0 admins.

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
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE webui_users SET is_admin = %s WHERE id = %s",
                        (is_admin, user_id),
                    )
                    rowcount = cur.rowcount
                if rowcount == 0:
                    raise UserNotFoundError(f"User id={user_id} not found")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.autocommit = True

    def assign_key_owner(self, key_id: int, new_user_id: int | None) -> None:
        """Reassign ownership of an API key to a different user (or clear it).

        Args:
            key_id: The api_keys.id to reassign.
            new_user_id: The webui_users.id to assign, or None to clear ownership
                (system/global key).

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
            rowcount = self._pool.execute(
                conn,
                "UPDATE api_keys SET user_id = %s WHERE id = %s",
                (new_user_id, key_id),
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

    def consume_password_reset_token(self, raw_token: str) -> int | None:
        """Verify + consume a password reset token.

        Returns:
            user_id if token is valid + unused + not expired.
            None if token not found or already used.

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
            # Mark as used — identify row by token_hash (unique column; no id needed)
            self._pool.execute(
                conn,
                "UPDATE email_verifications SET used_at = NOW() WHERE token_hash = %s",
                (token_hash,),
            )
        return row["user_id"]

    # ------------------------------------------------------------------
    # M9: Admin audit log
    # ------------------------------------------------------------------

    def log_audit(
        self,
        *,
        actor_id: int | None,
        action: str,
        target_id: int | None = None,
        detail: str | None = None,
    ) -> None:
        """Insert an admin audit log entry. Fire-and-forget — swallows exceptions.

        DEPRECATED (M9 W-AL): Use src.db.audit.write_audit_log() directly instead.
        This method is kept for backward compatibility only and will be removed
        after M9 in a cleanup PR (per ADR-0021 §Legacy Column Deprecation).

        New callers should use:
            from src.db.audit import write_audit_log
            write_audit_log(actor=f"user:{actor_id}", action=action, target=str(target_id))

        Args:
            actor_id: webui_users.id of the admin performing the action.
            action: Short action code (e.g. 'user.deactivate', 'user.reset_password').
            target_id: webui_users.id of the affected user (if applicable).
            detail: Optional free-text detail string.
        """
        try:
            with self._pool.checkout() as conn:
                # actor_str: string fallback for schemas that have `actor TEXT NOT NULL`
                # (from other M9 worktrees). Provides a value even when actor_id is None.
                actor_str = str(actor_id) if actor_id is not None else "system"
                self._pool.execute(
                    conn,
                    "INSERT INTO admin_audit_log"
                    " (actor_id, action, target_id, detail_text, actor, success)"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    (actor_id, action, target_id, detail, actor_str, True),
                )
        except Exception:
            # Fall back to minimal insert if actor column doesn't exist (fresh schema)
            try:
                with self._pool.checkout() as conn:
                    self._pool.execute(
                        conn,
                        "INSERT INTO admin_audit_log"
                        " (actor_id, action, target_id, detail_text)"
                        " VALUES (%s, %s, %s, %s)",
                        (actor_id, action, target_id, detail),
                    )
            except Exception:
                pass  # best-effort — audit failure must not break the main action
