"""Shared helper for spawning indexer subprocesses with job tracking.

Extracted from src/web_ui/routes/repos.py to be reused by W3-W8 routes
that all need the same pattern: create indexer_jobs row, spawn detached
subprocess with --job-id, return job_id for status polling.
"""
import subprocess
import sys

from src.db import job_registry


def spawn_indexer_subcommand(
    conn,
    subcommand_argv: list[str],
    job_label: str,
) -> int:
    """Create an indexer_jobs row, spawn detached subprocess, return job_id.

    Spawns `python -m src.indexer <argv> --job-id N` as a detached process.

    Args:
        conn: open psycopg2 connection
        subcommand_argv: e.g. ["index-repo", "--profile", "viindoo17", "--full"]
                         The CLI subcommand + its flags (without --job-id, this helper appends it).
        job_label: stored in indexer_jobs.profile_name. Used as the label for status polling.
                   For index-repo use the profile name; for index-core use "core:<version>";
                   for seed-patterns use "patterns"; for index-all use "all".

    Returns:
        The new job_id (also passed as --job-id to the subprocess).
    """
    job_id = job_registry.create_job(conn, job_label)
    argv = [sys.executable, "-m", "src.indexer", *subcommand_argv, "--job-id", str(job_id)]
    subprocess.Popen(argv, start_new_session=True)
    return job_id
