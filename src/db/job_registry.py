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


def update_job(
    conn,
    job_id: int,
    *,
    status: str | None = None,
    pid: int | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    error_msg: str | None = None,
) -> None:
    """Partial update of one indexer_jobs row on *conn* - only non-None fields
    are written. The caller owns the transaction (the indexer CLI passes its
    autocommit connection; :meth:`JobStore.update_job` commits a pool one).

    Raises ValueError if job_id does not exist or status is not a valid value.
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
    with conn.cursor() as cur:
        cur.execute(sql_obj, values)
        if cur.rowcount == 0:
            raise ValueError(f"Job {job_id} not found")


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
        """Partial update on a pool connection; see :func:`update_job`."""
        with self._pool.checkout() as conn:
            try:
                update_job(
                    conn, job_id, status=status, pid=pid, started_at=started_at,
                    finished_at=finished_at, error_msg=error_msg,
                )
            except ValueError:
                conn.rollback()
                raise
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
        """Mark running/queued jobs whose process is gone as 'error'.

        Called on Web UI startup to clean up jobs left over from crashed subprocesses:

        * a job whose PID is no longer alive;
        * a 'queued' job never started (``started_at`` NULL) older than
          ``INDEXER_JOB_QUEUED_TTL_SECONDS``, whatever its PID: its child never
          reported 'running' (a live one does within seconds), and nothing
          else would ever move it out of 'queued' (#381 F1). The PID alone
          cannot tell: it may be none, reused after a reboot, or another
          user's process.

        Returns the number of jobs marked as error.
        """
        import os  # noqa: PLC0415
        from datetime import UTC  # noqa: PLC0415
        from datetime import datetime as dt

        from src import constants  # noqa: PLC0415

        with self._pool.checkout() as conn:
            rows = self._pool.fetch_all(
                conn,
                "SELECT * FROM indexer_jobs"
                " WHERE status IN ('running', 'queued') ORDER BY created_at ASC",
            )
        ttl = constants.INDEXER_JOB_QUEUED_TTL_SECONDS
        now = dt.now(UTC)
        count = 0
        for row in rows:
            pid = row.get("pid")
            created_at = row.get("created_at")
            age = (now - created_at).total_seconds() if created_at else None
            # A live child reports 'running' (with started_at) within seconds.
            # Still queued past the TTL = it never did, whatever the recorded
            # pid says now: none, reused by another process after a reboot, or
            # owned by another user (PermissionError below).
            if (
                row.get("status") == "queued"
                and row.get("started_at") is None
                and age is not None
                and age > ttl
            ):
                self.update_job(
                    row["id"],
                    status="error",
                    finished_at=now,
                    error_msg=(
                        f"Job stayed queued for {int(age)}s (limit {int(ttl)}s, "
                        f"pid {pid if pid is not None else 'none'}): the indexer "
                        "process never started or died before reporting"
                    ),
                )
                count += 1
                continue
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
