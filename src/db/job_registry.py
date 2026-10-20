# SPDX-License-Identifier: AGPL-3.0-or-later
"""CRUD for indexer_jobs table — track indexer subprocess lifecycle."""
import logging
import os
from datetime import UTC, datetime

from psycopg2 import errors as pg_errors
from psycopg2 import sql as pgsql

from src.db.pg import PgPool

_VALID_STATUSES = {"queued", "running", "done", "error"}

_ALLOWED_JOB_COLUMNS = (
    "status", "pid", "started_at", "finished_at", "error_msg",
    "proc_start_ticks", "boot_id",
)

# Added by migration 0004_indexer_jobs_proc_identity (#383 item 4).
_IDENTITY_COLUMNS = ("proc_start_ticks", "boot_id")

_DATETIME_KEYS = ("started_at", "finished_at", "created_at")

_logger = logging.getLogger(__name__)


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

    def start_ticks(self, pid: int) -> int | None:
        """Clock ticks after boot at which process *pid* started
        (/proc/<pid>/stat field 22), None when unknown. Boot-relative, so a
        wall-clock step does not move it."""
        try:
            with open(f"/proc/{int(pid)}/stat", encoding="utf-8", errors="replace") as fh:
                return _parse_starttime(fh.read())
        except (OSError, ValueError):
            return None

    def start_time(self, pid: int) -> float | None:
        """Epoch seconds process *pid* started (:meth:`start_ticks` after
        ``btime``), None when unknown."""
        boot = self.boot_time()
        if boot is None:
            return None
        ticks = self.start_ticks(pid)
        if ticks is None:
            return None
        try:
            return boot + ticks / os.sysconf("SC_CLK_TCK")
        except (OSError, ValueError):
            return None

    def boot_id(self) -> str | None:
        """This boot's random id (/proc/sys/kernel/random/boot_id): it changes
        on every boot, so it tells a reboot apart without any clock."""
        try:
            with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as fh:
                value = fh.read().strip()
        except (OSError, UnicodeDecodeError):
            return None
        return value or None


_PROC = ProcInfo()


def own_process_identity() -> dict:
    """``proc_start_ticks`` + ``boot_id`` of the calling process, for its
    'running' report on its job row (#383 item 4). A value that cannot be read
    (not Linux) is left out, so the row keeps NULL and the 120 s heuristic of
    :func:`job_staleness` applies to it."""
    identity = {
        "proc_start_ticks": _PROC.start_ticks(os.getpid()),
        "boot_id": _PROC.boot_id(),
    }
    return {k: v for k, v in identity.items() if v is not None}


# Legacy rows only (proc_start_ticks or boot_id NULL: written before migration
# 0004, or where /proc could not be read). A job's process starts before the
# job reports 'running' (started_at), so a process that started later - or a
# boot after started_at - means the pid is not the job's. Known limit: /proc
# gives the boot time and a process's start only relative to the CURRENT wall
# clock (btime = now - uptime), while started_at is the wall clock at report
# time; a forward step of the clock after the job started (NTP step, manual
# set) shifts both by the step and would make a live job look reused. The slack
# absorbs such steps up to 120 s (plus btime's whole-second resolution); a
# larger forward step can still expire a live job's row (the run itself
# continues and reports done), and a pid reused (or a reboot) within 120 s of
# the job's start is not detected. Rows that carry the child's own identity
# (#383) are compared exactly instead, see job_staleness.
_START_SLACK_S = 120.0


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
      process. A row whose child recorded its identity with the 'running'
      report (``proc_start_ticks`` and ``boot_id``, #383) is compared
      exactly: another ``boot_id`` (the machine rebooted) or other start ticks
      for the pid (pid reused) - immune to wall-clock steps, and a reuse is
      caught however soon after the job's start. A legacy row (either column
      NULL) keeps the wall-clock heuristic: the machine booted after the job
      started, or that process started after the job did, beyond a 120 s slack
      for wall-clock steps (see ``_START_SLACK_S`` for its limits). Needs
      /proc; elsewhere a live pid is given the benefit of the doubt.
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
    ticks, boot_id = row.get("proc_start_ticks"), row.get("boot_id")
    if ticks is not None and boot_id is not None:
        current_boot = _PROC.boot_id()
        if current_boot is not None and current_boot != str(boot_id):
            return (
                f"PID {pid} is not the job's process: the machine rebooted "
                "after the job started (boot id differs)"
            )
        current_ticks = _PROC.start_ticks(pid)
        if current_ticks is not None and current_ticks != int(ticks):
            return (
                f"PID {pid} is not the job's process: its start ticks differ "
                "from the job's (pid reused)"
            )
        return None
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
    proc_start_ticks: int | None = None,
    boot_id: str | None = None,
) -> None:
    """Partial update of one indexer_jobs row on *conn* - only non-None fields
    are written. ``proc_start_ticks`` / ``boot_id`` are the child's own
    process identity (:func:`own_process_identity`), sent with its 'running'
    report. The caller owns the transaction (the indexer CLI passes its
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
    if proc_start_ticks is not None:
        col_values.append(("proc_start_ticks", proc_start_ticks))
    if boot_id is not None:
        col_values.append(("boot_id", boot_id))

    if not col_values:
        return  # nothing to update

    with conn.cursor() as cur:
        identity = [cv for cv in col_values if cv[0] in _IDENTITY_COLUMNS]
        if not identity:
            _execute_update(cur, job_id, col_values)
            return
        # Code deployed before migration 0004: the identity columns do not
        # exist yet. Still write the rest, or a live run would never report
        # 'running' and its queued row would expire (#383 item 4).
        savepoint = not getattr(conn, "autocommit", True)
        if savepoint:
            cur.execute("SAVEPOINT job_identity")
        try:
            _execute_update(cur, job_id, col_values)
        except pg_errors.UndefinedColumn:
            if savepoint:
                cur.execute("ROLLBACK TO SAVEPOINT job_identity")
            _logger.warning(
                "indexer_jobs has no process identity columns (apply migration "
                "0004_indexer_jobs_proc_identity); job %s updated without them", job_id,
            )
            _execute_update(cur, job_id, [cv for cv in col_values if cv not in identity])
        else:
            if savepoint:
                cur.execute("RELEASE SAVEPOINT job_identity")


def _execute_update(cur, job_id: int, col_values: list[tuple[str, object]]) -> None:
    """UPDATE the given columns of one row (psycopg2.sql.Identifier escapes
    the column names); ValueError when the row does not exist."""
    sql_obj = pgsql.SQL("UPDATE indexer_jobs SET {fields} WHERE id = %s").format(
        fields=pgsql.SQL(", ").join(
            pgsql.SQL("{col} = %s").format(col=pgsql.Identifier(c))
            for c, _ in col_values
        )
    )
    cur.execute(sql_obj, [v for _, v in col_values] + [job_id])
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
        finished_at, error_msg, created_at, proc_start_ticks, boot_id.
        Datetime values are ISO strings (str(value)) for JSON serialization
        friendliness.
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
        proc_start_ticks: int | None = None,
        boot_id: str | None = None,
    ) -> None:
        """Partial update on a pool connection; see :func:`update_job`."""
        with self._pool.checkout() as conn:
            try:
                update_job(
                    conn, job_id, status=status, pid=pid, started_at=started_at,
                    finished_at=finished_at, error_msg=error_msg,
                    proc_start_ticks=proc_start_ticks, boot_id=boot_id,
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
