"""CRUD for api_keys, ssh_key_pairs, usage_log tables."""
import hashlib
import secrets


def create_api_key(conn, name: str) -> tuple[str, str, int]:
    """Create API key. Return (raw_key, key_prefix, id). raw_key shown once.

    Args:
        conn: PostgreSQL connection (autocommit mode recommended).
        name: Descriptive name for the key (e.g. 'claude-code-laptop').

    Returns:
        Tuple of (raw_key_string, key_prefix_8chars, key_id).
        raw_key is ephemeral and should be displayed to user exactly once.
    """
    raw = "osm_" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    key_prefix = raw[:8]
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_keys (name, key_hash, key_prefix) VALUES (%s, %s, %s) RETURNING id",
            (name, key_hash, key_prefix),
        )
        row = cur.fetchone()
    if not conn.autocommit:
        conn.commit()
    return raw, key_prefix, row[0]


def verify_api_key(conn, raw_key: str) -> int | None:
    """Return api_key_id if active + valid. Update last_used_at. Return None if invalid.

    Args:
        conn: PostgreSQL connection.
        raw_key: The full API key string (starts with 'osm_').

    Returns:
        Integer key_id if found and active, None otherwise.
        Side effect: updates last_used_at timestamp on successful verification.
    """
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM api_keys WHERE key_hash = %s AND active = TRUE",
            (key_hash,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    key_id = row[0]
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE api_keys SET last_used_at = NOW() WHERE id = %s",
            (key_id,),
        )
    if not conn.autocommit:
        conn.commit()
    return key_id


def list_api_keys(conn) -> list[dict]:
    """List all API keys (without key_hash for security).

    Args:
        conn: PostgreSQL connection.

    Returns:
        List of dicts with keys: id, name, key_prefix, active, created_at, last_used_at.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, key_prefix, active, created_at, last_used_at "
            "FROM api_keys ORDER BY id"
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def deactivate_api_key(conn, key_id: int) -> None:
    """Deactivate an API key by id.

    Args:
        conn: PostgreSQL connection.
        key_id: The api_key id to deactivate.
    """
    with conn.cursor() as cur:
        cur.execute("UPDATE api_keys SET active = FALSE WHERE id = %s", (key_id,))
    if not conn.autocommit:
        conn.commit()


def log_usage(conn, api_key_id: int | None, tool_name: str, response_ms: int) -> None:
    """Log tool usage. Fire-and-forget — caller wraps in asyncio.create_task where needed.

    Args:
        conn: PostgreSQL connection.
        api_key_id: Integer key_id or None for anonymous requests.
        tool_name: Name of the MCP tool invoked (e.g. 'resolve_model').
        response_ms: Response time in milliseconds.

    Note:
        Swallows exceptions silently (best-effort logging).
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO usage_log (api_key_id, tool_name, response_ms) VALUES (%s, %s, %s)",
                (api_key_id, tool_name, response_ms),
            )
        if not conn.autocommit:
            conn.commit()
    except Exception:
        pass  # best-effort


def list_ssh_keys(conn) -> list[dict]:
    """List SSH key pairs (without private key for security).

    Args:
        conn: PostgreSQL connection.

    Returns:
        List of dicts with keys: id, name, public_key, key_version, created_at.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, public_key, key_version, created_at FROM ssh_key_pairs ORDER BY id"
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def create_feedback(
    conn,
    *,
    pattern_node_id: str,
    api_key_id: int | None,
    rating: str,
    comment: str | None = None,
) -> int:
    """Store a thumbs-up/down rating for a PatternExample node.

    Args:
        conn: PostgreSQL connection.
        pattern_node_id: Neo4j node id or pattern_id string
            (e.g. 'python__write-read-before-super').
        api_key_id: Authenticated API key id (or None for anonymous).
        rating: 'up' or 'down'.
        comment: Optional free-text comment from the user.

    Returns:
        Integer id of the newly created pattern_feedback row.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO pattern_feedback (pattern_node_id, api_key_id, rating, comment) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (pattern_node_id, api_key_id, rating, comment),
        )
        row = cur.fetchone()
        if not conn.autocommit:
            conn.commit()
        return row[0]


def list_feedback(conn, pattern_node_id: str) -> list[dict]:
    """Return all feedback entries for a given pattern node, newest first.

    Args:
        conn: PostgreSQL connection.
        pattern_node_id: The pattern id to filter on.

    Returns:
        List of dicts with keys: id, api_key_id, rating, comment, created_at.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, api_key_id, rating, comment, created_at "
            "FROM pattern_feedback "
            "WHERE pattern_node_id = %s "
            "ORDER BY created_at DESC",
            (pattern_node_id,),
        )
        rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "api_key_id": r[1],
                "rating": r[2],
                "comment": r[3],
                "created_at": str(r[4]),
            }
            for r in rows
        ]


def save_ssh_key(
    conn, name: str, public_key: str, private_key_encrypted: str, key_version: int = 1
) -> int:
    """Save SSH key pair. Return id.

    Args:
        conn: PostgreSQL connection.
        name: Descriptive name for this key pair.
        public_key: The full public key string (e.g. 'ssh-ed25519 AAAA...').
        private_key_encrypted: Private key encrypted with a master key (not stored raw).
        key_version: Version of the encryption key used (default 1).

    Returns:
        Integer id of the newly created ssh_key_pair row.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ssh_key_pairs (name, public_key, private_key_encrypted, key_version)"
            " VALUES (%s, %s, %s, %s) RETURNING id",
            (name, public_key, private_key_encrypted, key_version),
        )
        row = cur.fetchone()
    if not conn.autocommit:
        conn.commit()
    return row[0]
