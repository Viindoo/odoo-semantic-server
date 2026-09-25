# SPDX-License-Identifier: AGPL-3.0-or-later
"""CRUD for indexer_jobs table — track indexer subprocess lifecycle."""
import os
from datetime import UTC, datetime

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


def _parse_starttime(raw: str) -> int | None:
    """Field 22 (``starttime``, clock ticks after boot) of a /proc/<pid>/stat
    line, None when it cannot be read. ``comm`` (field 2) may hold spaces and
    parentheses, so the fields are split after the LAST ')'."""
    try:
        fields = raw[raw.rindex(")") + 2:].split()
        return int(fields[22 - 3])
    except (ValueError, IndexError):
        return None


class ProcInfo:
    """Linux ``/proc`` reader for process identity; every answer is None
    where it cannot be read (other OS, hidepid, race with exit)."""

    def boot_time(self) -> float | None:
        """Epoch seconds of the last boot (``btime`` in /proc/stat)."""
        try:
            with open("/proc/stat", encoding="ascii") as fh:
                for line in fh:
                    if line.startswith("btime "):
                        return float(line.split()[1])
        except (OSError, ValueError, IndexError):
            return None
        return None

    def start_time(self, pid: int) -> float | None:
        """Epoch seconds process *pid* started (/proc/<pid>/stat field 22
        in clock ticks after boot), None when unknown."""
        boot = self.boot_time()
        if boot is None:
            return None
        try:
            with open(f"/proc/{int(pid)}/stat", encoding="utf-8", errors="replace") as fh:
                ticks = _parse_starttime(fh.read())
            if ticks is None:
                return None
            return boot + ticks / os.sysconf("SC_CLK_TCK")
        except (OSError, ValueError):
            return None


_PROC = ProcInfo()

# A job's process starts before the job reports 'running' (started_at); allow
# for btime's whole-second resolution and clock-tick rounding.
_START_SLACK_S = 5.0


def _as_datetime(value) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def job_staleness(row: dict, *, now: datetime | None = None) -> str | None:
    """Why the unfinished (queued / running) job *row*'s process is gone, or
    None while it may still be alive. The one rule of the start-up sweep
    (:meth:`JobStore.mark_dead_jobs`) and the admin reset route (#381).

    * queued, never started (``started_at`` NULL), older than
      ``INDEXER_JOB_QUEUED_TTL_SECONDS``: a live child reports 'running'
      within seconds, whatever pid the row holds;
    * its pid does not exist;
    * its pid exists (or belongs to another user) but is not the job's
      process: the machine booted after the job started, or that process
      started after the job did (pid reused). Needs /proc; elsewhere a live
      pid is given the benefit of the doubt.
    """
    from src import constants  # noqa: PLC0415

    now = now or datetime.now(UTC)
    ttl = constants.INDEXER_JOB_QUEUED_TTL_SECONDS
    pid = row.get("pid")
    started_at = _as_datetime(row.get("started_at"))
    created_at = _as_datetime(row.get("created_at"))
    age = (now - created_at).total_seconds() if created_at else None
    if row.get("status") == "queued" and started_at is None and age is not None and age > ttl:
        return (
            f"Job stayed queued for {int(age)}s (limit {int(ttl)}s, "
            f"pid {pid if pid is not None else 'none'}): the indexer process "
            "never started or died before reporting"
        )
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return f"Process died unexpectedly (PID {pid} not found)"
    except PermissionError:
        pass  # exists, another user's: identity decides below
    if started_at is None:
        return None
    ref = started_at.timestamp()
    boot = _PROC.boot_time()
    if boot is not None and boot > ref + _START_SLACK_S:
        return f"PID {pid} is not the job's process: the machine booted after the job started"
    begun = _PROC.start_time(pid)
    if begun is not None and begun > ref + _START_SLACK_S:
        return f"PID {pid} is not the job's process: it started after the job did (pid reused)"
    return None


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

        Called on Web UI startup to clean up jobs left over from crashed
        subprocesses; the rule is :func:`job_staleness` (shared with the
        admin reset route). Returns the number of jobs marked as error.
        """
        with self._pool.checkout() as conn:
            rows = self._pool.fetch_all(
                conn,
                "SELECT * FROM indexer_jobs"
                " WHERE status IN ('running', 'queued') ORDER BY created_at ASC",
            )
        now = datetime.now(UTC)
        count = 0
        for row in rows:
            reason = job_staleness(row, now=now)
            if reason is None:
                continue
            self.update_job(
                row["id"], status="error", finished_at=now,
                error_msg=f"{reason} (found at Web UI startup)",
            )
            count += 1
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
