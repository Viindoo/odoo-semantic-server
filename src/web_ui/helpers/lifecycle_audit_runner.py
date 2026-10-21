# SPDX-License-Identifier: AGPL-3.0-or-later
# src/web_ui/helpers/lifecycle_audit_runner.py
"""Bounded, cached dry-run lifecycle audit for the admin lifecycle page (#380).

The page shows what ``lifecycle-audit`` reports (ADR-0056 / ADR-0057: parser
gaps, unproven drops, kept orphans, the shared-module bootstrap backlog, ...).
That report is produced by the audit itself, never re-computed here: the page
runs the SAME command the weekly timer runs, narrowed to one Odoo version::

    python -m src.indexer lifecycle-audit --all --version <v> --json

Why a child process and not ``run_lifecycle_audit`` in a worker thread:

- An audit reads every checkout of the version, git and the graph; it takes
  seconds to minutes. A thread cannot be stopped (asyncio.to_thread is not
  cancellable, the #276 lesson); a child process is killed at
  ``WEBUI_LIFECYCLE_AUDIT_TIMEOUT_SECONDS``.
- A crash of the audit (the neo4j driver can take the interpreter down, #380
  item 7) ends the child, not the Web UI.
- Its memory is returned when it exits.

Why a background run and a status poll, not one long request: nginx gives a
proxied request 60 s by default; the page starts the run (POST) and polls its
state (GET).

Bounds: at most ONE audit runs per Web UI process at a time (a second version
waits: ``start`` answers ``busy``). That is one per deployment: the Web UI is a
single uvicorn process (``src.web_ui.__main__`` passes the app object, which
uvicorn cannot fork into workers); a multi-worker layout would allow one audit
per worker. A timed-out or cancelled audit is killed with its whole process
group, the subprocesses it started included; a finished report of a version is reused
for ``WEBUI_LIFECYCLE_AUDIT_TTL_SECONDS`` unless the admin forces a new run;
the audit writes nothing (session-private ledger copy, read-only graph facade,
see ``src.indexer.lifecycle_audit``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

_logger = logging.getLogger(__name__)

# The outcome of LifecycleAuditCache.start.
START_RUNNING = "running"   # started now, or this version was already running
START_FRESH = "fresh"       # a report younger than the TTL exists; nothing started
START_BUSY = "busy"         # another version's audit is running; nothing started

AuditRunner = Callable[..., Awaitable[dict]]


class AuditFailed(RuntimeError):
    """The audit child did not produce a report (exit code, timeout, bad JSON)."""


def audit_argv(version: str) -> list[str]:
    """The command the page runs: the weekly timer's audit, one version."""
    return [
        sys.executable, "-m", "src.indexer", "lifecycle-audit",
        "--all", "--version", version, "--json",
    ]


def _last_line(data: bytes) -> str:
    lines = [ln for ln in data.decode("utf-8", "replace").splitlines() if ln.strip()]
    return lines[-1][:500] if lines else ""


