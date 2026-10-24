# SPDX-License-Identifier: AGPL-3.0-or-later
"""A SIGTERM ends a multi-worker index run promptly (no DB, no network).

Observed on prod 2026-10-24: ``systemctl stop odoo-semantic-reindex`` on an
``index-repo --all --profile-workers 2`` run did not stop within systemd's
90s TimeoutStopSec and was SIGKILLed. Both workers were inside embed
requests to a slow CPU embedder (9-18 minutes each). The SIGTERM handler only
runs in the main thread: the executor's ``shutdown(wait=True)`` and the
interpreter's join of its worker threads then waited for whole profiles.

Business rules protected here:

- A stop request ends the embed retry loop at once: no backoff sleep, no new
  request, and the pipeline's ``except Exception`` fallbacks (full-batch
  retry, per-chunk degrade) do not turn it into more requests.
- A SIGTERM during a multi-worker run (``--profile-workers`` and
  ``--max-workers``) returns from the run without waiting for the running
  workers, and the queued profiles / repos are never started.
- The real ``python -m src.indexer`` process ends well within systemd's stop
  timeout, exit code 1, even when its workers are blocked in HTTP reads that
  nothing can interrupt; a single-profile run still stops at once.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

import src.indexer.__main__ as main_mod
from src.indexer import stop as run_stop
from src.indexer.embedder import Qwen3Embedder
from src.indexer.writer_pgvector import EmbeddingChunk, _embed_chunks_resilient

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _no_stop_request():
    run_stop.reset()
    yield
    run_stop.reset()


def _chunk(entity: str) -> EmbeddingChunk:
    return EmbeddingChunk(
        chunk_type="field", module="stop_mod", odoo_version="99.0",
        entity_name=entity, model_name="stop.mod.record",
        file_path="stop_mod/models/record.py", chunk_idx=0, content=f"text {entity}",
    )


# ---------------------------------------------------------------------------
# Embed retry loop
# ---------------------------------------------------------------------------


def test_a_stop_request_ends_the_embed_retry_without_another_request():
    """The embedder times out; the stop lands during the retry backoff (60s):
    the embed must end at once with the stop, after that single request."""
    requests: list[httpx.Request] = []
    first_request = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        first_request.set()
        raise httpx.ReadTimeout("The read operation timed out", request=request)

    embedder = Qwen3Embedder(
        url="http://embedder.test", model="m", dim=2, retries=3,
        transport=httpx.MockTransport(handler),
        retry_backoff_base=60.0, retry_backoff_max=60.0,
    )
    outcome: list[BaseException | tuple] = []

    def run():
        try:
            outcome.append(_embed_chunks_resilient(embedder, [_chunk("a"), _chunk("b")]))
        except BaseException as exc:  # noqa: BLE001 - the outcome under test
            outcome.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    assert first_request.wait(5), "the embedder was never called"
    time.sleep(0.2)  # inside the 60s backoff now
    stopped_at = time.monotonic()
    run_stop.request_stop()
    worker.join(5)
    try:
        assert not worker.is_alive(), "still embedding 5s after the stop request"
        assert time.monotonic() - stopped_at < 2
        (result,) = outcome
        assert isinstance(result, run_stop.IndexRunStopped), result
        assert len(requests) == 1, (
            f"{len(requests)} embed requests: a stopped run must send no new one"
        )
    finally:
        embedder.close()


def test_a_stopped_run_sends_no_embed_request():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"embeddings": [[1.0, 0.0]]})

    embedder = Qwen3Embedder(
        url="http://embedder.test", model="m", dim=2,
        transport=httpx.MockTransport(handler),
    )
    run_stop.request_stop()
    try:
        with pytest.raises(run_stop.IndexRunStopped):
            _embed_chunks_resilient(embedder, [_chunk("a")])
    finally:
        embedder.close()
    assert calls == []


# ---------------------------------------------------------------------------
# Multi-worker pipeline paths, in process
# ---------------------------------------------------------------------------


def _mock_conn():
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone.return_value = (True,)  # advisory lock acquired
    conn.cursor.return_value = cur
    return conn


def _repo(repo_id: int) -> dict:
    return {
        "id": repo_id, "local_path": "/fake/repo", "odoo_version": "99.0",
        "url": "file://local", "branch": "99.0",
    }


def _counters() -> dict:
    return dict.fromkeys(("modules", "views", "qweb", "embeddings", "js_patches", "owl_comps"), 0)


@pytest.fixture
def sigterm_handler():
    """The CLI's SIGTERM handler (no job), restored after the test."""
    previous = signal.getsignal(signal.SIGTERM)
    main_mod._install_sigterm_handler(None)
    yield
    signal.signal(signal.SIGTERM, previous)


