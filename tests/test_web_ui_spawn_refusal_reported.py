# SPDX-License-Identifier: AGPL-3.0-or-later
"""A refused indexer spawn is reported as an error, never as a started job (#381) - no DB.

``spawn_indexer_subcommand`` raises ``ValueError`` (no job row, nothing
spawned) for a subcommand that cannot report its job. The operations routes
swallowed every spawn exception into HTTP 200 ``ok: true, job_id: null``
("job tracking unavailable"), so the admin read a refusal as a started run.
"""
from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from src.web_ui.helpers import subprocess_runner

_REFUSAL = "indexer subcommand 'x' does not accept --job-id"


@pytest.fixture(params=["refused", "spawn_failed"])
def refuse(request, monkeypatch):
    """Both ways a run does not start: the helper refuses the subcommand, or
    the spawn itself fails (Popen / log file / job row). Either is an error
    answer naming the reason, never ``ok: true``."""
    exc = (
        ValueError(_REFUSAL) if request.param == "refused"
        else OSError("[Errno 12] Cannot allocate memory")
    )

    def fake_spawn(_argv, job_label):
        raise exc

    monkeypatch.setattr(subprocess_runner, "spawn_indexer_subcommand", fake_spawn)
    return "--job-id" if request.param == "refused" else "Cannot allocate memory"


def _body(resp) -> dict:
    return json.loads(resp.body)


def _assert_refusal(resp, reason):
    body = _body(resp)
    assert resp.status_code == 500, body
    assert body.get("ok") is False, body
    assert reason in body.get("error", ""), body
    assert body.get("job_id") is None


@pytest.mark.asyncio
async def test_index_core_reports_a_refused_spawn(refuse, tmp_path):
    from src.web_ui.routes import operations

    body = operations.IndexCoreBody(source=str(tmp_path), version="17.0")
    resp = await operations.post_index_core.__wrapped__(body, None, _user_id=1)

    _assert_refusal(resp, refuse)


@pytest.mark.asyncio
async def test_seed_patterns_reports_a_refused_spawn(refuse):
    from src.web_ui.routes import operations

    resp = await operations.post_seed_patterns.__wrapped__(
        operations.SeedPatternsBody(), None, _user_id=1,
    )

    _assert_refusal(resp, refuse)


@pytest.mark.asyncio
async def test_index_all_reports_a_refused_spawn(refuse, monkeypatch):
    import src.db.pg as pg_mod
    import src.indexer.pipeline as pipeline
    from src.web_ui.routes import repos_indexing

    class _RepoStore:
        def list_profiles(self):
            return [{"name": "p"}]

    class _Pool:
        @contextmanager
        def checkout(self):
            yield object()

    monkeypatch.setattr(pg_mod, "repo_store", lambda: _RepoStore())
    monkeypatch.setattr(pg_mod, "get_pool", lambda: _Pool())
    monkeypatch.setattr(pipeline, "indexer_is_running", lambda _c, _p: False)

    resp = await repos_indexing.index_all.__wrapped__(
        None, repos_indexing.IndexAllBody(), _user_id=1,
    )

    _assert_refusal(resp, refuse)


def test_a_failed_spawn_leaves_its_job_in_error_not_queued(monkeypatch):
    updates: list[dict] = []

    class _Store:
        def create_job(self, _label):
            return 21

        def update_job(self, job_id, **kw):
            updates.append({"id": job_id, **kw})

    def failing_popen(*_a, **_k):
        raise OSError("[Errno 12] Cannot allocate memory")

    monkeypatch.setattr(subprocess_runner, "job_store", lambda: _Store())
    monkeypatch.setattr(subprocess_runner.subprocess, "Popen", failing_popen)

    with pytest.raises(OSError):
        subprocess_runner.spawn_indexer_subcommand(["index-repo", "--profile", "p"], "p")

    (update,) = updates
    assert update["id"] == 21 and update["status"] == "error"
    assert update["finished_at"] is not None
    assert "Cannot allocate memory" in update["error_msg"]