def _kill_group(pgid: int) -> None:
    """SIGKILL the audit child AND everything it started (its subprocesses).

    The child runs in its own session (``start_new_session=True``), so its pid
    is its process-group id; ``proc.kill()`` alone would leave the processes it
    spawned running after a timeout.
    """
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def run_audit_process(version: str, *, timeout: float) -> dict:
    """Run the audit child for *version*; return its JSON report.

    Raises :class:`AuditFailed` when the child exits non-zero, outlives
    *timeout* (it is killed), prints something that is not the report, or
    reports another schema than the one this process reads (the code on disk
    changed and the Web UI was not restarted).
    """
    from src.indexer.lifecycle_audit import AUDIT_SCHEMA  # noqa: PLC0415

    proc = await asyncio.create_subprocess_exec(
        *audit_argv(version),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError as exc:
            raise AuditFailed(
                f"the audit did not finish within {timeout:g}s and was stopped"
            ) from exc
    finally:
        if proc.returncode is None:
            _kill_group(proc.pid)
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except (TimeoutError, asyncio.CancelledError):
                pass
    if proc.returncode != 0:
        detail = _last_line(err) or "no error output"
        raise AuditFailed(f"the audit exited with code {proc.returncode}: {detail}")
    try:
        report = json.loads(out)
    except ValueError as exc:
        raise AuditFailed("the audit did not print a JSON report") from exc
    if not isinstance(report, dict) or report.get("schema") != AUDIT_SCHEMA:
        got = report.get("schema") if isinstance(report, dict) else type(report).__name__
        raise AuditFailed(
            f"the audit reported schema {got!r}, this Web UI reads {AUDIT_SCHEMA!r} "
            "(restart the Web UI after a deploy)"
        )
    return report


@dataclass
class _VersionState:
    report: dict | None = None
    report_at: float | None = None          # clock() when the report arrived
    report_finished: str | None = None      # wall-clock ISO time of the same
    error: str | None = None                # last run's failure, None when it succeeded
    error_finished: str | None = None


class LifecycleAuditCache:
    """One-at-a-time audit runs with a per-version report reused for *ttl* seconds.

    Lives on the event loop of the Web UI process: ``start`` and ``snapshot``
    never await, so no lock is needed between the check and the start.
    """

    def __init__(
        self,
        runner: AuditRunner,
        *,
        ttl: float,
        timeout: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runner = runner
        self._ttl = ttl
        self._timeout = timeout
        self._clock = clock
        self._states: dict[str, _VersionState] = {}
        self._running: str | None = None
        self._task: asyncio.Task | None = None

    @property
    def running_version(self) -> str | None:
        return self._running

    def _fresh(self, st: _VersionState | None) -> bool:
        return (
            st is not None and st.report is not None and st.report_at is not None
            and self._clock() - st.report_at < self._ttl
        )

    def snapshot(self, version: str) -> dict:
        """The page's view of *version*: state, the last report and its age.

        ``state`` is ``running`` (this version's audit is in progress),
        ``ready`` (a report exists; ``fresh`` says whether it is younger than
        the TTL), ``error`` (the last run failed and no report exists) or
        ``idle`` (never run in this process). A failed re-run keeps the
        previous report next to its ``error``.
        """
        st = self._states.get(version)
        if self._running == version:
            state = "running"
        elif st is not None and st.report is not None:
            state = "ready"
        elif st is not None and st.error:
            state = "error"
        else:
            state = "idle"
        age = None
        if st is not None and st.report_at is not None:
            age = round(max(self._clock() - st.report_at, 0.0), 1)
        return {
            "version": version,
            "state": state,
            "running_version": self._running,
            "fresh": self._fresh(st),
            "ttl_seconds": self._ttl,
            "age_seconds": age,
            "finished_at": st.report_finished if st else None,
            "error": st.error if st else None,
            "error_at": st.error_finished if st else None,
            "report": st.report if st else None,
        }

    def start(self, version: str, *, force: bool = False) -> str:
        """Start the audit of *version* unless it is not needed or not allowed now.

        Returns :data:`START_RUNNING`, :data:`START_FRESH` (a report younger
        than the TTL exists and *force* is False) or :data:`START_BUSY`
        (another version's audit holds the single slot).
        """
        if self._running == version:
            return START_RUNNING
        if self._running is not None:
            return START_BUSY
        if not force and self._fresh(self._states.get(version)):
            return START_FRESH
        self._running = version
        self._task = asyncio.get_running_loop().create_task(self._run(version))
        return START_RUNNING

    async def _run(self, version: str) -> None:
        st = self._states.setdefault(version, _VersionState())
        try:
            report = await self._runner(version, timeout=self._timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - shown on the page, never raised
            _logger.warning("lifecycle page audit of %s failed: %s", version, exc)
            st.error = str(exc)[:1000] or type(exc).__name__
            st.error_finished = datetime.now(UTC).isoformat(timespec="seconds")
        else:
            st.report = report
            st.report_at = self._clock()
            st.report_finished = datetime.now(UTC).isoformat(timespec="seconds")
            st.error = None
            st.error_finished = None
        finally:
            self._running = None
            self._task = None