@pytest.mark.parametrize("path", ["profile_workers", "max_workers"])
def test_sigterm_during_a_two_worker_run_does_not_wait_for_the_workers(sigterm_handler, path):
    """Three profiles (or repos), two workers, both blocked in work that no
    signal can interrupt (like an HTTP read): SIGTERM must end the run within
    a second, and the third one must never start."""
    release = threading.Event()  # the blocked work, released at teardown
    started: list[int] = []
    lock = threading.Lock()
    both_inside = threading.Event()

    def blocked_index_repo(repo, writer, **_kw):
        with lock:
            started.append(repo["id"])
            if len(started) == 2:
                both_inside.set()
        release.wait(30)
        return _counters()

    store = MagicMock()
    if path == "profile_workers":
        store.list_profiles.return_value = [
            {"name": f"p{i}", "odoo_version": "99.0"} for i in (1, 2, 3)
        ]
        store.get_repos_for_profile.side_effect = lambda name: [_repo(int(name[1:]))]
    else:
        store.get_repos_for_profile.return_value = [_repo(i) for i in (1, 2, 3)]
    store.get_ancestor_profile_names.side_effect = lambda name: [name]

    def send_sigterm():
        if both_inside.wait(10):
            os.kill(os.getpid(), signal.SIGTERM)

    # The pre-fix code waits for the workers: release them after 6s so a
    # failing run ends instead of hanging the suite.
    safety = threading.Timer(6.0, release.set)
    safety.start()
    threads_before = set(threading.enumerate())
    threading.Thread(target=send_sigterm, daemon=True).start()
    with (
        patch("src.indexer.pipeline.repo_store", return_value=store),
        patch("src.indexer.pipeline.Neo4jWriter", return_value=MagicMock()),
        patch("src.indexer.pipeline._neo4j_creds", return_value=("bolt://x", "u", "p")),
        patch("src.indexer.pipeline._index_repo", side_effect=blocked_index_repo),
        patch("src.indexer.pipeline.open_production_pg", side_effect=_mock_conn),
    ):
        from src.indexer.pipeline import index_all, index_profile

        t0 = time.monotonic()
        try:
            with pytest.raises(SystemExit) as exc:
                if path == "profile_workers":
                    index_all(_mock_conn(), profile_workers=2)
                else:
                    index_profile(_mock_conn(), profile_name="p", max_workers=2)
            elapsed = time.monotonic() - t0
        finally:
            release.set()
            safety.cancel()
            # The workers finish under the patches, never against real services.
            for thread in set(threading.enumerate()) - threads_before:
                if not thread.daemon:
                    thread.join(10)
    assert both_inside.is_set()
    assert exc.value.code == 1
    assert elapsed < 3, f"the stopped run waited {elapsed:.1f}s for its workers"
    assert sorted(started) == [1, 2], (
        f"started {sorted(started)}: a stopped run must not start queued work"
    )


def _unlocked(conn) -> bool:
    return any(
        "pg_advisory_unlock" in str(c.args[0])
        for c in conn.cursor.return_value.execute.call_args_list
    )


