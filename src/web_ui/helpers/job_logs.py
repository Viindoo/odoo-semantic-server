# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where the processes the Web UI spawns write their output, and how much of it
is read back.

* an indexer job (``spawn_indexer_subcommand``) -> ``<log dir>/osm-job-<job id>.log``;
* a repository clone (``spawn_cloner``) -> ``<log dir>/osm-clone-<repo id>.log``
  (one per repository, rewritten by its next clone);
* a backup started from the Operations page -> ``<log dir>/osm-backup-<job uuid>.log``
  (streamed to the admin while it runs).

The directory is a persistent data directory, never the temp dir: the shipped
Web UI unit runs with ``PrivateTmp=true``, so a file under ``/tmp`` is
invisible to an operator on the host and gone when the service restarts.

* ``INDEXER_JOB_LOG_DIR`` - the directory; default
  ``~/.local/share/odoo-semantic-mcp/job-logs`` (next to the clones, inside the
  data directory the shipped units list in ``ReadWritePaths``). A custom value
  must be writable by the service, so it must also be in the unit's
  ``ReadWritePaths``.
* ``INDEXER_JOB_LOG_RETENTION_DAYS`` - a log older than this many days is
  deleted when the next job, clone or backup starts (default 30; ``0`` keeps
  every log).

The directory is created ``0700`` and each log ``0600``: a log carries what the
indexer or git prints (repository URLs, server paths, tracebacks), which only
the service user and admins may read (``GET /api/jobs/{id}/status``,
``GET /api/repos/repos/{id}/clone-status``, the backup stream and status).
"""
import logging
import os
import time
import uuid
from pathlib import Path
from typing import IO

_logger = logging.getLogger(__name__)

# The status endpoint returns at most this many bytes from the end of a log
# (whole lines only): enough for the last steps and a traceback, small enough to
# poll every few seconds.
JOB_LOG_TAIL_BYTES = 32 * 1024

_DEFAULT_RETENTION_DAYS = 30.0
# File name per kind of spawned process; {} is the job id, the repo id or the
# backup job's uuid.
_LOG_NAMES = {
    "job": "osm-job-{}.log", "clone": "osm-clone-{}.log", "backup": "osm-backup-{}.log",
}
# Each identifier is normalised to its canonical form (int, or a uuid string),
# so a value from a URL can never name a file outside the directory.
_IDENT = {"job": int, "clone": int, "backup": lambda v: str(uuid.UUID(str(v)))}


def job_log_dir() -> Path:
    """The directory holding the job logs (read at call time)."""
    configured = os.getenv("INDEXER_JOB_LOG_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "share" / "odoo-semantic-mcp" / "job-logs"


def log_path(kind: str, ident: int) -> Path:
    """The log file of *kind* (``job`` / ``clone`` / ``backup``) number
    *ident*, whether or not it exists. ValueError when *ident* is not a valid
    identifier of that kind."""
    return job_log_dir() / _LOG_NAMES[kind].format(_IDENT[kind](ident))


def job_log_path(job_id: int) -> Path:
    """The log file of job *job_id* (whether or not it exists)."""
    return log_path("job", job_id)


def clone_log_path(repo_id: int) -> Path:
    """The log of the last clone of repository *repo_id* (whether or not it exists)."""
    return log_path("clone", repo_id)


def _retention_seconds() -> float | None:
    raw = os.getenv("INDEXER_JOB_LOG_RETENTION_DAYS", "").strip()
    try:
        days = float(raw) if raw else _DEFAULT_RETENTION_DAYS
    except ValueError:
        _logger.warning(
            "INDEXER_JOB_LOG_RETENTION_DAYS=%r is not a number; using %s",
            raw, _DEFAULT_RETENTION_DAYS,
        )
        days = _DEFAULT_RETENTION_DAYS
    return days * 86400 if days > 0 else None


def prune_job_logs(*, now: float | None = None) -> int:
    """Delete job and clone logs older than the retention; returns how many.
    Never raises: a log that cannot be deleted is logged and left."""
    keep = _retention_seconds()
    directory = job_log_dir()
    if keep is None or not directory.is_dir():
        return 0
    cutoff = (now if now is not None else time.time()) - keep
    removed = 0
    for path in (p for name in _LOG_NAMES.values() for p in directory.glob(name.format("*"))):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError as exc:
            _logger.warning("job log %s: cannot remove: %s", path, exc)
    return removed


def open_log(kind: str, ident: int) -> IO[bytes]:
    """Create (truncate) the log of *kind* number *ident* for a child's stdout
    and stderr, in a directory only the service user can read. An existing
    file keeps no wider mode than ``0600``."""
    directory = job_log_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = log_path(kind, ident)
    handle = open(  # noqa: SIM115 - the caller closes it once the child has the fd
        path, "wb", opener=lambda name, flags: os.open(name, flags, 0o600),
    )
    os.chmod(path, 0o600)  # a file left by an older build may be wider
    return handle


def open_job_log(job_id: int) -> IO[bytes]:
    """:func:`open_log` for job *job_id*."""
    return open_log("job", job_id)


def open_clone_log(repo_id: int) -> IO[bytes]:
    """:func:`open_log` for the clone of repository *repo_id*."""
    return open_log("clone", repo_id)


def read_log_tail(kind: str, ident: int, max_bytes: int = JOB_LOG_TAIL_BYTES) -> dict:
    """The end of the log of *kind* number *ident*: ``{"path", "tail", "truncated"}``.

    ``tail`` is None when the file does not exist (a log that was pruned or
    was written elsewhere); otherwise at most *max_bytes* bytes, starting on
    a line boundary when the file is longer (``truncated`` True), decoded as
    UTF-8 with undecodable bytes replaced.
    """
    path = log_path(kind, ident)
    result = {"path": str(path), "tail": None, "truncated": False}
    try:
        with open(path, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            start = max(0, size - max_bytes)
            # One byte before the window tells whether it starts a line.
            fh.seek(max(0, start - 1))
            data = fh.read(max_bytes + (1 if start > 0 else 0))
    except FileNotFoundError:
        return result
    except OSError as exc:
        _logger.warning("%s %s: cannot read log %s: %s", kind, ident, path, exc)
        return result
    if start > 0:
        # Drop the partial first line (everything up to the first newline).
        newline = data.find(b"\n")
        data = data[newline + 1:] if newline != -1 else b""
        result["truncated"] = True
    result["tail"] = data.decode("utf-8", errors="replace")
    return result


def read_job_log_tail(job_id: int, max_bytes: int = JOB_LOG_TAIL_BYTES) -> dict:
    """:func:`read_log_tail` of job *job_id*."""
    return read_log_tail("job", job_id, max_bytes)


def read_clone_log_tail(repo_id: int, max_bytes: int = JOB_LOG_TAIL_BYTES) -> dict:
    """:func:`read_log_tail` of the last clone of repository *repo_id*."""
    return read_log_tail("clone", repo_id, max_bytes)
