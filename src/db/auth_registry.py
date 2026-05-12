"""CRUD for api_keys, ssh_key_pairs, usage_log, webui_users tables via AuthStore."""
import hashlib
import secrets

from src.db.pg import PgPool


class AuthStore:
    """Encapsulates all auth / key / SSH / feedback SQL operations."""

    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------
    # API keys
    # ------------------------------------------------------------------

    def create_api_key(self, name: str) -> tuple[str, str, int]:
        """Create API key. Return (raw_key, key_prefix, id). raw_key shown once.

        Args:
            name: Descriptive name for the key (e.g. 'claude-code-laptop').

        Returns:
            Tuple of (raw_key_string, key_prefix_8chars, key_id).
            raw_key is ephemeral and should be displayed to user exactly once.
        """
        raw = "osm_" + secrets.token_urlsafe(32)
        key_hash = hashlib.sha256(raw.encode()).hexdigest()
        key_prefix = raw[:8]
        with self._pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO api_keys (name, key_hash, key_prefix)"
                    " VALUES (%s, %s, %s) RETURNING id",
                    (name, key_hash, key_prefix),
                )
                row_id = cur.fetchone()[0]
            conn.commit()
        return raw, key_prefix, row_id

    def verify_api_key(self, raw_key: str) -> int | None:
        """Return api_key_id if active + valid. Update last_used_at. Return None if invalid.

        Args:
            raw_key: The full API key string (starts with 'osm_').

        Returns:
            Integer key_id if found and active, None otherwise.
            Side effect: updates last_used_at timestamp on successful verification.
        """
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        with self._pool.checkout() as conn:
            row = self._pool.fetch_one(
                conn,
                "SELECT id FROM api_keys WHERE key_hash = %s AND active = TRUE",
                (key_hash,),
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

    def list_api_keys(self) -> list[dict]:
        """List all API keys (without key_hash for security).

        Returns:
            List of dicts with keys: id, name, key_prefix, active, created_at, last_used_at.
        """
        with self._pool.checkout() as conn:
            return self._pool.fetch_all(
                conn,
                "SELECT id, name, key_prefix, active, created_at, last_used_at "
                "FROM api_keys ORDER BY id",
            )

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

    def set_user_password(self, username: str, password_hash: str) -> None:
        """Insert or update password_hash for username.

        Args:
            username: The web UI username.
            password_hash: bcrypt hash of the new password.
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
                    "INSERT INTO webui_users (username, password_hash) VALUES (%s, %s)",
                    (username, password_hash),
                )
