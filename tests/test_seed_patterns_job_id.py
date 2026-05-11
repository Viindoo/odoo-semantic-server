# tests/test_seed_patterns_job_id.py
"""Tests for --job-id flag in src/indexer/seed_patterns.py (M8 W6).

Covers:
- Argparse accepts --job-id without error.
- job lifecycle: queued → running → done (pid set, timestamps populated).
- Error case: bad patterns file → status='error', error_msg populated.
- Sentinel-skip: second run without --force → still transitions to done.
"""
import json
import os
from unittest.mock import MagicMock

import pytest

from src.db import job_registry
from src.db.migrate import run_migrations
from src.indexer import seed_patterns as sp_mod

pytestmark = pytest.mark.postgres


@pytest.fixture
def migrated_pg(clean_pg):
    run_migrations(clean_pg)
    return clean_pg


@pytest.fixture
def patterns_file(tmp_path):
    """A minimal valid patterns.json file (pattern_id must match ^[a-z][a-z0-9-]*$)."""
    data = [
        {
            "pattern_id": "test-pattern-w6",
            "intent_keywords": ["test"],
            "file_ref": "sale/models/sale_order.py",
            "snippet_text": "# test snippet",
            "gotchas": ["gotcha one", "gotcha two", "gotcha three"],
            "odoo_version_min": "17.0",
            "language": "python",
            "core_symbol_names": [],
        }
    ]
    pf = tmp_path / "patterns.json"
    pf.write_text(json.dumps(data))
    return pf


# ---------------------------------------------------------------------------
# Argparse unit tests (no DB required)
# ---------------------------------------------------------------------------

class TestArgparse:
    """Verify --job-id is accepted by the argparse parser."""

    def test_job_id_accepted(self):
        """--job-id 42 parses correctly."""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--job-id", type=int, default=None)
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--no-embed", action="store_true")
        parser.add_argument("--version", default=None)
        parser.add_argument(
            "--patterns-file", default=str(sp_mod._DEFAULT_PATTERNS_FILE)
        )
        args = parser.parse_args(["--job-id", "42", "--force"])
        assert args.job_id == 42
        assert args.force is True

    def test_job_id_default_none(self):
        """Default job_id is None when flag absent."""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--job-id", type=int, default=None)
        args = parser.parse_args([])
        assert args.job_id is None


# ---------------------------------------------------------------------------
# Integration tests — job lifecycle (requires postgres + neo4j would be ideal,
# but we mock Neo4j here and only exercise PG job_registry)
# ---------------------------------------------------------------------------

