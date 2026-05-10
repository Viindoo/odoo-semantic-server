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
    conn: PgConn, profile_id: int, url: str, branch: str, local_path: str
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO repos (profile_id, url, branch, local_path) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (profile_id, url, branch, local_path),
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
