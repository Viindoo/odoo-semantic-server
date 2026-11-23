# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared helper for spawning indexer subprocesses with job tracking.

Extracted from src/web_ui/routes/repos.py to be reused by W3-W8 routes
that all need the same pattern: create indexer_jobs row, spawn detached
subprocess with --job-id, return job_id for status polling.
"""
import logging
import subprocess
import sys
import threading
from datetime import UTC, datetime

from src.db.pg import job_store
from src.web_ui.helpers import job_logs

_logger = logging.getLogger(__name__)


def spawn_indexer_subcommand(
    subcommand_argv: list[str],
    job_label: str,
) -> int:
    """Create an indexer_jobs row, spawn detached subprocess, return job_id.

    Spawns `python -m src.indexer <argv> [--verbose] --job-id N` as a detached
    process. ``--verbose`` is added when the subcommand declares it, so the
    job log records each step (which repository, fetch, incremental plan,
    per-repo result) and an admin can tell what a running job is doing.
    Subprocess stdout+stderr go to the job log
    (:func:`src.web_ui.helpers.job_logs.job_log_path`), a persistent data
    directory readable on the host and through ``GET /api/jobs/{id}/status``;
    logs older than the retention are deleted first.

    Args:
        subcommand_argv: e.g. ["index-repo", "--profile", "viindoo17", "--full"]
                         The CLI subcommand + its flags (without --job-id, this helper appends it).
        job_label: stored in indexer_jobs.profile_name. Used as the label for status polling.
                   For index-repo use the profile name; for index-core use "core:<version>";
                   for seed-patterns use "patterns"; for index-all use "all".

    Returns:
        The new job_id (also passed as --job-id to the subprocess).

    Raises:
        ValueError: the subcommand does not declare ``--job-id`` (no job row
            is created and nothing is spawned).
        Exception: the log file or the spawn failed; the job row is marked
            'error' with the reason first, then the error is re-raised.
    """
    # The child reports running / done / error through --job-id; a subcommand
    # that does not declare it would die on argparse exit 2 and leave its job
    # queued forever (#381 F2). Refuse before any job row exists.
    from src.indexer.__main__ import subcommand_accepts  # noqa: PLC0415

    subcommand = subcommand_argv[0] if subcommand_argv else ""
    if not subcommand_accepts(subcommand, "--job-id"):
        raise ValueError(
            f"indexer subcommand {subcommand!r} does not accept --job-id: "
            "a Web UI job could never leave 'queued'"
        )
    job_id = job_store().create_job(job_label)
    argv = [sys.executable, "-m", "src.indexer", *subcommand_argv]
    if "--verbose" not in argv and subcommand_accepts(subcommand, "--verbose"):
        argv.append("--verbose")
    argv += ["--job-id", str(job_id)]

    job_logs.prune_job_logs()
    # Popen dup2()s the fd into the child — parent can close its copy right after.
    try:
        with job_logs.open_job_log(job_id) as log_file:
            proc = subprocess.Popen(
                argv,
                start_new_session=True,
                stdout=log_file,
                stderr=log_file,
            )
    except Exception as exc:
        # Nothing started: the job must not stay 'queued' (#381).
        try:
            job_store().update_job(
                job_id,
                status="error",
                finished_at=datetime.now(UTC),
                error_msg=f"Spawn failed: {type(exc).__name__}: {exc}"[:1000],
            )
        except Exception as update_exc:  # noqa: BLE001 - re-raise the spawn error
            _logger.warning(
                "index job %s: recording spawn failure failed: %s", job_id, update_exc,
            )
        raise
    # Record the child's pid now: a child that dies before its own 'running'
    # report still leaves a pid the start-up sweep can check (#381 F1).
    try:
        job_store().update_job(job_id, pid=proc.pid)
    except Exception as exc:  # noqa: BLE001 - the run itself is started
        _logger.warning("index job %s: recording pid %s failed: %s", job_id, proc.pid, exc)
    # Reap the child when it exits so it doesn't linger as a zombie.
    # Without this, the web server (parent) never calls wait() and the process
    # stays in Z (zombie) state indefinitely after the indexer finishes.
    threading.Thread(target=proc.wait, daemon=True).start()
    return job_id


def spawn_cloner(repo_id: int) -> None:
    """Spawn ``python -m src.cloner --repo-id N`` detached, its stdout + stderr
    in the repository's clone log
    (:func:`src.web_ui.helpers.job_logs.clone_log_path`; same directory,
    permissions and retention as the job logs, readable by admins through
    ``GET /api/repos/repos/{id}/clone-status``). The cloner records its own
    outcome on the repo row (``clone_status`` / ``clone_error_msg``).

    Raises whatever opening the log or the spawn raised; nothing is started then.
    """
    job_logs.prune_job_logs()
    with job_logs.open_clone_log(repo_id) as log_file:
        proc = subprocess.Popen(
            [sys.executable, "-m", "src.cloner", "--repo-id", str(repo_id)],
            start_new_session=True,
            stdout=log_file,
            stderr=log_file,
        )
    # Reap the child when it exits so it does not linger as a zombie.
    threading.Thread(target=proc.wait, daemon=True).start()