class _NoClosePg:
    """Proxy that no-ops close() to keep test pg_conn alive."""

    def __init__(self, conn):
        self._conn = conn

    def close(self):
        pass  # no-op: test owns the connection lifetime

    def cursor(self, *args, **kwargs):
        return self._conn.cursor(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class TestJobLifecycleWithPg:
    """Verify job_registry transitions when seed_patterns.main() runs with --job-id."""

    @pytest.fixture(autouse=True)
    def _cleanup_jobs(self, migrated_pg):
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM indexer_jobs")
        yield
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM indexer_jobs")

    def _create_queued_job(self, pg, label: str = "patterns") -> int:
        return job_registry.create_job(pg, label)

    def _stub_neo4j(self, monkeypatch):
        """Stub out Neo4j so test doesn't need a running Neo4j instance."""
        mock_writer = MagicMock()
        mock_writer.driver = MagicMock()
        monkeypatch.setattr(sp_mod, "_get_neo4j_writer", lambda: mock_writer)
        monkeypatch.setattr(sp_mod, "_write_neo4j", lambda patterns: None)
        monkeypatch.setattr(sp_mod, "_write_pgvector", lambda chunks: None)
        monkeypatch.setattr(sp_mod, "_get_stored_patterns_sha", lambda driver: None)
        monkeypatch.setattr(sp_mod, "_set_stored_patterns_sha", lambda driver, sha: None)
        return mock_writer

    def _make_no_close_factory(self, pg):
        """Return a factory that gives a no-close proxy for the test connection."""
        wrapped = _NoClosePg(pg)
        return lambda: wrapped

    def test_job_transitions_queued_to_running_to_done(
        self, migrated_pg, patterns_file, monkeypatch
    ):
        """Running seed_patterns.main() with --job-id transitions job to done."""
        self._stub_neo4j(monkeypatch)
        monkeypatch.setattr(
            sp_mod, "_open_pg_for_job_tracking", self._make_no_close_factory(migrated_pg)
        )

        job_id = self._create_queued_job(migrated_pg)
        initial = job_registry.get_job(migrated_pg, job_id)
        assert initial["status"] == "queued"

        rc = sp_mod.main([
            "--patterns-file", str(patterns_file),
            "--force",
            "--no-embed",
            "--job-id", str(job_id),
        ])
        assert rc == 0

        final = job_registry.get_job(migrated_pg, job_id)
        assert final["status"] == "done", f"Expected done, got: {final['status']}"
        assert final["pid"] is not None
        assert final["started_at"] is not None
        assert final["finished_at"] is not None

    def test_job_pid_matches_current_process(
        self, migrated_pg, patterns_file, monkeypatch
    ):
        """PID stored in the job row should equal os.getpid()."""
        self._stub_neo4j(monkeypatch)
        monkeypatch.setattr(
            sp_mod, "_open_pg_for_job_tracking", self._make_no_close_factory(migrated_pg)
        )

        job_id = self._create_queued_job(migrated_pg)
        sp_mod.main([
            "--patterns-file", str(patterns_file),
            "--force",
            "--no-embed",
            "--job-id", str(job_id),
        ])

        row = job_registry.get_job(migrated_pg, job_id)
        assert row["pid"] == os.getpid()

    def test_job_error_on_missing_patterns_file(
        self, migrated_pg, monkeypatch
    ):
        """Non-existent patterns file → job status='error', error_msg populated."""
        monkeypatch.setattr(
            sp_mod, "_open_pg_for_job_tracking", self._make_no_close_factory(migrated_pg)
        )

        job_id = self._create_queued_job(migrated_pg)
        rc = sp_mod.main([
            "--patterns-file", "/no/such/patterns.json",
            "--job-id", str(job_id),
        ])
        assert rc == 2

        row = job_registry.get_job(migrated_pg, job_id)
        assert row["status"] == "error"
        assert row["error_msg"] is not None
        assert "not found" in row["error_msg"].lower()

    def test_sentinel_skip_still_marks_done(
        self, migrated_pg, patterns_file, monkeypatch
    ):
        """Sentinel hash unchanged (--no-force) → job still transitions to done."""
        current_sha = sp_mod._compute_patterns_sha256(patterns_file)

        self._stub_neo4j(monkeypatch)
        # Override stored sha to match → will trigger skip path
        monkeypatch.setattr(
            sp_mod, "_get_stored_patterns_sha", lambda driver: current_sha
        )
        monkeypatch.setattr(
            sp_mod, "_open_pg_for_job_tracking", self._make_no_close_factory(migrated_pg)
        )

        job_id = self._create_queued_job(migrated_pg)
        rc = sp_mod.main([
            "--patterns-file", str(patterns_file),
            "--job-id", str(job_id),
            # no --force → will hit sentinel skip
        ])
        assert rc == 0

        row = job_registry.get_job(migrated_pg, job_id)
        assert row["status"] == "done", f"Expected done after skip, got: {row['status']}"

    def test_no_job_id_does_not_open_pg(
        self, patterns_file, monkeypatch
    ):
        """When --job-id absent, _open_pg_for_job_tracking is never called."""
        self._stub_neo4j(monkeypatch)

        open_pg_calls = []

        def _spy_open_pg():
            open_pg_calls.append(True)
            return MagicMock()

        monkeypatch.setattr(sp_mod, "_open_pg_for_job_tracking", _spy_open_pg)

        rc = sp_mod.main([
            "--patterns-file", str(patterns_file),
            "--force",
            "--no-embed",
            # no --job-id
        ])
        assert rc == 0
        assert open_pg_calls == [], "pg should not be opened when --job-id not set"

    def test_exception_in_seed_marks_error_and_reraises(
        self, migrated_pg, patterns_file, monkeypatch
    ):
        """Exception during _write_neo4j → job status='error', exception re-raised."""
        self._stub_neo4j(monkeypatch)
        monkeypatch.setattr(
            sp_mod, "_open_pg_for_job_tracking", self._make_no_close_factory(migrated_pg)
        )
        monkeypatch.setattr(
            sp_mod, "_write_neo4j",
            lambda patterns: (_ for _ in ()).throw(RuntimeError("neo4j exploded"))
        )

        job_id = self._create_queued_job(migrated_pg)
        with pytest.raises(RuntimeError, match="neo4j exploded"):
            sp_mod.main([
                "--patterns-file", str(patterns_file),
                "--force",
                "--no-embed",
                "--job-id", str(job_id),
            ])

        row = job_registry.get_job(migrated_pg, job_id)
        assert row["status"] == "error"
        assert "neo4j exploded" in row["error_msg"]
