# SPDX-License-Identifier: AGPL-3.0-or-later
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
        """List admin-managed access-key SSH pairs (without private key).

        Excludes ``key_type='deploy_key'`` rows: per-tenant deploy keys are
        owned via the self-service endpoint (ADR-0034 D7), not the admin SSH-key
        surface, and must never appear in the admin "Stored Keys" table, the
        Add-Repo SSH-key dropdown, or the dashboard count. The clone path
        resolves credentials by id via get_ssh_key_by_id (unaffected by this
        filter).

        Returns:
            List of dicts with keys: id, name, public_key, key_version, created_at.
        """
        with self._pool.checkout() as conn:
            return self._pool.fetch_all(
                conn,
                "SELECT id, name, public_key, key_version, created_at"
                " FROM ssh_key_pairs WHERE key_type = 'access_key' ORDER BY id",
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

    def get_or_create_tenant_deploy_key(self, conn, tenant_id: int) -> str:
        """Return the public key for the tenant's deploy keypair.

        Idempotent: if a row with key_type='deploy_key' and the given tenant_id
        already exists, return its public_key.  Otherwise generate a new Ed25519
        keypair, FERNET-encrypt the private key (reusing generate_ed25519_keypair
        from src.web_ui.routes.ssh_keys), and INSERT it.  The private key is
        NEVER returned.

        This method accepts an open psycopg2 connection so the caller can manage
        the transaction boundary.  Caller is responsible for commit.

        Args:
            conn: Open psycopg2 connection (NOT drawn from the pool — caller owns it).
            tenant_id: tenants.id for which the deploy key is being requested.

        Returns:
            The OpenSSH public key string (e.g. 'ssh-ed25519 AAAA...').

        Raises:
            RuntimeError: If FERNET_KEY is not set (ADR-0020 fail-fast policy).
        """
        from src.web_ui.routes.ssh_keys import generate_ed25519_keypair

        with conn.cursor() as cur:
            cur.execute(
                "SELECT public_key FROM ssh_key_pairs "
                "WHERE tenant_id = %s AND key_type = 'deploy_key' "
                "LIMIT 1",
                (tenant_id,),
            )
            row = cur.fetchone()

        if row is not None:
            return row[0]

        # No deploy key yet — generate and persist one.
        public_key, private_key_encrypted = generate_ed25519_keypair()
        # ON CONFLICT guards the check-then-insert race: if a concurrent
        # first-time request for the same tenant won, our INSERT no-ops
        # (RETURNING yields no row) and we re-read the winner's public key so
        # both callers return the SAME key. Conflict target matches the partial
        # index ux_ssh_deploy_key_per_tenant (m13_002).
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ssh_key_pairs"
                " (name, public_key, private_key_encrypted, key_version, key_type, tenant_id)"
                " VALUES (%s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (tenant_id) WHERE key_type = 'deploy_key' DO NOTHING"
                " RETURNING public_key",
                (
                    f"deploy-key-tenant-{tenant_id}",
                    public_key,
                    private_key_encrypted,
                    1,
                    "deploy_key",
                    tenant_id,
                ),
            )
            row = cur.fetchone()
        if row is not None:
            return row[0]

        # Lost the race — a peer inserted the tenant's deploy key first.
        # Return their public key so the result is idempotent.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT public_key FROM ssh_key_pairs "
                "WHERE tenant_id = %s AND key_type = 'deploy_key' "
                "LIMIT 1",
                (tenant_id,),
            )
            row = cur.fetchone()
        return row[0]

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
        in a future major release.  Only the canonical columns (actor, action,
        target, success) are written; legacy columns were dropped by migration
        m9_010_drop_audit_legacy_columns.sql.

        New callers should use:
            from src.db.audit import write_audit_log
            write_audit_log(actor=f"user:{actor_id}", action=action, target=str(target_id))

        Args:
            actor_id: webui_users.id of the admin performing the action.
            action: Short action code (e.g. 'user.deactivate', 'user.reset_password').
            target_id: webui_users.id of the affected user (if applicable).
            detail: Accepted for backward compatibility; not persisted (INSERT omits this column).
        """
        try:
            with self._pool.checkout() as conn:
                # Canonical-only insert (M10 WI-4: legacy actor_id/target_id/detail_text
                # columns dropped by migration m9_010_drop_audit_legacy_columns.sql).
                actor_str = str(actor_id) if actor_id is not None else "system"
                target_str = str(target_id) if target_id is not None else None
                self._pool.execute(
                    conn,
                    "INSERT INTO admin_audit_log"
                    " (actor, action, target, success)"
                    " VALUES (%s, %s, %s, %s)",
                    (actor_str, action, target_str, True),
                )
        except Exception:
            pass  # best-effort — audit failure must not break the main action

    # ------------------------------------------------------------------
    # W1: Tenant membership CRUD (multi-tenant-per-user model, ADR-0038)
    # ------------------------------------------------------------------

    def list_tenants(self) -> list[dict]:
        """Return all tenants ordered by id, with member/repo/profile counts.

        Returns:
            List of dicts with keys: id, name, active, created_at,
            member_count, repo_count, profile_count.
        """
        with self._pool.checkout() as conn:
            rows = self._pool.fetch_all(
                conn,
                """
                SELECT
                    t.id, t.name, t.active, t.created_at,
                    (SELECT count(*) FROM tenant_members tm WHERE tm.tenant_id = t.id)
                        AS member_count,
                    (SELECT count(*) FROM repos r WHERE r.tenant_id = t.id)
                        AS repo_count,
                    (SELECT count(*) FROM profiles p WHERE p.tenant_id = t.id)
                        AS profile_count
                FROM tenants t
                ORDER BY t.id
                """,
            )
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "active": bool(r["active"]),
                "created_at": str(r["created_at"]) if r["created_at"] else None,
                "member_count": int(r["member_count"]),
                "repo_count": int(r["repo_count"]),
                "profile_count": int(r["profile_count"]),
            }
            for r in rows
        ]

    def get_tenant_by_id(self, tenant_id: int) -> dict | None:
        """Return a single tenant row or None if not found."""
        with self._pool.checkout() as conn:
            row = self._pool.fetch_one(
                conn,
                "SELECT id, name, active, created_at FROM tenants WHERE id = %s",
                (tenant_id,),
            )
        if row is None:
            return None
        return {
            "id": row["id"],
            "name": row["name"],
            "active": bool(row["active"]),
            "created_at": str(row["created_at"]) if row["created_at"] else None,
        }

    def create_tenant(self, name: str) -> int:
        """Create a new tenant.

        Args:
            name: Unique tenant name. Must not contain ',' (GUC-delimiter guard).

        Returns:
            Integer id of the newly created tenant.

        Raises:
            ValueError: name contains ',' or is empty.
            psycopg2.errors.UniqueViolation: name is already taken.
        """
        if not name or not name.strip():
            raise ValueError("Tenant name must not be empty")
        if "," in name:
            raise ValueError("Tenant name must not contain ','")
        name = name.strip()
        with self._pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO tenants (name) VALUES (%s) RETURNING id",
                    (name,),
                )
                row_id = cur.fetchone()[0]
            conn.commit()
        return row_id

    def update_tenant(
        self, tenant_id: int, *, name: str | None = None, active: bool | None = None
    ) -> bool:
        """Update a tenant's name and/or active flag.

        Args:
            tenant_id: The tenant to update.
            name: New name (optional). Must not be empty or contain ','.
            active: New active flag (optional).

        Returns:
            True if the tenant was found and updated, False if not found.

        Raises:
            ValueError: name contains ',' or is empty.
            psycopg2.errors.UniqueViolation: name is already taken.
        """
        if name is not None and "," in name:
            raise ValueError("Tenant name must not contain ','")
        if name is not None and not name.strip():
            raise ValueError("Tenant name must not be empty")
        parts = []
        params: list = []
        if name is not None:
            parts.append("name = %s")
            params.append(name.strip())
        if active is not None:
            parts.append("active = %s")
            params.append(active)
        if not parts:
            return True
        params.append(tenant_id)
        with self._pool.checkout() as conn:
            rowcount = self._pool.execute(
                conn,
                f"UPDATE tenants SET {', '.join(parts)} WHERE id = %s",  # noqa: S608
                tuple(params),
            )
        return rowcount > 0

    def delete_tenant(self, tenant_id: int) -> bool:
        """Delete a tenant.

        Raises ValueError if the tenant still has repos or profiles assigned to it
        (D8 — no silent cascade-to-NULL; membership CASCADE is allowed).

        TOCTOU safety: the tenant row is locked with SELECT ... FOR UPDATE before the
        resource-count check, and the count + DELETE run in a single transaction. A
        concurrent PATCH that assigns a repo/profile to this tenant takes a FOR KEY
        SHARE lock on this same row (the FK reference), which conflicts with FOR
        UPDATE — so the assign is serialised against the delete and cannot slip in
        between the count check and the DELETE. Without this, the count and DELETE ran
        in two separate pool checkouts; because repos.tenant_id / profiles.tenant_id
        are ON DELETE CASCADE (m13_002), a raced assign would have been silently
        cascade-deleted — exactly the data loss D8 forbids. Mirrors set_user_admin().

        Args:
            tenant_id: The tenant to delete.

        Returns:
            True if deleted, False if not found.

        Raises:
            ValueError: tenant still has repo or profile resources assigned.
        """
        with self._pool.checkout() as conn:
            conn.autocommit = False
            try:
                # Lock the tenant row first — serialises concurrent resource-assigns
                # (their FK FOR KEY SHARE lock conflicts with this FOR UPDATE).
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id FROM tenants WHERE id = %s FOR UPDATE",
                        (tenant_id,),
                    )
                    if cur.fetchone() is None:
                        conn.commit()
                        return False  # not found
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT count(*) FROM repos WHERE tenant_id = %s",
                        (tenant_id,),
                    )
                    repo_count = cur.fetchone()[0]
                if repo_count > 0:
                    raise ValueError(
                        f"Tenant {tenant_id} still has {repo_count} repo(s) assigned. "
                        "Unassign them before deleting the tenant."
                    )
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT count(*) FROM profiles WHERE tenant_id = %s",
                        (tenant_id,),
                    )
                    profile_count = cur.fetchone()[0]
                if profile_count > 0:
                    raise ValueError(
                        f"Tenant {tenant_id} still has {profile_count} profile(s) "
                        "assigned. Unassign them before deleting the tenant."
                    )
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
                    rowcount = cur.rowcount
                conn.commit()
                return rowcount > 0
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.autocommit = True

    def list_tenant_ids_for_user(self, user_id: int) -> list[int]:
        """Return tenant_ids the user has a membership row for. [] if none.

        Args:
            user_id: webui_users.id to look up.

        Returns:
            List of tenant_id integers. Empty list if no memberships.
        """
        with self._pool.checkout() as conn:
            rows = self._pool.fetch_all(
                conn,
                "SELECT tenant_id FROM tenant_members WHERE user_id = %s",
                (user_id,),
            )
        return [r["tenant_id"] for r in rows]

    def list_tenant_memberships_for_user(self, user_id: int) -> list[dict]:
        """Return [{tenant_id, name, role}] for all tenants the user belongs to.

        Joins tenant_members with tenants to include the tenant name.
        Used by GET /api/account/tenants (W2 self-service portal).

        Args:
            user_id: webui_users.id to look up.

        Returns:
            List of dicts with keys: tenant_id, name, role. Empty list if no memberships.
        """
        with self._pool.checkout() as conn:
            rows = self._pool.fetch_all(
                conn,
                """
                SELECT tm.tenant_id, t.name, tm.role
                FROM tenant_members tm
                JOIN tenants t ON t.id = tm.tenant_id
                WHERE tm.user_id = %s
                ORDER BY t.name
                """,
                (user_id,),
            )
        return [
            {
                "tenant_id": r["tenant_id"],
                "name": r["name"],
                "role": r["role"],
            }
            for r in rows
        ]

    def user_is_member_of(self, user_id: int, tenant_id: int) -> bool:
        """True iff a tenant_members row exists for (user_id, tenant_id).

        Args:
            user_id: webui_users.id.
            tenant_id: tenants.id.

        Returns:
            True if membership row exists, False otherwise.
        """
        with self._pool.checkout() as conn:
            row = self._pool.fetch_one(
                conn,
                "SELECT 1 FROM tenant_members WHERE user_id = %s AND tenant_id = %s LIMIT 1",
                (user_id, tenant_id),
            )
        return row is not None

    def add_tenant_member(self, user_id: int, tenant_id: int, role: str = "member") -> None:
        """Add or update a user's membership in a tenant.

        Idempotent: ON CONFLICT upserts the role if the row already exists.

        Args:
            user_id: webui_users.id.
            tenant_id: tenants.id.
            role: 'member' (default) or 'tenant_admin'.
        """
        if role not in ("member", "tenant_admin"):
            raise ValueError(f"Invalid role '{role}'. Must be 'member' or 'tenant_admin'.")
        with self._pool.checkout() as conn:
            self._pool.execute(
                conn,
                """
                INSERT INTO tenant_members (user_id, tenant_id, role)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, tenant_id) DO UPDATE SET role = EXCLUDED.role
                """,
                (user_id, tenant_id, role),
            )

    def remove_tenant_member(self, user_id: int, tenant_id: int) -> None:
        """Remove a user's membership from a tenant.

        Args:
            user_id: webui_users.id.
            tenant_id: tenants.id.
        """
        with self._pool.checkout() as conn:
            self._pool.execute(
                conn,
                "DELETE FROM tenant_members WHERE user_id = %s AND tenant_id = %s",
                (user_id, tenant_id),
            )

    def list_members_of_tenant(self, tenant_id: int) -> list[dict]:
        """Return all members of a tenant with user details.

        Args:
            tenant_id: tenants.id to list members for.

        Returns:
            List of dicts with keys: user_id, username, email, role, created_at.
        """
        with self._pool.checkout() as conn:
            rows = self._pool.fetch_all(
                conn,
                """
                SELECT tm.user_id, u.username, u.email, tm.role, tm.created_at
                FROM tenant_members tm
                JOIN webui_users u ON u.id = tm.user_id
                WHERE tm.tenant_id = %s
                ORDER BY u.username
                """,
                (tenant_id,),
            )
        return [
            {
                "user_id": r["user_id"],
                "username": r["username"],
                "email": r["email"],
                "role": r["role"],
                "created_at": str(r["created_at"]) if r["created_at"] else None,
            }
            for r in rows
        ]

    def assign_profile_tenant(self, profile_id: int, tenant_id: int | None) -> bool:
        """Assign (or clear) the tenant for a profile.

        Args:
            profile_id: profiles.id to update.
            tenant_id: tenants.id, or None to set as shared/global.

        Returns:
            True if the profile was found and updated, False otherwise.
        """
        with self._pool.checkout() as conn:
            rowcount = self._pool.execute(
                conn,
                "UPDATE profiles SET tenant_id = %s WHERE id = %s",
                (tenant_id, profile_id),
            )
        return rowcount > 0

    def assign_repo_tenant(self, repo_id: int, tenant_id: int | None) -> bool:
        """Assign (or clear) the tenant for a repo.

        Args:
            repo_id: repos.id to update.
            tenant_id: tenants.id, or None to set as shared/global.

        Returns:
            True if the repo was found and updated, False otherwise.
        """
        with self._pool.checkout() as conn:
            rowcount = self._pool.execute(
                conn,
                "UPDATE repos SET tenant_id = %s WHERE id = %s",
                (tenant_id, repo_id),
            )
        return rowcount > 0


# ---------------------------------------------------------------------------
# W-3 helpers: plan assignment + per-key overrides
# Added by W-3 of PR feat/m10b-p0-rbac-quota-ui (M10B P0-ext, ADR-0041).
# ---------------------------------------------------------------------------


def get_plan_by_id(pg_pool: "PgPool", plan_id: int) -> dict | None:
    # Added by W-3 of PR feat/m10b-p0-rbac-quota-ui (M10B P0-ext, ADR-0041).
    """Return plans row as dict or None if not found.

    Used by admin routes to validate plan_id before assignment.

    Args:
        pg_pool: PgPool instance (from src.db.pg.get_pool()).
        plan_id: plans.id to look up.

    Returns:
        Dict with keys id, slug, display_name, quota_calls_per_month,
        rate_limit_rpm, seat_limit, is_public — or None if not found.
    """
    with pg_pool.checkout() as conn:
        row = pg_pool.fetch_one(
            conn,
            "SELECT id, slug, display_name, quota_calls_per_month, "
            "rate_limit_rpm, seat_limit, is_public "
            "FROM plans WHERE id = %s",
            (plan_id,),
        )
    if row is None:
        return None
    return {
        "id": row["id"],
        "slug": row["slug"],
        "display_name": row["display_name"],
        "quota_calls_per_month": row["quota_calls_per_month"],
        "rate_limit_rpm": row["rate_limit_rpm"],
        "seat_limit": row["seat_limit"],
        "is_public": bool(row["is_public"]),
    }


def set_api_key_plan_and_overrides(
    pg_pool: "PgPool",
    key_id: int,
    plan_id: int,
    rate_limit_override: int | None,
    quota_override: int | None,
    *,
    update_rate_limit_override: bool = True,
    update_quota_override: bool = True,
) -> dict:
    # Added by W-3 of PR feat/m10b-p0-rbac-quota-ui (M10B P0-ext, ADR-0041).
    # BLOCK-1 fix: partial-update flags added so callers can update only the
    # columns that were explicitly present in the request body (model_fields_set).
    """Atomic UPDATE api_keys with partial-update semantics for override columns.

    Fetches the old snapshot before the update so callers can include both old
    and new values in the audit log.

    Args:
        pg_pool: PgPool instance.
        key_id: api_keys.id to update.
        plan_id: plans.id to assign (always updated).
        rate_limit_override: New rate-limit override value (used only when
            update_rate_limit_override=True).
        quota_override: New quota override value (used only when
            update_quota_override=True).
        update_rate_limit_override: When False, the rate_limit_override column
            is excluded from the SET clause and its current DB value is preserved.
            Default True for backward compatibility with callers that always
            intend to write the override column.
        update_quota_override: Same semantics as update_rate_limit_override but
            for the quota_override column.

    Returns:
        Dict with keys: old_plan_id, old_rate_limit_override, old_quota_override,
        new_plan_id, new_rate_limit_override, new_quota_override.
        new_rate_limit_override / new_quota_override reflect what was actually
        written: the supplied value if the flag is True, or the pre-existing DB
        value if the flag is False (preserved).

    Raises:
        KeyError: key_id does not exist in api_keys.
    """
    with pg_pool.checkout() as conn:
        conn.autocommit = False
        try:
            # Fetch current state (row-lock for atomicity)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT plan_id, rate_limit_override, quota_override "
                    "FROM api_keys WHERE id = %s FOR UPDATE",
                    (key_id,),
                )
                row = cur.fetchone()
            if row is None:
                raise KeyError(f"API key id={key_id} not found")
            old_plan_id = row[0]
            old_rate_limit_override = row[1]
            old_quota_override = row[2]

            # Build dynamic SET clause — only include override columns when the
            # caller explicitly flagged them as present in the request body.
            # plan_id is always updated (it is a required field in every request).
            set_parts = ["plan_id = %s"]
            params: list = [plan_id]

            if update_rate_limit_override:
                set_parts.append("rate_limit_override = %s")
                params.append(rate_limit_override)

            if update_quota_override:
                set_parts.append("quota_override = %s")
                params.append(quota_override)

            params.append(key_id)
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE api_keys SET {', '.join(set_parts)} WHERE id = %s",
                    params,
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True

    # Resolve the effective new values for the audit snapshot:
    # - if a column was updated, report the supplied value;
    # - if preserved, report the old DB value unchanged.
    new_rate_limit_override = (
        rate_limit_override if update_rate_limit_override else old_rate_limit_override
    )
    new_quota_override = (
        quota_override if update_quota_override else old_quota_override
    )

    return {
        "old_plan_id": old_plan_id,
        "old_rate_limit_override": old_rate_limit_override,
        "old_quota_override": old_quota_override,
        "new_plan_id": plan_id,
        "new_rate_limit_override": new_rate_limit_override,
        "new_quota_override": new_quota_override,
    }


def bulk_set_plan_for_user(
    pg_pool: "PgPool",
    user_id: int,
    plan_id: int,
) -> list[int]:
    # Added by W-3 of PR feat/m10b-p0-rbac-quota-ui (M10B P0-ext, ADR-0041).
    """UPDATE plan_id on ALL api_keys (active + inactive) for a given user.

    Per D3: cascade covers ALL keys regardless of active status.
    NOTE: does NOT touch per-key overrides (rate_limit_override, quota_override).

    Args:
        pg_pool: PgPool instance.
        user_id: webui_users.id — must exist (caller validates; 404 returned
            by route if user not found).
        plan_id: plans.id to assign to every key.

    Returns:
        List of affected api_keys.id values (for per-key cache invalidation).
        Empty list if the user has no keys (valid — caller returns 200 + count=0).
    """
    with pg_pool.checkout() as conn:
        conn.autocommit = False
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE api_keys SET plan_id = %s "
                    "WHERE user_id = %s RETURNING id",
                    (plan_id, user_id),
                )
                rows = cur.fetchall()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.autocommit = True
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# W-4 helper: reactivate an API key (symmetric counterpart of deactivate)
# Added by W-4 of PR feat/m10b-p0-rbac-quota-ui (M10B P0-ext).
# ---------------------------------------------------------------------------


def reactivate_api_key(pg_pool: "PgPool", key_id: int) -> dict | None:
    """Set api_keys.active = TRUE for the given key_id. Returns the updated
    row as dict, or None if the key does not exist. Idempotent — calling
    on an already-active key still returns the row without raising.

    Returns dict with keys: id, name, key_prefix, active, user_id, created_at,
    last_used_at, expires_at.

    Added by W-4 of PR feat/m10b-p0-rbac-quota-ui (M10B P0-ext).
    """
    with pg_pool.checkout() as conn:
        row = pg_pool.fetch_one(
            conn,
            "UPDATE api_keys SET active = TRUE WHERE id = %s "
            "RETURNING id, name, key_prefix, active, user_id, "
            "created_at, last_used_at, expires_at",
            (key_id,),
        )
    if row is None:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "key_prefix": row["key_prefix"],
        "active": bool(row["active"]),
        "user_id": row["user_id"],
        "created_at": row["created_at"],
        "last_used_at": row["last_used_at"],
        "expires_at": row["expires_at"],
    }
