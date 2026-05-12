"""CRUD for indexer_jobs table — track indexer subprocess lifecycle."""
from datetime import datetime

import psycopg2.extras

from src.db._types import PgConn

_VALID_STATUSES = {"queued", "running", "done", "error"}


def create_job(conn: PgConn, profile_name: str) -> int:
    """Create a new job in 'queued' status. Return job_id.

    Args:
        conn: PostgreSQL connection.
        profile_name: Profile being indexed (e.g. 'odoo17').

    Returns:
        Integer id of the new indexer_jobs row.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO indexer_jobs (profile_name) VALUES (%s) RETURNING id",
            (profile_name,),
        )
        row = cur.fetchone()
    if not conn.autocommit:
        conn.commit()
    return row[0]


def get_job(conn: PgConn, job_id: int) -> dict | None:
    """Fetch one job. Return dict with all columns OR None if not found.

    Returns dict with keys: id, profile_name, status, pid, started_at,
    finished_at, error_msg, created_at. Datetime values are ISO strings
    (str(value)) for JSON serialization friendliness.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM indexer_jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
    if row is None:
        return None
    result = dict(row)
    for key in ("started_at", "finished_at", "created_at"):
        if result[key] is not None:
            result[key] = str(result[key])
    return result


def update_job(
    conn: PgConn,
    job_id: int,
    *,
    status: str | None = None,
    pid: int | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    error_msg: str | None = None,
) -> None:
    """Partial update — only non-None fields are written.

    Raises ValueError if job_id does not exist (cur.rowcount == 0 after UPDATE).
    Raises ValueError if status is not a valid value.
    """
    if status is not None and status not in _VALID_STATUSES:
        raise ValueError(
            f"Invalid status {status!r}. Must be one of: {sorted(_VALID_STATUSES)}"
        )

    fields = []
    values = []

    if status is not None:
        fields.append("status = %s")
        values.append(status)
    if pid is not None:
        fields.append("pid = %s")
        values.append(pid)
    if started_at is not None:
        fields.append("started_at = %s")
        values.append(started_at)
    if finished_at is not None:
        fields.append("finished_at = %s")
        values.append(finished_at)
    if error_msg is not None:
        fields.append("error_msg = %s")
        values.append(error_msg)

    if not fields:
        # Nothing to update — no-op
        return

    values.append(job_id)
    sql = f"UPDATE indexer_jobs SET {', '.join(fields)} WHERE id = %s"

    with conn.cursor() as cur:
        cur.execute(sql, values)
        if cur.rowcount == 0:
            if not conn.autocommit:
                conn.rollback()
            raise ValueError(f"Job {job_id} not found")
    if not conn.autocommit:
        conn.commit()


def list_running_jobs(conn: PgConn) -> list[dict]:
    """All jobs with status='running'. Empty list if none.

    Returns dicts shaped like get_job().
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM indexer_jobs WHERE status = 'running' ORDER BY created_at ASC"
        )
        rows = cur.fetchall()

    result = []
    for row in rows:
        entry = dict(row)
        for key in ("started_at", "finished_at", "created_at"):
            if entry[key] is not None:
                entry[key] = str(entry[key])
        result.append(entry)
    return result


def get_last_job(conn: PgConn, profile_name: str) -> dict | None:
    """Most recent job for a profile (ORDER BY created_at DESC LIMIT 1)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM indexer_jobs WHERE profile_name = %s ORDER BY created_at DESC LIMIT 1",
            (profile_name,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    result = dict(row)
    for key in ("started_at", "finished_at", "created_at"):
        if result[key] is not None:
            result[key] = str(result[key])
    return result


def mark_dead_jobs(conn: PgConn) -> int:
    """Mark running/queued jobs whose PID is no longer alive as 'error'.

    Called on Web UI startup to clean up jobs left over from crashed subprocesses.
    Returns the number of jobs marked as error.
    """
    import os
    from datetime import UTC, datetime

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM indexer_jobs"
            " WHERE status IN ('running', 'queued') ORDER BY created_at ASC"
        )
        rows = cur.fetchall()

    count = 0
    for row in rows:
        pid = row.get("pid")
        if pid is None:
            continue
        try:
            os.kill(pid, 0)
            # Process exists — leave it alone
        except ProcessLookupError:
            # PID is dead
            update_job(
                conn,
                row["id"],
                status="error",
                finished_at=datetime.now(UTC),
                error_msg=f"Process died unexpectedly (PID {pid} not found at server startup)",
            )
            count += 1
        except PermissionError:
            # Process exists but different UID — leave it alone
            pass
    return count
