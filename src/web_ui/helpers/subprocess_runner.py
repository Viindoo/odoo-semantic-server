# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared helper for spawning indexer subprocesses with job tracking.

Extracted from src/web_ui/routes/repos.py to be reused by W3-W8 routes
that all need the same pattern: create indexer_jobs row, spawn detached
subprocess with --job-id, return job_id for status polling.
"""
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from src.db.pg import job_store


def spawn_indexer_subcommand(
    subcommand_argv: list[str],
    job_label: str,
) -> int:
    """Create an indexer_jobs row, spawn detached subprocess, return job_id.

    Spawns `python -m src.indexer <argv> --job-id N` as a detached process.
    Subprocess stdout+stderr are redirected to /tmp/osm-job-{job_id}.log so
    indexer output is not silently lost.

    Args:
        subcommand_argv: e.g. ["index-repo", "--profile", "viindoo17", "--full"]
                         The CLI subcommand + its flags (without --job-id, this helper appends it).
        job_label: stored in indexer_jobs.profile_name. Used as the label for status polling.
                   For index-repo use the profile name; for index-core use "core:<version>";
                   for seed-patterns use "patterns"; for index-all use "all".

    Returns:
        The new job_id (also passed as --job-id to the subprocess).
    """
    job_id = job_store().create_job(job_label)
    argv = [sys.executable, "-m", "src.indexer", *subcommand_argv, "--job-id", str(job_id)]

    # Capture subprocess output to /tmp/osm-job-{job_id}.log.
    # Popen dup2()s the fd into the child — parent can close its copy right after.
    log_path = Path(tempfile.gettempdir()) / f"osm-job-{job_id}.log"
    with open(log_path, "w") as log_file:
        proc = subprocess.Popen(
            argv,
            start_new_session=True,
            stdout=log_file,
            stderr=log_file,
        )
    # Reap the child when it exits so it doesn't linger as a zombie.
    # Without this, the web server (parent) never calls wait() and the process
    # stays in Z (zombie) state indefinitely after the indexer finishes.
    threading.Thread(target=proc.wait, daemon=True).start()
    return job_id
