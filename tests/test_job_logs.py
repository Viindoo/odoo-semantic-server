# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web UI job logs live where an operator and an admin can read them (#403).

Business rules protected (no database, no Docker):

* The default job-log directory is the service's persistent data directory,
  never the temp dir: the shipped Web UI unit runs with ``PrivateTmp=true``,
  which hides ``/tmp`` from the host and empties it on restart. The shipped
  unit lets the service write there (``ReadWritePaths``).
* The status endpoint reads a bounded tail of whole lines, never the whole file.
* Logs older than the retention are deleted; ``0`` keeps them all; nothing but
  job logs is ever deleted.
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

from src.web_ui.helpers import job_logs

REPO_ROOT = Path(__file__).resolve().parents[1]
WEBUI_UNIT = REPO_ROOT / "docs" / "deploy" / "odoo-semantic-webui.service"


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    directory = tmp_path / "job-logs"
    monkeypatch.setenv("INDEXER_JOB_LOG_DIR", str(directory))
    return directory


def _write(job_id: int, text: str) -> Path:
    path = job_logs.job_log_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_the_default_log_dir_is_the_data_dir_not_the_temp_dir(monkeypatch):
    monkeypatch.delenv("INDEXER_JOB_LOG_DIR", raising=False)
    directory = job_logs.job_log_dir()
    assert directory == Path.home() / ".local" / "share" / "odoo-semantic-mcp" / "job-logs"
    assert not directory.is_relative_to(tempfile.gettempdir())


def test_the_shipped_webui_unit_can_write_the_default_log_dir(monkeypatch):
    # ReadWritePaths= repeats: keep every value.
    with WEBUI_UNIT.open(encoding="utf-8") as fh:
        lines = [ln for ln in fh if not ln.lstrip().startswith("#")]
    writable = [
        Path(ln.split("=", 1)[1].strip()) for ln in lines
        if ln.startswith("ReadWritePaths=")
    ]
    user = next(ln.split("=", 1)[1].strip() for ln in lines if ln.startswith("User="))
    assert "PrivateTmp=true\n" in lines, "positive control: the unit hides /tmp"

    monkeypatch.delenv("INDEXER_JOB_LOG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home") / user))
    default = job_logs.job_log_dir()
    assert any(default.is_relative_to(path) for path in writable), (
        f"{default} is outside every ReadWritePaths= of {WEBUI_UNIT.name}: {writable}")


def test_a_short_log_is_returned_whole(log_dir):
    path = _write(7, "line one\nline two\n")
    tail = job_logs.read_job_log_tail(7)
    assert tail == {"path": str(path), "tail": "line one\nline two\n", "truncated": False}


def test_a_long_log_returns_only_whole_lines_within_the_bound(log_dir):
    lines = [f"2026-11-23 10:00:{i:02d} INFO step {i:04d}\n" for i in range(3000)]
    _write(8, "".join(lines))
    tail = job_logs.read_job_log_tail(8, max_bytes=4096)
    assert tail["truncated"] is True
    assert len(tail["tail"].encode()) <= 4096
    assert tail["tail"].endswith(lines[-1])
    first = tail["tail"].splitlines(keepends=True)[0]
    assert first in lines, "the tail starts on a line boundary"


def test_a_window_that_starts_on_a_line_keeps_that_line(log_dir):
    _write(9, "aaaa\nbbbb\ncccc\n")
    assert job_logs.read_job_log_tail(9, max_bytes=10)["tail"] == "bbbb\ncccc\n"


def test_a_missing_log_has_no_tail(log_dir):
    tail = job_logs.read_job_log_tail(404)
    assert tail["tail"] is None and tail["path"].endswith("osm-job-404.log")


def test_undecodable_bytes_do_not_break_the_tail(log_dir):
    path = job_logs.job_log_path(10)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"ok \xff\xfe bytes\n")
    assert job_logs.read_job_log_tail(10)["tail"].startswith("ok ")


def test_logs_past_the_retention_are_pruned_and_nothing_else(log_dir, monkeypatch):
    monkeypatch.setenv("INDEXER_JOB_LOG_RETENTION_DAYS", "30")
    old = _write(1, "old\n")
    fresh = _write(2, "fresh\n")
    other = log_dir / "notes.txt"
    other.write_text("keep me\n")
    month_ago = time.time() - 31 * 86400
    os.utime(old, (month_ago, month_ago))
    os.utime(other, (month_ago, month_ago))

    assert job_logs.prune_job_logs() == 1
    assert not old.exists() and fresh.exists() and other.exists()


def test_a_zero_retention_keeps_every_log(log_dir, monkeypatch):
    monkeypatch.setenv("INDEXER_JOB_LOG_RETENTION_DAYS", "0")
    old = _write(1, "old\n")
    ancient = time.time() - 3650 * 86400
    os.utime(old, (ancient, ancient))
    assert job_logs.prune_job_logs() == 0 and old.exists()


def test_clone_logs_share_the_directory_and_the_retention(log_dir, monkeypatch):
    monkeypatch.setenv("INDEXER_JOB_LOG_RETENTION_DAYS", "30")
    clone = job_logs.clone_log_path(5)
    assert clone == log_dir / "osm-clone-5.log"
    clone.parent.mkdir(parents=True, exist_ok=True)
    clone.write_text("fatal: repository not found\n")
    assert job_logs.read_clone_log_tail(5)["tail"] == "fatal: repository not found\n"
    month_ago = time.time() - 31 * 86400
    os.utime(clone, (month_ago, month_ago))
    assert job_logs.prune_job_logs() == 1 and not clone.exists()


def test_a_reopened_log_is_narrowed_to_the_service_user(log_dir):
    path = job_logs.clone_log_path(6)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("old\n")
    os.chmod(path, 0o644)
    job_logs.open_clone_log(6).close()
    assert (path.stat().st_mode & 0o777) == 0o600


def test_backup_logs_share_the_directory_and_the_retention(log_dir, monkeypatch):
    monkeypatch.setenv("INDEXER_JOB_LOG_RETENTION_DAYS", "30")
    job = "3f2b8c1e-5d6a-4b7c-9e8f-0a1b2c3d4e5f"
    backup = job_logs.log_path("backup", job)
    assert backup == log_dir / f"osm-backup-{job}.log"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text("Backup written\n")
    month_ago = time.time() - 31 * 86400
    os.utime(backup, (month_ago, month_ago))
    assert job_logs.prune_job_logs() == 1 and not backup.exists()


def test_a_log_identifier_never_names_a_file_outside_the_directory(log_dir):
    for kind, bad in (("backup", "../../etc/passwd"), ("job", "1/../x"), ("clone", "..")):
        with pytest.raises(ValueError):
            job_logs.log_path(kind, bad)
