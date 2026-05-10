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
    """Update clone_status and optionally error_msg.

    Status enum: 'manual', 'pending', 'cloned', 'error'.
    """
    valid_statuses = ("manual", "pending", "cloned", "error")
    if status not in valid_statuses:
        raise ValueError(f"Invalid clone_status: {status}. Must be one of {valid_statuses}")

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE repos SET clone_status = %s, error_msg = %s WHERE id = %s",
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