@pytest.mark.parametrize("path", ["profile_workers", "max_workers"])
def test_a_stopped_run_keeps_the_profile_lock_while_its_workers_may_write(
    sigterm_handler, path,
):
    """The run returns on SIGTERM while workers are still inside their repo
    (an embed read that returns during the grace is followed by its pgvector
    write). The profile advisory lock must stay held, and its connection
    open, until the process ends: released earlier, a second run of the
    profile could start and write beside the old workers."""
    release = threading.Event()
    lock = threading.Lock()
    started: list[int] = []
    both_inside = threading.Event()
    opened: list = []

    def blocked_index_repo(repo, writer, **_kw):
        with lock:
            started.append(repo["id"])
            if len(started) == 2:
                both_inside.set()
        release.wait(30)
        return _counters()

    def open_pg():
        conn = _mock_conn()
        opened.append(conn)
        return conn

    store = MagicMock()
    if path == "profile_workers":
        store.list_profiles.return_value = [
            {"name": f"p{i}", "odoo_version": "99.0"} for i in (1, 2)
        ]
        # Two repos per profile: the second one is never started after the stop.
        store.get_repos_for_profile.side_effect = lambda name: [
            _repo(int(name[1:])), _repo(10 + int(name[1:])),
        ]
    else:
        store.get_repos_for_profile.return_value = [_repo(i) for i in (1, 2, 3)]
    store.get_ancestor_profile_names.side_effect = lambda name: [name]
    main_conn = _mock_conn()

    def send_sigterm():
        if both_inside.wait(10):
            os.kill(os.getpid(), signal.SIGTERM)

    safety = threading.Timer(6.0, release.set)
    safety.start()
    threads_before = set(threading.enumerate())
    threading.Thread(target=send_sigterm, daemon=True).start()
    with (
        patch("src.indexer.pipeline.repo_store", return_value=store),
        patch("src.indexer.pipeline.Neo4jWriter", return_value=MagicMock()),
        patch("src.indexer.pipeline._neo4j_creds", return_value=("bolt://x", "u", "p")),
        patch("src.indexer.pipeline._index_repo", side_effect=blocked_index_repo),
        patch("src.indexer.pipeline.open_production_pg", side_effect=open_pg),
    ):
        from src.indexer.pipeline import index_all, index_profile

        try:
            with pytest.raises(SystemExit):
                if path == "profile_workers":
                    index_all(main_conn, profile_workers=2)
                else:
                    index_profile(main_conn, profile_name="p", max_workers=2)
            # The run returned; the two workers are still blocked in their repo.
            lock_conns = (
                [c for c in opened if c.cursor.called] if path == "profile_workers"
                else [main_conn]
            )
            unlocked_while_running = [_unlocked(c) for c in lock_conns]
        finally:
            release.set()
            safety.cancel()
            for thread in set(threading.enumerate()) - threads_before:
                if not thread.daemon:
                    thread.join(10)
    assert both_inside.is_set()
    assert lock_conns, "precondition: the profile lock was taken"
    assert unlocked_while_running == [False] * len(lock_conns), (
        "the profile lock was released while its workers were still running"
    )
    # Even after the workers unwound: the lock is left to the process exit.
    for conn in lock_conns:
        assert not _unlocked(conn)
        assert run_stop.is_held(conn)
        conn.close.assert_not_called()


def test_a_run_that_is_not_stopped_still_releases_the_profile_lock():
    store = MagicMock()
    store.get_repos_for_profile.return_value = [_repo(i) for i in (1, 2)]
    store.get_ancestor_profile_names.return_value = ["p"]
    conn = _mock_conn()
    with (
        patch("src.indexer.pipeline.repo_store", return_value=store),
        patch("src.indexer.pipeline.Neo4jWriter", return_value=MagicMock()),
        patch("src.indexer.pipeline._neo4j_creds", return_value=("bolt://x", "u", "p")),
        patch("src.indexer.pipeline._index_repo", return_value=_counters()),
        patch("src.indexer.pipeline.open_production_pg", side_effect=_mock_conn),
        patch("src.indexer.seed_patterns.run", return_value={"skipped": True}),
    ):
        from src.indexer.pipeline import index_profile

        index_profile(conn, profile_name="p", max_workers=2)

    assert _unlocked(conn)
    assert not run_stop.is_held(conn)


# ---------------------------------------------------------------------------
# The real process entry (python -m src.indexer), in a child process
# ---------------------------------------------------------------------------

