# SPDX-License-Identifier: AGPL-3.0-or-later
"""CRUD for indexer_jobs table — track indexer subprocess lifecycle."""
from datetime import datetime

from psycopg2 import sql as pgsql

from src.db.pg import PgPool

_VALID_STATUSES = {"queued", "running", "done", "error"}

_ALLOWED_JOB_COLUMNS = ("status", "pid", "started_at", "finished_at", "error_msg")

_DATETIME_KEYS = ("started_at", "finished_at", "created_at")


def _serialize_datetimes(row: dict) -> dict:
    """Convert datetime fields to str for JSON serialization friendliness."""
    for key in _DATETIME_KEYS:
        if row.get(key) is not None:
            row[key] = str(row[key])
    return row


class JobStore:
    """Encapsulates all CRUD for the indexer_jobs table."""

    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    def create_job(self, profile_name: str) -> int:
        """Create a new job in 'queued' status. Return job_id.

        Args:
            profile_name: Profile being indexed (e.g. 'odoo17').

        Returns:
            Integer id of the new indexer_jobs row.
        """
        with self._pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO indexer_jobs (profile_name) VALUES (%s) RETURNING id",
                    (profile_name,),
                )
                row_id = cur.fetchone()[0]
            conn.commit()
        return row_id

    def get_job(self, job_id: int) -> dict | None:
        """Fetch one job. Return dict with all columns OR None if not found.

        Returns dict with keys: id, profile_name, status, pid, started_at,
        finished_at, error_msg, created_at. Datetime values are ISO strings
        (str(value)) for JSON serialization friendliness.
        """
        with self._pool.checkout() as conn:
            row = self._pool.fetch_one(
                conn, "SELECT * FROM indexer_jobs WHERE id = %s", (job_id,)
            )
        if row is None:
            return None
        return _serialize_datetimes(row)

    def update_job(
        self,
        job_id: int,
        *,
        status: str | None = None,
        pid: int | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        error_msg: str | None = None,
    ) -> None:
        """Partial update — only non-None fields are written.

        Raises ValueError if job_id does not exist (rowcount == 0 after UPDATE).
        Raises ValueError if status is not a valid value.
        """
        if status is not None and status not in _VALID_STATUSES:
            raise ValueError(
                f"Invalid status {status!r}. Must be one of: {sorted(_VALID_STATUSES)}"
            )

        col_values: list[tuple[str, object]] = []
        if status is not None:
            col_values.append(("status", status))
        if pid is not None:
            col_values.append(("pid", pid))
        if started_at is not None:
            col_values.append(("started_at", started_at))
        if finished_at is not None:
            col_values.append(("finished_at", finished_at))
        if error_msg is not None:
            col_values.append(("error_msg", error_msg))

        if not col_values:
            return  # nothing to update

        # Build safe SQL using psycopg2.sql.Identifier (escapes column names properly)
        col_names = [cv[0] for cv in col_values]
        values = [cv[1] for cv in col_values] + [job_id]

        sql_obj = pgsql.SQL("UPDATE indexer_jobs SET {fields} WHERE id = %s").format(
            fields=pgsql.SQL(", ").join(
                pgsql.SQL("{col} = %s").format(col=pgsql.Identifier(c))
                for c in col_names
            )
        )

        with self._pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(sql_obj, values)
                if cur.rowcount == 0:
                    conn.rollback()
                    raise ValueError(f"Job {job_id} not found")
            conn.commit()

    def list_running_jobs(self) -> list[dict]:
        """All jobs with status='running'. Empty list if none.

        Returns dicts shaped like get_job().
        """
        with self._pool.checkout() as conn:
            rows = self._pool.fetch_all(
                conn,
                "SELECT * FROM indexer_jobs WHERE status = 'running' ORDER BY created_at ASC",
            )
        return [_serialize_datetimes(entry) for entry in rows]

    def get_last_job(self, profile_name: str) -> dict | None:
        """Most recent job for a profile (ORDER BY created_at DESC LIMIT 1)."""
        with self._pool.checkout() as conn:
            row = self._pool.fetch_one(
                conn,
                "SELECT * FROM indexer_jobs"
                " WHERE profile_name = %s ORDER BY created_at DESC LIMIT 1",
                (profile_name,),
            )
        if row is None:
            return None
        return _serialize_datetimes(row)

    def mark_dead_jobs(self) -> int:
        """Mark running/queued jobs whose PID is no longer alive as 'error'.

        Called on Web UI startup to clean up jobs left over from crashed subprocesses.
        Returns the number of jobs marked as error.
        """
        import os  # noqa: PLC0415
        from datetime import UTC  # noqa: PLC0415
        from datetime import datetime as dt

        with self._pool.checkout() as conn:
            rows = self._pool.fetch_all(
                conn,
                "SELECT * FROM indexer_jobs"
                " WHERE status IN ('running', 'queued') ORDER BY created_at ASC",
            )
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
                self.update_job(
                    row["id"],
                    status="error",
                    finished_at=dt.now(UTC),
                    error_msg=f"Process died unexpectedly (PID {pid} not found at server startup)",
                )
                count += 1
            except PermissionError:
                # Process exists but different UID — leave it alone
                pass
        return count

    def list_all_jobs(self) -> list[dict]:
        """All jobs ordered by created_at DESC.

        Returns dicts shaped like get_job().
        """
        with self._pool.checkout() as conn:
            rows = self._pool.fetch_all(
                conn,
                "SELECT * FROM indexer_jobs ORDER BY created_at DESC",
            )
        return [_serialize_datetimes(entry) for entry in rows]
