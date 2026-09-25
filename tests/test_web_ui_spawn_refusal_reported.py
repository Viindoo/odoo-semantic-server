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


@pytest.fixture
def refuse(monkeypatch):
    def fake_spawn(_argv, job_label):
        raise ValueError(_REFUSAL)

    monkeypatch.setattr(subprocess_runner, "spawn_indexer_subcommand", fake_spawn)


def _body(resp) -> dict:
    return json.loads(resp.body)


def _assert_refusal(resp):
    body = _body(resp)
    assert resp.status_code == 500, body
    assert body.get("ok") is False, body
    assert "--job-id" in body.get("error", ""), body
    assert body.get("job_id") is None


@pytest.mark.asyncio
async def test_index_core_reports_a_refused_spawn(refuse, tmp_path):
    from src.web_ui.routes import operations

    body = operations.IndexCoreBody(source=str(tmp_path), version="17.0")
    resp = await operations.post_index_core.__wrapped__(body, None, _user_id=1)

    _assert_refusal(resp)


@pytest.mark.asyncio
async def test_seed_patterns_reports_a_refused_spawn(refuse):
    from src.web_ui.routes import operations

    resp = await operations.post_seed_patterns.__wrapped__(
        operations.SeedPatternsBody(), None, _user_id=1,
    )

    _assert_refusal(resp)


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

    _assert_refusal(resp)
