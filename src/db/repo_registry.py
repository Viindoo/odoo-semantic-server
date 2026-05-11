# src/db/repo_registry.py
"""CRUD for profiles + repos in PostgreSQL."""
import psycopg2
import psycopg2.errors
from psycopg2.extras import RealDictCursor

from src.db._types import PgConn


def add_profile(conn: PgConn, name: str, odoo_version: str, description: str = "") -> int:
    """Insert a new profile. Raises ValueError if name already exists."""
    with conn.cursor() as cur:
        try:
            cur.execute(
                "INSERT INTO profiles (name, odoo_version, description) "
                "VALUES (%s, %s, %s) RETURNING id",
                (name, odoo_version, description),
            )
            return cur.fetchone()[0]
        except psycopg2.errors.UniqueViolation as e:
            raise ValueError(f"Profile '{name}' already exists") from e


def list_profiles(conn: PgConn) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM profiles ORDER BY id")
        return [dict(r) for r in cur.fetchall()]


def add_repo(
    conn: PgConn,
    profile_id: int,
    url: str,
    branch: str,
    local_path: str,
    *,
    ssh_key_id: int | None = None,
    clone_status: str = "manual",
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO repos (profile_id, url, branch, local_path, ssh_key_id, clone_status) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (profile_id, url, branch, local_path, ssh_key_id, clone_status),
        )
        return cur.fetchone()[0]


def list_repos(conn: PgConn) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT r.*, p.name AS profile_name, p.odoo_version
            FROM repos r LEFT JOIN profiles p ON r.profile_id = p.id
            ORDER BY r.id
        """)
        return [dict(r) for r in cur.fetchall()]


def get_repos_for_profile(conn: PgConn, profile_name: str) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT r.*, p.odoo_version
            FROM repos r JOIN profiles p ON r.profile_id = p.id
            WHERE p.name = %s ORDER BY r.id
        """, (profile_name,))
        return [dict(r) for r in cur.fetchall()]


def update_repo_status(
    conn: PgConn, repo_id: int, status: str, error_msg: str | None = None
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE repos SET status = %s, error_msg = %s, "
            "last_indexed_at = CASE WHEN %s = 'indexed' THEN NOW() ELSE last_indexed_at END "
            "WHERE id = %s",
            (status, error_msg, status, repo_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"repo id={repo_id} not found")


def get_repo_head_sha(conn: PgConn, repo_id: int) -> str | None:
    """Return head_sha for repo_id, or None if NULL or repo doesn't exist."""
    with conn.cursor() as cur:
        cur.execute("SELECT head_sha FROM repos WHERE id = %s", (repo_id,))
        row = cur.fetchone()
        return row[0] if row is not None else None


def update_repo_head_sha(conn: PgConn, repo_id: int, head_sha: str) -> None:
    """Update head_sha and bump last_indexed_at."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE repos SET head_sha = %s, last_indexed_at = NOW() WHERE id = %s",
            (head_sha, repo_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"repo id={repo_id} not found")


def set_clone_status(
    conn: PgConn, repo_id: int, status: str, error_msg: str | None = None
) -> None:
    """Update clone_status and optionally clone_error_msg.

    Status enum: 'manual', 'pending', 'cloned', 'error'.

    Note: cloner errors are stored in `clone_error_msg` (NOT `error_msg`).
    `error_msg` is reserved for indexer errors (written by `update_repo_status`).
    Keeping them separate prevents the cloner success path from clearing a prior
    indexer error and vice versa.
    """
    valid_statuses = ("manual", "pending", "cloned", "error")
    if status not in valid_statuses:
        raise ValueError(f"Invalid clone_status: {status}. Must be one of {valid_statuses}")

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE repos SET clone_status = %s, clone_error_msg = %s WHERE id = %s",
            (status, error_msg, repo_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"repo id={repo_id} not found")


def get_repos_by_clone_status(
    conn: PgConn, profile_name: str, status: str
) -> list[dict]:
    """Return all repos for a profile matching the given clone_status."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT r.*, p.odoo_version
            FROM repos r
            JOIN profiles p ON r.profile_id = p.id
            WHERE p.name = %s AND r.clone_status = %s
            ORDER BY r.id
            """,
            (profile_name, status),
        )
        return [dict(r) for r in cur.fetchall()]


def get_repo_by_id(conn: PgConn, repo_id: int) -> dict | None:
    """Return a single repo row joined with its profile, or None if not found."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT r.*, p.name AS profile_name, p.odoo_version
            FROM repos r LEFT JOIN profiles p ON r.profile_id = p.id
            WHERE r.id = %s
            """,
            (repo_id,),
        )
        row = cur.fetchone()
        return dict(row) if row is not None else None


def get_repo_ids_by_local_path_basenames(
    conn: PgConn, basenames: list[str]
) -> list[int]:
    """Return repo IDs whose local_path basename matches any entry in *basenames*.

    The Neo4j Module.repo property equals ``Path(local_path).name`` (the
    directory basename of the checkout).  This function maps those basename
    strings back to PostgreSQL ``repos.id`` values so that ``reset_head_sha``
    can null them out.

    Uses ``regexp_replace`` to extract the basename server-side — avoids
    fetching all rows into Python and doing the split there.

    Returns:
        List of repo IDs (may be shorter than basenames if some are not in DB).
    """
    if not basenames:
        return []
    with conn.cursor() as cur:
        # regexp_replace strips everything up to and including the last '/'.
        # E.g. '/home/user/git/odoo_17.0' → 'odoo_17.0'.
        cur.execute(
            """
            SELECT id
            FROM repos
            WHERE regexp_replace(local_path, '^.*/', '') = ANY(%s)
            """,
            (basenames,),
        )
        return [row[0] for row in cur.fetchall()]


def reset_head_sha(conn: PgConn, repo_ids: list[int]) -> int:
    """Bulk-reset head_sha to NULL for given repo IDs.

    Forces those repos to be fully re-indexed on the next indexer run.
    Used by cross-repo dependency propagation (M7 W14): when upstream modules
    change, downstream repos' head_sha is NULLed so they are not skipped.

    Returns:
        Number of rows updated (may be less than len(repo_ids) if some IDs
        do not exist in the table).
    """
    if not repo_ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE repos SET head_sha = NULL WHERE id = ANY(%s)",
            (repo_ids,),
        )
        return cur.rowcount


def update_repo_local_path(conn: PgConn, repo_id: int, local_path: str) -> None:
    """Update local_path for a repo after a successful clone.

    Does NOT touch last_indexed_at — cloning is not indexing. last_indexed_at
    is bumped only by update_repo_head_sha (called at the end of a real index run).
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE repos SET local_path = %s WHERE id = %s",
            (local_path, repo_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"repo id={repo_id} not found")
