"""Tests for --job-id flag in src/indexer/__main__.py — pure unit (no PG).

WI-F2: --job-id arg for index-repo subcommand, reports status to job_registry.
"""
from unittest.mock import MagicMock

import pytest

import src.indexer.__main__ as main_mod


@pytest.fixture
def fake_pg():
    """Fake PostgreSQL connection."""
    return MagicMock()


def test_no_job_id_skips_tracking(fake_pg, monkeypatch):
    """When --job-id absent, update_job is never called."""
    update_calls = []

    def mock_update(*args, **kwargs):
        update_calls.append((args, kwargs))

    monkeypatch.setattr("src.indexer.__main__.open_production_pg", lambda: fake_pg)
    monkeypatch.setattr(
        "src.indexer.__main__.index_profile",
        lambda pg, **kw: {"modules": 1, "fields": 0, "methods": 0},
    )
    monkeypatch.setattr("src.indexer.__main__.job_registry.update_job", mock_update)

    rc = main_mod.main(["index-repo", "--profile", "p1", "--no-embed"])
    assert rc == 0
    assert update_calls == []  # tracking never invoked


def test_with_job_id_marks_running_then_done(fake_pg, monkeypatch):
    """Successful run: update_job called twice (running → done)."""
    calls = []

    def mock_update(conn, job_id, **kw):
        calls.append((job_id, kw))

    monkeypatch.setattr("src.indexer.__main__.open_production_pg", lambda: fake_pg)
    monkeypatch.setattr(
        "src.indexer.__main__.index_profile",
        lambda pg, **kw: {"modules": 1, "fields": 0, "methods": 0},
    )
    monkeypatch.setattr("src.indexer.__main__.job_registry.update_job", mock_update)

    rc = main_mod.main(["index-repo", "--profile", "p1", "--no-embed", "--job-id", "42"])
    assert rc == 0
    assert len(calls) == 2
    assert calls[0][0] == 42 and calls[0][1]["status"] == "running"
    assert "pid" in calls[0][1] and "started_at" in calls[0][1]
    assert calls[1][0] == 42 and calls[1][1]["status"] == "done"
    assert "finished_at" in calls[1][1]


def test_with_job_id_marks_error_on_exception(fake_pg, monkeypatch):
    """Failed run: update_job called twice (running → error) + exception re-raised."""
    calls = []

    def mock_update(conn, job_id, **kw):
        calls.append((job_id, kw))

    def boom(*a, **kw):
        raise RuntimeError("boom!")

    monkeypatch.setattr("src.indexer.__main__.open_production_pg", lambda: fake_pg)
    monkeypatch.setattr("src.indexer.__main__.index_profile", boom)
    monkeypatch.setattr("src.indexer.__main__.job_registry.update_job", mock_update)

    with pytest.raises(RuntimeError, match="boom"):
        main_mod.main(["index-repo", "--profile", "p1", "--no-embed", "--job-id", "99"])

    assert len(calls) == 2
    assert calls[0][1]["status"] == "running"
    assert calls[1][1]["status"] == "error"
    assert calls[1][1]["error_msg"].startswith("boom")


def test_update_job_failure_does_not_block_indexing(fake_pg, monkeypatch):
    """If update_job raises (e.g. job row deleted), indexing still completes."""
    monkeypatch.setattr("src.indexer.__main__.open_production_pg", lambda: fake_pg)
    monkeypatch.setattr(
        "src.indexer.__main__.index_profile",
        lambda pg, **kw: {"modules": 1, "fields": 0, "methods": 0},
    )
    monkeypatch.setattr(
        "src.indexer.__main__.job_registry.update_job",
        MagicMock(side_effect=Exception("DB down"))
    )

    rc = main_mod.main(["index-repo", "--profile", "p1", "--no-embed", "--job-id", "1"])
    assert rc == 0  # indexing succeeded despite tracking failure
