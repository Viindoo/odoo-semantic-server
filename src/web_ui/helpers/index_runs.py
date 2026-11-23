# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which index run, if any, is indexing a profile right now, and whether it
covers a given repository.

An index run of profile P holds P's advisory lock (``pipeline._indexer_lock``),
whoever started it: a Web UI job, the CLI or the nightly reindex unit. A Web
UI job is an ``indexer_jobs`` row before its process takes the lock. So P is
being indexed while its lock is held, or while a live job row (``queued`` /
``running`` after the sweep, :meth:`JobStore.mark_dead_jobs`) names P or
``all``.

A run reads P's repository list once, on a connection it opened before the
read, and does not see a repository added later. Coverage is therefore
decided conservatively: a repository is covered when it was created no later
than the start of the lock holder's database session (``pg_stat_activity``),
or no later than the job's own ``running`` report; a job that has not started
yet covers every repository that exists now. When neither instant can be
read, the repository is reported as not covered.

The lock is read with ``src.db.pg.advisory_lock_holders`` (``pg_locks``, no
lock is taken), the same read as ``pipeline.indexer_is_running``, so this check
never makes a starting index run find its lock busy.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

ALL_PROFILES_LABEL = "all"


@dataclass(frozen=True)
class ProfileRun:
    """The index run in progress for one profile."""

    job_id: int | None
    """The Web UI job, None for a run started outside the Web UI."""
    job_label: str | None
    """The job's ``profile_name``: the profile itself, or ``all``."""
    started_at: datetime | None
    repos_listed_by: datetime | None
    """Every repository created at or before this instant is in the run."""
    lists_repos_later: bool = False
    """The run has not read the repository list yet: it covers every
    repository that exists now."""

    @property
    def external(self) -> bool:
        return self.job_id is None

    def covers(self, repo_created_at: datetime | None) -> bool:
        if self.lists_repos_later:
            return True
        if self.repos_listed_by is None or repo_created_at is None:
            return False
        return repo_created_at <= self.repos_listed_by

    def describe(self, *, show_job: bool = True) -> str:
        """``job #12``, ``job #12 (all profiles)``, or the outside-run phrase;
        *show_job* False hides the number of an ``all`` job (admin-only)."""
        if self.external:
            return "a run started outside the Web UI (CLI or scheduled reindex)"
        if self.job_label != ALL_PROFILES_LABEL:
            return f"job #{self.job_id}"
        return f"job #{self.job_id} (all profiles)" if show_job else "an index run of all profiles"

    def as_json(self, *, covers_repo: bool) -> dict:
        return {
            "job_id": self.job_id,
            "job_label": self.job_label,
            "started_at": str(self.started_at) if self.started_at is not None else None,
            "covers_repo": covers_repo,
        }


def _newest_job(jobs: list[dict], labels: tuple[str, ...], *, running_only: bool) -> dict | None:
    """The newest live job whose label is the first of *labels* that has one."""
    for label in labels:
        for job in jobs:  # newest first
            if job.get("profile_name") != label:
                continue
            if running_only and job.get("status") != "running":
                continue
            return job
    return None


def profile_runs(conn, profile_names, unfinished_jobs: list[dict]) -> dict[str, ProfileRun]:
    """The run in progress for each of *profile_names* (absent: none).

    *unfinished_jobs* are the live job rows, newest first, after the sweep
    (:meth:`JobStore.list_unfinished_jobs`); *conn* is an autocommit
    connection used to read ``pg_locks``."""
    from src.db.pg import advisory_lock_holders  # noqa: PLC0415
    from src.indexer.pipeline import _profile_lock_id  # noqa: PLC0415

    names = list(dict.fromkeys(profile_names))
    lock_of = {name: _profile_lock_id(name) for name in names}
    holders = advisory_lock_holders(conn, lock_of.values())
    runs: dict[str, ProfileRun] = {}
    for name in names:
        labels = (name, ALL_PROFILES_LABEL)
        if lock_of[name] in holders:
            session_start = holders[lock_of[name]]
            job = _newest_job(unfinished_jobs, labels, running_only=True)
            job_start = job.get("started_at") if job else None
            runs[name] = ProfileRun(
                job_id=job["id"] if job else None,
                job_label=job["profile_name"] if job else None,
                started_at=job_start or session_start,
                repos_listed_by=session_start or job_start,
            )
            continue
        job = _newest_job(unfinished_jobs, labels, running_only=False)
        if job is None:
            continue
        started = job.get("started_at")
        runs[name] = ProfileRun(
            job_id=job["id"],
            job_label=job["profile_name"],
            started_at=started,
            repos_listed_by=started,
            # Not started yet: the child reads the repository list after now.
            lists_repos_later=job.get("status") == "queued" or started is None,
        )
    return runs


def repo_created_at(conn, repo_ids) -> dict[int, datetime]:
    """``repos.created_at`` as an aware instant, per repo id.

    The column is ``timestamp`` written by ``DEFAULT NOW()``; the cast reads it
    in this session's TimeZone, which is the server default like the inserting
    session's."""
    ids = sorted({int(i) for i in repo_ids})
    if not ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, created_at::timestamptz FROM repos WHERE id = ANY(%s)", (ids,),
        )
        return {int(rid): created for rid, created in cur.fetchall() if created is not None}


def current_profile_runs(profile_names, repo_ids=(), *, found: str):
    """Sweep dead jobs, then return ``(runs, created)``: the run in progress
    per profile (:func:`profile_runs`) and the creation instant per repo id
    (:func:`repo_created_at`). *found* is the sweep's location text."""
    from src.db.pg import get_pool, job_store  # noqa: PLC0415

    store = job_store()
    store.mark_dead_jobs(found=found)
    jobs = store.list_unfinished_jobs()
    with get_pool().checkout() as conn:
        runs = profile_runs(conn, profile_names, jobs)
        created = repo_created_at(conn, repo_ids) if runs else {}
    return runs, created
