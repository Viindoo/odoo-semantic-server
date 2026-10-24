# SPDX-License-Identifier: AGPL-3.0-or-later
"""Process-wide stop request for an index run (SIGTERM, Ctrl-C).

A SIGTERM handler runs in the main thread only. With ``--profile-workers`` or
``--max-workers`` above 1 the work happens in ``ThreadPoolExecutor`` worker
threads, which a signal never interrupts: before this module, a stopped run
waited for every worker to finish its whole profile (the executor's
``shutdown(wait=True)`` and the interpreter's join of its threads at exit),
and systemd SIGKILLed it after ``TimeoutStopSec`` (2026-10-24, two workers
inside 9-18 minute embed requests to a slow CPU embedder).

The pieces, each used where the run can block:

* :func:`request_stop` - set by the CLI SIGTERM handler (and by
  :func:`stoppable_executor` when the main thread leaves it on SystemExit or
  KeyboardInterrupt).
* :func:`raise_if_stop_requested` / :func:`sleep_or_stop` - checkpoints the
  workers pass (per profile, per repo, per module, before each embed attempt
  and during its retry backoff). They raise :class:`IndexRunStopped`, a
  ``BaseException`` so the ``except Exception`` fallbacks of the pipeline
  (the per-chunk embed fallback, the per-repo "failed - continuing") never
  swallow it and turn a stop into minutes of further work.
* :func:`stoppable_executor` - a ``ThreadPoolExecutor`` that, when the main
  thread leaves it on a non-``Exception`` BaseException, drops the queued work
  and does not wait for the running work.

An HTTP read already in flight cannot be interrupted from another thread
(closing the httpx client does not wake it); the CLI process entry
(``src.indexer.__main__.run_as_process``) therefore waits a bounded grace for
the workers and then ends the process with ``os._exit``. The database
connections are then closed by the kernel: PostgreSQL rolls back the open
transactions and releases the advisory locks, Neo4j rolls back its
transactions.

The MCP server and the pattern seeder import the embedder too but never call
:func:`request_stop`, so for them the checkpoints are no-ops.
"""
from __future__ import annotations

import concurrent.futures
import threading
from collections.abc import Iterator
from contextlib import contextmanager

_STOP = threading.Event()

# Seconds a stopped CLI run waits for its worker threads before it ends the
# process with os._exit (``src.indexer.__main__.run_as_process``). Counted
# after the SIGTERM handler, whose job update connects within 5s: the total
# stays well under systemd's default TimeoutStopSec (90s).
STOP_GRACE_S: float = 15.0


class IndexRunStopped(BaseException):
    """The run was asked to stop (SIGTERM): unwind without doing more work.

    A ``BaseException`` (like ``SystemExit``) on purpose: the pipeline's
    ``except Exception`` handlers must let it through."""


def request_stop() -> None:
    """Ask every worker of this process to stop at its next checkpoint."""
    _STOP.set()


def stop_requested() -> bool:
    return _STOP.is_set()


def reset() -> None:
    """Clear the request (tests; a run never clears it)."""
    _STOP.clear()


def raise_if_stop_requested() -> None:
    if _STOP.is_set():
        raise IndexRunStopped("index run stop requested")


def sleep_or_stop(seconds: float) -> None:
    """``time.sleep(seconds)`` that ends early, raising, on a stop request."""
    if _STOP.wait(seconds):
        raise IndexRunStopped("index run stop requested")


@contextmanager
def stoppable_executor(max_workers: int) -> Iterator[concurrent.futures.ThreadPoolExecutor]:
    """``with ThreadPoolExecutor(max_workers) as ex`` that does not hang a stop.

    Normal exit and an ``Exception`` keep the ``with`` semantics: wait for
    every submitted task. A SystemExit (the SIGTERM handler) or a
    KeyboardInterrupt requests the stop, cancels the queued tasks and returns
    at once; the running tasks unwind at their next checkpoint."""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    try:
        yield executor
    except Exception:
        executor.shutdown(wait=True)
        raise
    except BaseException:
        request_stop()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    executor.shutdown(wait=True)
