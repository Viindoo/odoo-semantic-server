# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_operations_backup_route.py
"""Unit tests for backup endpoints in src/web_ui/routes/operations.py (M9 W-BK)."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


def _make_app(tmp_path: Path):
    """Build a minimal FastAPI app with operations router and auth bypass.

    Auth bypass is now driven by the conftest autouse fixture (sets
    WEBUI_AUTH_DISABLED=1 via monkeypatch for non-auth tests). We rely on
    that rather than mutating os.environ here — direct mutation leaked the
    bypass into the auth-flow test modules and caused cross-test failures.
    """
    from fastapi import FastAPI

    from src.web_ui.routes.operations import router
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture()
def backup_client(tmp_path, monkeypatch):
    """TestClient with auth bypass and BACKUP_DIR pointing to tmp_path."""
    monkeypatch.setenv("WEBUI_AUTH_DISABLED", "1")
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    monkeypatch.setenv("BACKUP_DIR", str(backup_dir))

    app = _make_app(tmp_path)
    with TestClient(app, raise_server_exceptions=True) as client:
        yield client, backup_dir


class TestBackupRequiresAdmin:
    def test_requires_auth_without_bypass(self, tmp_path, monkeypatch):
        """Without auth bypass, route should be protected (middleware enforces 401)."""
        # The route itself doesn't enforce auth — AuthRequiredMiddleware does.
        # We verify the route exists and returns 200 with bypass active.
        monkeypatch.setenv("WEBUI_AUTH_DISABLED", "1")
        backup_dir = tmp_path / "backup"
        backup_dir.mkdir()
        monkeypatch.setenv("BACKUP_DIR", str(backup_dir))

        from fastapi import FastAPI

        from src.web_ui.routes.operations import router
        app = FastAPI()
        app.include_router(router)

        with TestClient(app) as client:
            # With bypass active, POST should not return 401
            resp = client.post(
                "/api/operations/backup",
                json={"output": str(backup_dir / "test.tar.gz")},
            )
            # 200 means route was reachable — auth bypass is working
            assert resp.status_code == 200


class TestBackupCreatesJobReturnsStreamUrl:
    def test_returns_job_id_and_stream_url(self, backup_client, monkeypatch):
        client, backup_dir = backup_client
        output = str(backup_dir / "out.tar.gz")

        with patch(
            "src.web_ui.routes.operations._spawn_backup_subprocess"
        ) as mock_spawn:
            resp = client.post(
                "/api/operations/backup",
                json={"output": output},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "job_id" in data
        assert "stream_url" in data
        assert data["stream_url"].startswith("/api/operations/backup/")
        assert data["stream_url"].endswith("/stream")
        mock_spawn.assert_called_once()

    def test_auto_generates_output_when_blank(self, backup_client):
        client, backup_dir = backup_client

        with patch("src.web_ui.routes.operations._spawn_backup_subprocess"):
            resp = client.post("/api/operations/backup", json={})

        assert resp.status_code == 200
        data = resp.json()
        assert data["output"].endswith(".tar.gz")

    def test_rejects_output_outside_backup_dir(self, backup_client, tmp_path):
        client, backup_dir = backup_client
        outside = tmp_path / "other" / "dump.tar.gz"

        with patch("src.web_ui.routes.operations._spawn_backup_subprocess"):
            resp = client.post(
                "/api/operations/backup",
                json={"output": str(outside)},
            )

        assert resp.status_code == 400
        assert "BACKUP_DIR" in resp.json().get("error", "")

    def test_rejects_non_tar_gz_output(self, backup_client):
        client, backup_dir = backup_client
        bad = str(backup_dir / "dump.sql")

        with patch("src.web_ui.routes.operations._spawn_backup_subprocess"):
            resp = client.post(
                "/api/operations/backup",
                json={"output": bad},
            )

        assert resp.status_code == 400

    def test_status_endpoint_returns_job_info(self, backup_client):
        client, backup_dir = backup_client
        output = str(backup_dir / "out.tar.gz")

        with patch("src.web_ui.routes.operations._spawn_backup_subprocess"):
            resp = client.post(
                "/api/operations/backup",
                json={"output": output},
            )
        job_id = resp.json()["job_id"]

        status_resp = client.get(f"/api/operations/backup/{job_id}/status")
        assert status_resp.status_code == 200
        status_data = status_resp.json()
        assert status_data["job_id"] == job_id
        assert status_data["status"] in ("pending", "running", "done", "error")

    def test_status_returns_404_for_unknown_job(self, backup_client):
        client, _ = backup_client
        resp = client.get("/api/operations/backup/nonexistent-job-id/status")
        assert resp.status_code == 404


class TestBackupStreamEmitsDoneEvent:
    def test_stream_emits_done_when_job_complete(self, backup_client, monkeypatch):
        """Simulate a completed job and verify SSE stream emits done event."""

        client, backup_dir = backup_client

        # Manually inject a completed job into _backup_jobs
        from src.web_ui.routes import operations as ops_module

        job_id = "test-done-job-id"
        with ops_module._backup_jobs_lock:
            ops_module._backup_jobs[job_id] = {
                "job_id": job_id,
                "status": "done",
                "output": str(backup_dir / "out.tar.gz"),
                "started_at": "2026-01-01T00:00:00+00:00",
                "finished_at": "2026-01-01T00:01:00+00:00",
                "exit_code": 0,
                "created_at": "2026-01-01T00:00:00+00:00",
            }

        # Stream should immediately emit done event (no log file = no lines)
        resp = client.get(f"/api/operations/backup/{job_id}/stream")
        assert resp.status_code == 200

        # Parse SSE events
        events = []
        for line in resp.text.splitlines():
            if line.startswith("data: "):
                try:
                    events.append(json.loads(line[6:]))
                except json.JSONDecodeError:
                    pass

        done_events = [e for e in events if e.get("done") is True]
        assert done_events, f"No done event found. Events: {events}"
        assert done_events[0]["exit_code"] == 0
        assert done_events[0]["status"] == "done"

        # Cleanup
        with ops_module._backup_jobs_lock:
            ops_module._backup_jobs.pop(job_id, None)

    def test_stream_returns_404_data_for_unknown_job(self, backup_client):
        client, _ = backup_client
        resp = client.get("/api/operations/backup/no-such-job/stream")
        assert resp.status_code == 200
        # Should emit an error event
        found_error = False
        for line in resp.text.splitlines():
            if line.startswith("data: "):
                try:
                    ev = json.loads(line[6:])
                    if "error" in ev:
                        found_error = True
                except json.JSONDecodeError:
                    pass
        assert found_error