# The child runs ``python -m src.indexer index-repo`` through runpy with the
# database, Neo4j and repo stage stubbed; the embedder is the real HTTP client
# against a local socket that accepts and never answers - the slow-embedder
# read no thread can interrupt. Every DSN points nowhere, and .env is never
# loaded, so nothing can reach a real service.
_CHILD = textwrap.dedent('''
    import runpy
    import socket
    import sys
    import threading
    from unittest.mock import MagicMock

    ready_file, mode = sys.argv[1], sys.argv[2]
    expected = 2 if mode == "all" else 1

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    port = server.getsockname()[1]
    held = []

    def accept():
        while True:
            conn, _ = server.accept()
            held.append(conn)
            if len(held) >= expected:
                with open(ready_file, "w") as fh:
                    fh.write("ready")

    threading.Thread(target=accept, daemon=True).start()

    import src.config as config
    import src.indexer.embedder as embedder_mod
    import src.indexer.pipeline as pipeline
    import src.indexer.stop as run_stop
    from src.indexer.writer_pgvector import EmbeddingChunk, _embed_chunks_resilient

    run_stop.STOP_GRACE_S = 1.0
    config.init_dotenv = lambda: None
    config.get = lambda section, key, fallback=None: (
        f"http://127.0.0.1:{port}" if (section, key) == ("embedder", "url") else fallback
    )
    config.from_env_or_ini = lambda *a, fallback=None, **k: fallback
    embedder_mod._resolved_timeout_read = lambda default: 300
    embedder_mod._resolved_max_batch = lambda default: default

    def conn():
        c = MagicMock()
        cur = MagicMock()
        cur.__enter__ = lambda s: s
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone.return_value = (True,)
        c.cursor.return_value = cur
        return c

    store = MagicMock()
    store.list_profiles.return_value = [
        {"name": n, "odoo_version": "99.0"} for n in ("alpha", "beta")
    ]
    store.get_repos_for_profile.side_effect = lambda name: [{
        "id": 1 if name == "alpha" else 2, "local_path": "/fake", "odoo_version": "99.0",
        "url": "file://local", "branch": "99.0",
    }]
    store.get_ancestor_profile_names.side_effect = lambda name: [name]

    def index_repo(repo, writer, *, embedder=None, **_kw):
        chunk = EmbeddingChunk(
            chunk_type="field", module=f"m{repo['id']}", odoo_version="99.0",
            entity_name="f", model_name="x.y", file_path="m/models/x.py",
            chunk_idx=0, content="text",
        )
        _embed_chunks_resilient(embedder, [chunk])
        return dict.fromkeys(
            ("modules", "views", "qweb", "embeddings", "js_patches", "owl_comps"), 0,
        )

    pipeline.open_production_pg = conn
    pipeline.repo_store = lambda: store
    pipeline.Neo4jWriter = lambda *a, **k: MagicMock()
    pipeline._neo4j_creds = lambda: ("bolt://127.0.0.1:1", "u", "p")
    pipeline._index_repo = index_repo

    if mode == "all":
        args = ["--all", "--profile-workers", "2"]
    else:
        args = ["--profile", "alpha"]
    sys.argv = ["src.indexer", "index-repo", "--no-fetch", *args]
    runpy.run_module("src.indexer", run_name="__main__", alter_sys=True)
''')


def _start_child(tmp_path: Path, mode: str) -> tuple[subprocess.Popen, Path]:
    script = tmp_path / "child.py"
    script.write_text(_CHILD, encoding="utf-8")
    ready = tmp_path / "ready"
    env = {
        **os.environ,
        "PYTHONPATH": str(_REPO_ROOT),
        "PG_DSN": "host=127.0.0.1 port=1 dbname=nowhere connect_timeout=1",
        "NEO4J_URI": "bolt://127.0.0.1:1",
        "ODOO_SEMANTIC_CONF": str(tmp_path / "missing.conf"),
        "EMBEDDER_BACKEND": "ollama",
    }
    proc = subprocess.Popen(
        [sys.executable, str(script), str(ready), mode],
        cwd=str(tmp_path), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    deadline = time.monotonic() + 60
    while not ready.exists():
        if proc.poll() is not None:
            out, err = proc.communicate()
            pytest.fail(f"child exited {proc.returncode} before embedding:\n{out}\n{err}")
        if time.monotonic() > deadline:
            proc.kill()
            out, err = proc.communicate()
            pytest.fail(f"child never reached the embed requests:\n{out}\n{err}")
        time.sleep(0.05)
    time.sleep(0.2)  # both reads are in flight
    return proc, ready


def _stop_child(proc: subprocess.Popen, budget_s: float) -> tuple[float, str]:
    t0 = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    try:
        _out, err = proc.communicate(timeout=budget_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        _out, err = proc.communicate()
        pytest.fail(
            f"the index run was still running {budget_s:.0f}s after SIGTERM "
            f"(systemd would SIGKILL it):\n{err[-3000:]}"
        )
    return time.monotonic() - t0, err


def test_sigterm_ends_a_two_profile_run_blocked_in_embed_requests(tmp_path):
    """``index-repo --all --profile-workers 2``, both workers inside an embed
    request that never answers: the process ends within the stop grace
    (1s here, 15s in production), exit 1, saying it abandoned the workers."""
    proc, _ = _start_child(tmp_path, "all")
    elapsed, err = _stop_child(proc, budget_s=10)
    assert proc.returncode == 1, err[-3000:]
    assert elapsed < 10
    assert "still busy" in err, err[-3000:]


def test_sigterm_still_ends_a_single_profile_run_at_once(tmp_path):
    """No workers: the SIGTERM interrupts the main thread's own embed request,
    the run exits 1 at once and abandons nothing."""
    proc, _ = _start_child(tmp_path, "profile")
    elapsed, err = _stop_child(proc, budget_s=10)
    assert proc.returncode == 1, err[-3000:]
    assert elapsed < 5
    assert "still busy" not in err, err[-3000:]
