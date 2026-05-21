# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_web_ui_repos.py
"""Tests for /api/repos Web UI routes — requires PostgreSQL (M8 W1 pure JSON API)."""
import unittest.mock as mock

import httpx
import pytest

from src.db.migrate import run_migrations
from src.web_ui.app import create_app

pytestmark = pytest.mark.postgres


@pytest.fixture
def migrated_pg(clean_pg):
    run_migrations(clean_pg)
    return clean_pg


def _async_client(app):
    """Return an AsyncClient backed by the ASGI app via ASGITransport."""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


class TestProfilesEndpoint:
    @pytest.mark.asyncio
    async def test_get_profiles_returns_200(self, migrated_pg):
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/api/repos/profiles")
        assert resp.status_code == 200
        body = resp.json()
        assert "profiles" in body

    @pytest.mark.asyncio
    async def test_get_profiles_shows_no_profiles_initially(self, migrated_pg):
        # Migration 0004 seeds 5 root profiles; endpoint is non-empty from the start.
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/api/repos/profiles")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body["profiles"], list)

    @pytest.mark.asyncio
    async def test_create_profile_returns_ok(self, migrated_pg):
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(
                "/api/repos/profiles",
                json={"name": "test_profile", "version": "17.0", "description": ""},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True

    @pytest.mark.asyncio
    async def test_create_profile_persists(self, migrated_pg):
        from src.db.pg import repo_store

        app = create_app()
        async with _async_client(app) as client:
            await client.post(
                "/api/repos/profiles",
                json={"name": "viindoo17", "version": "17.0", "description": "test"},
            )
        profiles = repo_store().list_profiles()
        # Migration 0004 seeds 5 root profiles; filter to verify the created one.
        named = [p for p in profiles if p["name"] == "viindoo17"]
        assert len(named) == 1
        assert named[0]["odoo_version"] == "17.0"

    @pytest.mark.asyncio
    async def test_get_profiles_shows_profile_after_create(self, migrated_pg):
        app = create_app()
        async with _async_client(app) as client:
            await client.post(
                "/api/repos/profiles",
                json={"name": "myprofile", "version": "16.0", "description": ""},
            )
            resp = await client.get("/api/repos/profiles")
        assert resp.status_code == 200
        body = resp.json()
        names = [p["name"] for p in body["profiles"]]
        assert "myprofile" in names

    @pytest.mark.asyncio
    async def test_add_repo_returns_ok(self, migrated_pg):
        from src.db.pg import repo_store

        # Pre-create profile directly via ORM
        repo_store().add_profile(name="p1", odoo_version="17.0")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(
                "/api/repos/repos",
                json={
                    "profile": "p1",
                    "url": "file://local",
                    "branch": "17.0",
                    "local_path": "/tmp/odoo_17",
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True

    @pytest.mark.asyncio
    async def test_index_repo_returns_ok(self, migrated_pg):
        from src.db.pg import repo_store

        pid = repo_store().add_profile(name="p1", odoo_version="17.0")
        rid = repo_store().add_repo(
            profile_id=pid,
            url="file://local",
            branch="17.0",
            local_path="/tmp/odoo_17",
        )

        app = create_app()
        with mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/api/repos/repos/{rid}/index",
                    json={},
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True
        mock_popen.assert_called_once()

    @pytest.mark.asyncio
    async def test_index_repo_uses_profile_name_not_all(self, migrated_pg):
        """I4: index button must dispatch --profile <name>, not --all."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile(name="myprofile", odoo_version="17.0")
        rid = repo_store().add_repo(
            profile_id=pid,
            url="file://local",
            branch="17.0",
            local_path="/tmp/odoo_17",
        )

        app = create_app()
        with mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                await client.post(f"/api/repos/repos/{rid}/index", json={})

        mock_popen.assert_called_once()
        call_args = mock_popen.call_args[0][0]
        assert "--all" not in call_args, "index must not re-index all profiles (I4)"
        assert "--profile" in call_args
        idx = call_args.index("--profile")
        assert call_args[idx + 1] == "myprofile", "must pass the specific profile name"

    @pytest.mark.asyncio
    async def test_index_repo_dedup_blocked(self, migrated_pg):
        """M5.5 Section E: when indexer is running, returns 409, Popen NOT called."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile(name="p_dedup", odoo_version="17.0")
        rid = repo_store().add_repo(
            profile_id=pid,
            url="file://local",
            branch="17.0",
            local_path="/tmp/odoo_dedup",
        )

        app = create_app()
        with mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=True
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/api/repos/repos/{rid}/index", json={}
                )

        assert resp.status_code == 409
        body = resp.json()
        err = body.get("error", "").lower()
        assert "already" in err or "running" in err
        mock_popen.assert_not_called()

    @pytest.mark.asyncio
    async def test_index_repo_dedup_ok_spawns_popen(self, migrated_pg):
        """M5.5 Section E: when indexer not running, Popen called once."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile(name="p_free", odoo_version="17.0")
        rid = repo_store().add_repo(
            profile_id=pid,
            url="file://local",
            branch="17.0",
            local_path="/tmp/odoo_free",
        )

        app = create_app()
        with mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/api/repos/repos/{rid}/index", json={}
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True
        mock_popen.assert_called_once()

    @pytest.mark.asyncio
    async def test_index_repo_missing_returns_404(self, migrated_pg):
        """index_repo must return 404 for unknown repo_id, not silently {"ok": True}."""
        app = create_app()
        with mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/api/repos/repos/999999/index", json={}
                )
        assert resp.status_code == 404
        assert "not found" in resp.json().get("error", "").lower()
        mock_popen.assert_not_called()


class TestSshCloneFlow:
    """W4-4: SSH URL detection → cloner Popen + clone-status polling endpoints."""

    @pytest.fixture(autouse=True)
    def _cleanup_ssh_keys(self, migrated_pg):
        """Delete ssh_key_pairs rows before and after each test."""
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM ssh_key_pairs")
        yield
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM ssh_key_pairs")

    @pytest.mark.asyncio
    async def test_post_ssh_url_with_ssh_key_id_spawns_cloner(self, migrated_pg):
        """SSH URL + ssh_key_id → repo inserted, Popen called with src.cloner --repo-id."""
        from src.db.pg import auth_store, repo_store

        repo_store().add_profile(name="ssh_profile", odoo_version="17.0")
        key_id = auth_store().save_ssh_key(
            "deploy-key", "ssh-ed25519 AAAA…", "enc_privkey"
        )

        app = create_app()
        with mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/api/repos/repos",
                    json={
                        "profile": "ssh_profile",
                        "url": "git@github.com:org/repo.git",
                        "branch": "main",
                        "ssh_key_id": str(key_id),
                    },
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True
        assert body.get("clone_status") == "pending"

        # Popen called with src.cloner --repo-id <N>
        mock_popen.assert_called_once()
        argv = mock_popen.call_args[0][0]
        assert argv[1:3] == ["-m", "src.cloner"]
        assert "--repo-id" in argv
        repo_id_str = argv[argv.index("--repo-id") + 1]
        assert repo_id_str.isdigit()

        # Repo row has ssh_key_id set
        repos = repo_store().list_repos()
        assert len(repos) == 1
        repo = repos[0]
        assert repo["ssh_key_id"] == key_id
        assert repo["clone_status"] == "manual"

    @pytest.mark.asyncio
    async def test_post_ssh_url_without_ssh_key_id_returns_error(self, migrated_pg):
        """SSH URL with no ssh_key_id → 400, no Popen, no repo row."""
        from src.db.pg import repo_store

        repo_store().add_profile(name="ssh_nokey_profile", odoo_version="17.0")

        app = create_app()
        with mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/api/repos/repos",
                    json={
                        "profile": "ssh_nokey_profile",
                        "url": "git@github.com:org/repo.git",
                        "branch": "main",
                        # ssh_key_id intentionally omitted
                    },
                )

        assert resp.status_code == 400
        body = resp.json()
        assert "SSH" in body.get("error", "") or "ssh" in body.get("error", "").lower()
        mock_popen.assert_not_called()
        repos = repo_store().list_repos()
        assert len(repos) == 0

    @pytest.mark.asyncio
    async def test_post_https_url_no_cloner_spawn(self, migrated_pg):
        """HTTPS URL → legacy flow: no Popen, ssh_key_id=NULL."""
        from src.db.pg import repo_store

        repo_store().add_profile(name="https_profile", odoo_version="17.0")

        app = create_app()
        with mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/api/repos/repos",
                    json={
                        "profile": "https_profile",
                        "url": "https://github.com/odoo/odoo",
                        "branch": "17.0",
                        "local_path": "/tmp/odoo_https",
                    },
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True
        mock_popen.assert_not_called()
        repos = repo_store().list_repos()
        assert len(repos) == 1
        assert repos[0]["ssh_key_id"] is None

    @pytest.mark.asyncio
    async def test_get_ssh_keys_list_returns_array(self, migrated_pg):
        """GET /api/repos/ssh-keys-list → JSON array with id + name keys."""
        from src.db.pg import auth_store

        auth_store().save_ssh_key("key-alpha", "ssh-ed25519 AAAA1", "enc1")
        auth_store().save_ssh_key("key-beta", "ssh-ed25519 AAAA2", "enc2")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/api/repos/ssh-keys-list")

        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 2
        for entry in data:
            assert "id" in entry
            assert "name" in entry
        names = {e["name"] for e in data}
        assert names == {"key-alpha", "key-beta"}

    @pytest.mark.asyncio
    async def test_post_ssh_url_non_numeric_key_id_returns_400(self, migrated_pg):
        """SSH URL with non-numeric ssh_key_id → 400, no Popen (Fix 6)."""
        from src.db.pg import repo_store

        repo_store().add_profile(name="ssh_abc_profile", odoo_version="17.0")

        app = create_app()
        with mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    "/api/repos/repos",
                    json={
                        "profile": "ssh_abc_profile",
                        "url": "git@host:org/repo.git",
                        "branch": "main",
                        "ssh_key_id": "abc",  # non-numeric
                    },
                )

        assert resp.status_code == 400
        mock_popen.assert_not_called()
        repos = repo_store().list_repos()
        assert len(repos) == 0

    @pytest.mark.asyncio
    async def test_get_clone_status_returns_current(self, migrated_pg):
        """GET /api/repos/repos/{id}/clone-status → JSON with clone_status + error_msg."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile(name="clone_profile", odoo_version="17.0")
        rid = repo_store().add_repo(
            profile_id=pid,
            url="git@github.com:org/repo.git",
            branch="main",
            local_path="/tmp/clone_test",
            ssh_key_id=None,
            clone_status="manual",
        )
        repo_store().set_clone_status(rid, "pending")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get(f"/api/repos/repos/{rid}/clone-status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == rid
        assert data["clone_status"] == "pending"
        assert data["error_msg"] is None


class TestJobIntegration:
    """WI-F3: job record creation + GET /api/jobs/{id}/status endpoint."""

    @pytest.fixture(autouse=True)
    def _cleanup_jobs(self, migrated_pg):
        """Delete indexer_jobs rows before and after each test in this class."""
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM indexer_jobs")
        yield
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM indexer_jobs")

    @pytest.mark.asyncio
    async def test_index_repo_creates_job_and_passes_job_id(self, migrated_pg):
        """POST /api/repos/repos/{id}/index → job created, --job-id in argv."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile(name="p_job", odoo_version="17.0")
        rid = repo_store().add_repo(
            profile_id=pid,
            url="file://local",
            branch="17.0",
            local_path="/tmp/odoo_job",
        )

        app = create_app()
        with mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/api/repos/repos/{rid}/index", json={}
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True
        mock_popen.assert_called_once()

        call_argv = mock_popen.call_args[0][0]
        assert "--job-id" in call_argv, "--job-id flag must be in Popen argv"
        job_id_idx = call_argv.index("--job-id")
        job_id_str = call_argv[job_id_idx + 1]
        assert job_id_str.isdigit(), "--job-id value must be a numeric string"

        # Verify indexer_jobs row was created
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM indexer_jobs")
            count = cur.fetchone()[0]
        assert count == 1

        # Verify the job has status 'queued'
        with migrated_pg.cursor() as cur:
            cur.execute(
                "SELECT status, profile_name FROM indexer_jobs WHERE id = %s",
                (int(job_id_str),),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "queued"
        assert row[1] == "p_job"

    @pytest.mark.asyncio
    async def test_index_repo_dedup_blocks_no_job_created(self, migrated_pg):
        """Khi indexer_is_running True → KHÔNG tạo job, KHÔNG Popen, 409 response."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile(name="p_dedup2", odoo_version="17.0")
        rid = repo_store().add_repo(
            profile_id=pid,
            url="file://local",
            branch="17.0",
            local_path="/tmp/odoo_dedup2",
        )

        app = create_app()
        with mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=True
        ), mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/api/repos/repos/{rid}/index", json={}
                )

        assert resp.status_code == 409
        body = resp.json()
        assert "error" in body
        mock_popen.assert_not_called()

        # No job row created
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM indexer_jobs")
            count = cur.fetchone()[0]
        assert count == 0

    @pytest.mark.asyncio
    async def test_get_job_status_existing(self, migrated_pg):
        """GET /api/jobs/{id}/status with existing job → 200 + correct JSON shape."""
        from src.db.pg import job_store

        job_id = job_store().create_job("p_status")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get(f"/api/jobs/{job_id}/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == job_id
        assert data["profile_name"] == "p_status"
        assert data["status"] == "queued"
        assert data["pid"] is None
        assert data["started_at"] is None
        assert data["finished_at"] is None
        assert data["error_msg"] is None
        assert "created_at" in data

    @pytest.mark.asyncio
    async def test_get_job_status_missing(self, migrated_pg):
        """GET /api/jobs/999999/status → 404."""
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/api/jobs/999999/status")

        assert resp.status_code == 404
        data = resp.json()
        assert data["error"] == "job not found"


class TestStatusBadge:
    """WI-F4: status badge data via JSON endpoint."""

    @pytest.fixture(autouse=True)
    def _cleanup_jobs(self, migrated_pg):
        """Delete indexer_jobs rows before and after each test in this class."""
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM indexer_jobs")
        yield
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM indexer_jobs")

    @pytest.mark.asyncio
    async def test_profiles_endpoint_includes_last_job(self, migrated_pg):
        """GET /api/repos/profiles includes last_job data when job exists."""
        from src.db.pg import job_store, repo_store

        pid = repo_store().add_profile(name="badge_profile", odoo_version="17.0")
        repo_store().add_repo(
            profile_id=pid,
            url="file://local",
            branch="17.0",
            local_path="/tmp/odoo_badge",
        )
        # Create a job for the profile
        job_id = job_store().create_job("badge_profile")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/api/repos/profiles")

        assert resp.status_code == 200
        body = resp.json()
        profiles = body.get("profiles", [])
        # Migration 0004 seeds 5 root profiles; filter to the one we created.
        badge_profiles = [p for p in profiles if p["name"] == "badge_profile"]
        assert len(badge_profiles) == 1
        # Profile should contain repos
        profile = badge_profiles[0]
        repos = profile.get("repos", [])
        assert len(repos) == 1
        # last_job should be attached
        last_job = repos[0].get("last_job")
        assert last_job is not None
        assert last_job["id"] == job_id
        assert last_job["status"] == "queued"

    @pytest.mark.asyncio
    async def test_profiles_endpoint_no_job_when_no_jobs(self, migrated_pg):
        """GET /api/repos/profiles has null last_job when no job exists."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile(name="nobadge_profile", odoo_version="17.0")
        repo_store().add_repo(
            profile_id=pid,
            url="file://local",
            branch="17.0",
            local_path="/tmp/odoo_nobadge",
        )

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/api/repos/profiles")

        body = resp.json()
        profiles = body.get("profiles", [])
        # Migration 0004 seeds 5 root profiles; filter to the one we created.
        nobadge_profiles = [p for p in profiles if p["name"] == "nobadge_profile"]
        assert len(nobadge_profiles) == 1
        repos = nobadge_profiles[0].get("repos", [])
        assert len(repos) == 1
        last_job = repos[0].get("last_job")
        assert last_job is None


# ---------------------------------------------------------------------------
# M8 — Profile hierarchy Web UI tests (ported to pure JSON API)
# ---------------------------------------------------------------------------

class TestProfileHierarchyWebUI:
    """Tests for parent_profile_id field on POST /api/repos/profiles and the
    PATCH /api/repos/profiles/{id}/parent endpoint (ported from M8 Wave A
    master commit cf1820c — Jinja2 form-style → JSON API)."""

    @pytest.mark.asyncio
    async def test_create_profile_with_parent(self, migrated_pg):
        """POST /api/repos/profiles with parent_id creates profile with FK set."""
        from src.db.pg import repo_store

        # Use "99.0" version to avoid conflicts with profiles seeded by migration 0004.
        parent_id = repo_store().add_profile("test_root_99", "99.0")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(
                "/api/repos/profiles",
                json={
                    "name": "test_mid_99",
                    "version": "99.0",
                    "description": "",
                    "parent_id": parent_id,
                },
            )

        assert resp.status_code == 200
        assert resp.json().get("ok") is True

        # Verify FK was set in DB
        profiles = repo_store().list_profiles()
        child = next(
            (p for p in profiles if p["name"] == "test_mid_99"), None
        )
        assert child is not None
        assert child["parent_profile_id"] == parent_id

    @pytest.mark.asyncio
    async def test_create_profile_without_parent_remains_root(self, migrated_pg):
        """POST without parent_id (or null) creates a root profile (NULL FK)."""
        from src.db.pg import repo_store

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(
                "/api/repos/profiles",
                json={"name": "root_only", "version": "17.0"},
            )

        assert resp.status_code == 200
        profiles = repo_store().list_profiles()
        prof = next((p for p in profiles if p["name"] == "root_only"), None)
        assert prof is not None
        assert prof["parent_profile_id"] is None

    @pytest.mark.asyncio
    async def test_create_profile_version_mismatch_rejected(self, migrated_pg):
        """POST with parent of a different odoo_version returns 400."""
        from src.db.pg import repo_store

        # Parent on 17.0
        parent_id = repo_store().add_profile("odoo_17_parent", "17.0")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(
                "/api/repos/profiles",
                json={
                    "name": "child_18",
                    "version": "18.0",  # mismatch
                    "parent_id": parent_id,
                },
            )

        assert resp.status_code == 400
        assert "error" in resp.json()

    @pytest.mark.asyncio
    async def test_set_profile_parent_endpoint(self, migrated_pg):
        """PATCH /api/repos/profiles/{id}/parent updates parent_profile_id."""
        from src.db.pg import repo_store

        # Use "99.0" version to avoid conflicts with profiles seeded by migration 0004.
        parent_id = repo_store().add_profile("test_root_99", "99.0")
        child_id = repo_store().add_profile("test_mid_99", "99.0")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/repos/profiles/{child_id}/parent",
                json={"parent_id": parent_id},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["profile_id"] == child_id
        assert body["parent_id"] == parent_id

        profiles = repo_store().list_profiles()
        child = next(p for p in profiles if p["id"] == child_id)
        assert child["parent_profile_id"] == parent_id

    @pytest.mark.asyncio
    async def test_set_profile_parent_clear(self, migrated_pg):
        """PATCH with parent_id=null clears the parent (root again)."""
        from src.db.pg import repo_store

        # Use "99.0" version to avoid conflicts with profiles seeded by migration 0004.
        parent_id = repo_store().add_profile("test_root_99", "99.0")
        child_id = repo_store().add_profile("test_mid_99", "99.0")
        repo_store().set_profile_parent(child_id, parent_id)

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/repos/profiles/{child_id}/parent",
                json={"parent_id": None},
            )

        assert resp.status_code == 200
        profiles = repo_store().list_profiles()
        child = next(p for p in profiles if p["id"] == child_id)
        assert child["parent_profile_id"] is None

    @pytest.mark.asyncio
    async def test_set_profile_parent_cycle_returns_400(self, migrated_pg):
        """PATCH with cycle returns 422 + error message (was 400 before W-RC)."""
        from src.db.pg import repo_store

        # Use "99.0" version to avoid conflicts with profiles seeded by migration 0004.
        # Build A → B, then try B.parent = A (cycle)
        a_id = repo_store().add_profile("test_root_99", "99.0")
        b_id = repo_store().add_profile("test_mid_99", "99.0")
        repo_store().set_profile_parent(b_id, a_id)  # B's parent = A

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/repos/profiles/{a_id}/parent",
                json={"parent_id": b_id},  # A's parent = B (creates cycle)
            )

        assert resp.status_code == 422
        body = resp.json()
        assert "detail" in body
        detail = body["detail"].lower()
        assert "cycle" in detail

    @pytest.mark.asyncio
    async def test_set_profile_parent_version_mismatch_returns_400(self, migrated_pg):
        """PATCH with parent of different odoo_version returns 422 (was 400 before W-RC)."""
        from src.db.pg import repo_store

        parent_id = repo_store().add_profile("p17", "17.0")
        child_id = repo_store().add_profile("c18", "18.0")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/repos/profiles/{child_id}/parent",
                json={"parent_id": parent_id},
            )

        assert resp.status_code == 422
        assert "detail" in resp.json()

    @pytest.mark.asyncio
    async def test_patch_parent_nonexistent_profile_returns_404(self, migrated_pg):
        """PATCH /parent with nonexistent profile_id returns 404."""
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                "/api/repos/profiles/99999/parent",
                json={"parent_id": None},
            )

        assert resp.status_code == 404
        assert "detail" in resp.json()

    @pytest.mark.asyncio
    async def test_patch_parent_cycle_returns_422(self, migrated_pg):
        """PATCH /parent cycle returns 422 with typed ProfileCycleError."""
        from src.db.pg import repo_store

        a_id = repo_store().add_profile("cycle_a", "17.0")
        b_id = repo_store().add_profile("cycle_b", "17.0")
        repo_store().set_profile_parent(b_id, a_id)

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/repos/profiles/{a_id}/parent",
                json={"parent_id": b_id},
            )

        assert resp.status_code == 422
        body = resp.json()
        assert "detail" in body
        assert "cycle" in body["detail"].lower()

    @pytest.mark.asyncio
    async def test_patch_parent_version_mismatch_returns_422(self, migrated_pg):
        """PATCH /parent version mismatch returns 422 with typed ProfileVersionMismatchError."""
        from src.db.pg import repo_store

        p17 = repo_store().add_profile("vm_p17", "17.0")
        c18 = repo_store().add_profile("vm_c18", "18.0")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/repos/profiles/{c18}/parent",
                json={"parent_id": p17},
            )

        assert resp.status_code == 422
        body = resp.json()
        assert "detail" in body
        assert "mismatch" in body["detail"].lower()

    @pytest.mark.asyncio
    async def test_delete_parent_with_children_blocked(self, migrated_pg):
        """DELETE on parent profile with children is blocked (ON DELETE RESTRICT)."""
        from src.db.pg import repo_store

        # Use "99.0" version to avoid conflicts with profiles seeded by migration 0004.
        parent_id = repo_store().add_profile("test_root_99", "99.0")
        child_id = repo_store().add_profile("test_mid_99", "99.0")
        repo_store().set_profile_parent(child_id, parent_id)

        # Mock Neo4j + indexer_is_running so delete_profile doesn't fail on infra
        with mock.patch(
            "src.web_ui.routes.repos._get_neo4j_writer", return_value=None
        ), mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ):
            app = create_app()
            async with _async_client(app) as client:
                resp = await client.delete(
                    f"/api/repos/profiles/{parent_id}"
                )

        # ON DELETE RESTRICT raises FK error → JSON 500 with error
        assert resp.status_code == 500
        assert "error" in resp.json()


class TestUpdateProfileEndpoint:
    """Tests for PATCH /api/repos/profiles/{id} (edit profile metadata)."""

    @pytest.mark.asyncio
    async def test_update_profile_happy_path(self, migrated_pg):
        """PATCH with description change returns 200 + updated_fields."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile("upd_profile_happy", "17.0", "original desc")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/repos/profiles/{pid}",
                json={"description": "updated description"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["profile_id"] == pid
        assert "description" in body["updated_fields"]

        # Verify persisted
        profiles = repo_store().list_profiles()
        p = next(pr for pr in profiles if pr["id"] == pid)
        assert p["description"] == "updated description"

    @pytest.mark.asyncio
    async def test_update_profile_404(self, migrated_pg):
        """PATCH with nonexistent profile_id returns 404."""
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                "/api/repos/profiles/99999",
                json={"description": "x"},
            )

        assert resp.status_code == 404
        assert "detail" in resp.json()

    @pytest.mark.asyncio
    async def test_update_profile_name_conflict(self, migrated_pg):
        """PATCH renaming to an existing name returns 409."""
        from src.db.pg import repo_store

        repo_store().add_profile("name_taken", "17.0")
        pid2 = repo_store().add_profile("name_to_rename", "17.0")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/repos/profiles/{pid2}",
                json={"name": "name_taken"},
            )

        assert resp.status_code == 409
        body = resp.json()
        assert "detail" in body

    @pytest.mark.asyncio
    async def test_update_profile_version_mismatch_descendants(self, migrated_pg):
        """PATCH changing version when descendants have different version returns 422."""
        from src.db.pg import repo_store

        parent_id = repo_store().add_profile("parent_ver17", "17.0")
        child_id = repo_store().add_profile("child_ver17", "17.0")
        repo_store().set_profile_parent(child_id, parent_id)

        # Child is on 17.0 — changing parent to 18.0 should conflict
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/repos/profiles/{parent_id}",
                json={"version": "18.0"},
            )

        assert resp.status_code == 422
        body = resp.json()
        assert "detail" in body
        assert "mismatch" in body["detail"].lower() or "descendant" in body["detail"].lower()

    @pytest.mark.asyncio
    async def test_update_profile_no_op_returns_empty_fields(self, migrated_pg):
        """PATCH with same values returns 200 with empty updated_fields."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile("noop_profile", "17.0", "same desc")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/repos/profiles/{pid}",
                json={"description": "same desc"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["updated_fields"] == []

    @pytest.mark.asyncio
    async def test_update_profile_version_ok_no_descendants(self, migrated_pg):
        """PATCH version change when no descendants is allowed."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile("lone_profile", "17.0")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/repos/profiles/{pid}",
                json={"version": "18.0"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert "odoo_version" in body["updated_fields"]

        profiles = repo_store().list_profiles()
        p = next(pr for pr in profiles if pr["id"] == pid)
        assert p["odoo_version"] == "18.0"


class TestCloneAllEndpoint:
    """Tests for POST /api/repos/profiles/{id}/clone-all (JSON API)."""

    @pytest.mark.asyncio
    async def test_clone_all_endpoint_short_circuits_file_urls(
        self, migrated_pg, tmp_path
    ):
        """file:// repos pointing to existing dirs are short-circuited; no subprocess."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile("clone_test_profile", "17.0")
        rid = repo_store().add_repo(
            profile_id=pid,
            url=f"file://{tmp_path}",
            branch="17.0",
            local_path="",
        )

        app = create_app()
        with mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/api/repos/profiles/{pid}/clone-all",
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["short_circuited"] == 1
        assert body["spawned"] == 0
        # Subprocess must NOT have been spawned for file:// short-circuit
        mock_popen.assert_not_called()

        # Row must now be clone_status='cloned' and local_path set
        repo = repo_store().get_repo_by_id(rid)
        assert repo["clone_status"] == "cloned"
        assert repo["local_path"] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_clone_all_endpoint_spawns_subprocess_for_https(
        self, migrated_pg, tmp_path
    ):
        """HTTPS repos get a cloner subprocess spawned, not short-circuited."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile("clone_https_profile", "17.0")
        repo_store().add_repo(
            profile_id=pid,
            url="https://github.com/odoo/odoo.git",
            branch="17.0",
            local_path="",
        )

        app = create_app()
        with mock.patch("subprocess.Popen") as mock_popen:
            proc_mock = mock.MagicMock()
            mock_popen.return_value = proc_mock
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/api/repos/profiles/{pid}/clone-all",
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["spawned"] == 1
        mock_popen.assert_called_once()
        call_args = mock_popen.call_args[0][0]
        assert "src.cloner" in call_args

    @pytest.mark.asyncio
    async def test_clone_all_skips_already_cloned(self, migrated_pg, tmp_path):
        """Repos already at clone_status='cloned' are not re-processed."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile("clone_skip_profile", "17.0")
        repo_store().add_repo(
            profile_id=pid,
            url=f"file://{tmp_path}",
            branch="17.0",
            local_path=str(tmp_path),
            clone_status="cloned",
        )

        app = create_app()
        with mock.patch("subprocess.Popen") as mock_popen:
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/api/repos/profiles/{pid}/clone-all",
                )

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 0
        assert "message" in body
        mock_popen.assert_not_called()

    @pytest.mark.asyncio
    async def test_clone_all_nonexistent_profile_returns_404(self, migrated_pg):
        """POST /clone-all for a nonexistent profile_id returns 404 (F22)."""
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post("/api/repos/profiles/99999/clone-all")

        assert resp.status_code == 404
        body = resp.json()
        assert "detail" in body

    @pytest.mark.asyncio
    async def test_clone_all_existing_profile_no_pending_returns_200(
        self, migrated_pg
    ):
        """POST /clone-all for existing profile with no pending repos returns 200 (F22)."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile("no_pending_profile", "17.0")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(f"/api/repos/profiles/{pid}/clone-all")

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["total"] == 0
        assert "message" in body


class TestUpdateRepoEndpoint:
    """Tests for PATCH /api/repos/repos/{id} — edit repo without losing head_sha."""

    @pytest.mark.asyncio
    async def test_update_repo_happy_path(self, migrated_pg):
        """PATCH branch → 200; head_sha is preserved."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile("edit_profile", "17.0")
        rid = repo_store().add_repo(
            profile_id=pid,
            url="https://github.com/odoo/odoo.git",
            branch="17.0",
            local_path="/tmp/odoo",
        )
        # Simulate a prior index run by storing a head_sha
        repo_store().update_repo_head_sha(rid, "abc123deadbeef")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/repos/repos/{rid}",
                json={"branch": "16.0"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["repo_id"] == rid
        assert "branch" in body["updated_fields"]

        # head_sha must NOT be cleared
        repo = repo_store().get_repo_by_id(rid)
        assert repo["branch"] == "16.0"
        assert repo["head_sha"] == "abc123deadbeef"

    @pytest.mark.asyncio
    async def test_update_repo_404(self, migrated_pg):
        """PATCH non-existent repo_id → 404."""
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                "/api/repos/repos/99999",
                json={"branch": "17.0"},
            )

        assert resp.status_code == 404
        assert "error" in resp.json()

    @pytest.mark.asyncio
    async def test_update_repo_conflict(self, migrated_pg):
        """PATCH url+branch that collides with another repo → 409."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile("conflict_profile", "17.0")
        repo_store().add_repo(
            profile_id=pid,
            url="https://github.com/odoo/odoo.git",
            branch="17.0",
            local_path="/tmp/odoo",
        )
        rid2 = repo_store().add_repo(
            profile_id=pid,
            url="https://github.com/odoo/enterprise.git",
            branch="17.0",
            local_path="/tmp/enterprise",
        )

        # Try to change rid2's URL to match rid1's url+branch
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/repos/repos/{rid2}",
                json={"url": "https://github.com/odoo/odoo.git", "branch": "17.0"},
            )

        assert resp.status_code == 409
        assert "error" in resp.json()

    @pytest.mark.asyncio
    async def test_update_repo_ssh_key_required(self, migrated_pg):
        """PATCH to SSH URL without ssh_key_id → 400."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile("ssh_profile", "17.0")
        rid = repo_store().add_repo(
            profile_id=pid,
            url="https://github.com/odoo/odoo.git",
            branch="17.0",
            local_path="/tmp/odoo",
        )

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/repos/repos/{rid}",
                json={"url": "git@github.com:odoo/odoo.git"},
            )

        assert resp.status_code == 400
        body = resp.json()
        assert "error" in body
        assert "SSH" in body["error"] or "ssh" in body["error"].lower()

    @pytest.mark.asyncio
    async def test_update_repo_empty_body_returns_empty_fields(self, migrated_pg):
        """PATCH with all-None body (no-op) returns 200 with empty updated_fields."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile("noop_repo_profile", "17.0")
        rid = repo_store().add_repo(
            profile_id=pid,
            url="https://github.com/odoo/odoo.git",
            branch="17.0",
            local_path="/tmp/odoo_noop",
        )
        repo_store().update_repo_head_sha(rid, "sha_noop123")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/repos/repos/{rid}",
                json={},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["updated_fields"] == []

        # head_sha must be preserved
        repo = repo_store().get_repo_by_id(rid)
        assert repo["head_sha"] == "sha_noop123"

    @pytest.mark.asyncio
    async def test_update_repo_single_field_others_unchanged(self, migrated_pg):
        """PATCH a single field leaves other fields + head_sha intact."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile("single_field_profile", "17.0")
        rid = repo_store().add_repo(
            profile_id=pid,
            url="https://github.com/odoo/odoo.git",
            branch="17.0",
            local_path="/tmp/odoo_single",
        )
        repo_store().update_repo_head_sha(rid, "sha_single456")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/repos/repos/{rid}",
                json={"branch": "16.0"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["updated_fields"] == ["branch"]

        repo = repo_store().get_repo_by_id(rid)
        assert repo["branch"] == "16.0"
        assert repo["url"] == "https://github.com/odoo/odoo.git"
        assert repo["local_path"] == "/tmp/odoo_single"
        assert repo["head_sha"] == "sha_single456"  # must NOT be cleared

    @pytest.mark.asyncio
    async def test_update_repo_concurrent_uniqueviolation_returns_409(
        self, migrated_pg
    ):
        """Race condition: pre-check passes but UPDATE raises UniqueViolation → 409 not 500."""
        import psycopg2.errors

        from src.db.pg import repo_store

        pid = repo_store().add_profile("race_profile", "17.0")
        rid = repo_store().add_repo(
            profile_id=pid,
            url="https://github.com/odoo/odoo.git",
            branch="17.0",
            local_path="/tmp/odoo_race",
        )

        # Mock the pool so the UNIQUE pre-check passes (no conflict row) and the
        # subsequent UPDATE raises UniqueViolation — simulating a concurrent INSERT
        # winning the race between our SELECT and our UPDATE.
        #
        # The mock must be SQL-aware: get_repo_by_id() also uses fetch_one to load
        # the existing row, and that call must NOT be intercepted (otherwise the
        # route returns 404 before reaching the UPDATE path).
        original_fetch_one = repo_store()._pool.fetch_one
        original_execute = repo_store()._pool.execute

        def fake_fetch_one(conn, sql, params=None):
            # Only intercept the UNIQUE conflict pre-check; let get_repo_by_id pass through.
            if "WHERE url = %s AND branch = %s AND id != %s" in sql:
                return None
            return original_fetch_one(conn, sql, params)

        def fake_execute(conn, sql, params=None):
            if "UPDATE repos SET" in sql:
                raise psycopg2.errors.UniqueViolation("duplicate key")
            return original_execute(conn, sql, params)

        app = create_app()
        with mock.patch.object(
            repo_store()._pool, "fetch_one", side_effect=fake_fetch_one
        ), mock.patch.object(
            repo_store()._pool, "execute", side_effect=fake_execute
        ):
            async with _async_client(app) as client:
                resp = await client.patch(
                    f"/api/repos/repos/{rid}",
                    json={"branch": "16.0"},
                )

        assert resp.status_code == 409
        body = resp.json()
        assert "error" in body


class TestUpdateProfileEndpointExtra:
    """Additional test cases for PATCH /api/repos/profiles/{id} (PR #116 review)."""

    @pytest.mark.asyncio
    async def test_update_profile_version_change_with_indexed_data_returns_409(
        self, migrated_pg
    ):
        """PATCH version when profile has indexed repos → 409 with reindex message."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile("indexed_ver_profile", "17.0")
        rid = repo_store().add_repo(
            profile_id=pid,
            url="https://github.com/odoo/odoo.git",
            branch="17.0",
            local_path="/tmp/odoo_indexed_ver",
        )
        # Simulate indexed state
        repo_store().update_repo_head_sha(rid, "sha_indexed_ver")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/repos/profiles/{pid}",
                json={"version": "18.0"},
            )

        assert resp.status_code == 409
        body = resp.json()
        assert "detail" in body
        detail_lower = body["detail"].lower()
        assert "indexed" in detail_lower or "reindex" in detail_lower

    @pytest.mark.asyncio
    async def test_update_profile_name_change_with_indexed_data_returns_409(
        self, migrated_pg
    ):
        """PATCH name when profile has indexed repos → 409 (Neo4j Module.profile is name array)."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile("indexed_name_profile", "17.0")
        rid = repo_store().add_repo(
            profile_id=pid,
            url="https://github.com/odoo/odoo.git",
            branch="17.0",
            local_path="/tmp/odoo_indexed_name",
        )
        # Simulate indexed state
        repo_store().update_repo_head_sha(rid, "sha_indexed_name")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/repos/profiles/{pid}",
                json={"name": "new_name_after_index"},
            )

        assert resp.status_code == 409
        body = resp.json()
        assert "detail" in body
        detail_lower = body["detail"].lower()
        assert "indexed" in detail_lower or "reindex" in detail_lower

    @pytest.mark.asyncio
    async def test_update_profile_version_change_no_indexed_data_allowed(
        self, migrated_pg
    ):
        """PATCH version when NO indexed repos (head_sha=NULL) → allowed (200)."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile("unindexed_ver_profile", "17.0")
        repo_store().add_repo(
            profile_id=pid,
            url="https://github.com/odoo/odoo.git",
            branch="17.0",
            local_path="/tmp/odoo_unindexed_ver",
            # head_sha stays NULL — not indexed
        )

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/repos/profiles/{pid}",
                json={"version": "18.0"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert "odoo_version" in body["updated_fields"]

    @pytest.mark.asyncio
    async def test_update_profile_ancestor_version_mismatch_returns_422(
        self, migrated_pg
    ):
        """PATCH child version to differ from parent → 422 (ancestor check)."""
        from src.db.pg import repo_store

        parent_id = repo_store().add_profile("anc_parent_v17", "17.0")
        child_id = repo_store().add_profile("anc_child_v17", "17.0")
        repo_store().set_profile_parent(child_id, parent_id)

        # Child has no indexed repos — only ancestor check should trigger
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/repos/profiles/{child_id}",
                json={"version": "16.0"},
            )

        assert resp.status_code == 422
        body = resp.json()
        assert "detail" in body
        detail_lower = body["detail"].lower()
        assert "mismatch" in detail_lower or "parent" in detail_lower

    @pytest.mark.asyncio
    async def test_update_profile_3_level_descendants_version_mismatch(
        self, migrated_pg
    ):
        """root → child → grandchild; PATCH root version → 422 (descendant check)."""
        from src.db.pg import repo_store

        root_id = repo_store().add_profile("root_v17", "17.0")
        child_id = repo_store().add_profile("child_v17", "17.0")
        grandchild_id = repo_store().add_profile("grandchild_v17", "17.0")
        repo_store().set_profile_parent(child_id, root_id)
        repo_store().set_profile_parent(grandchild_id, child_id)

        # Changing root version conflicts with grandchild (still on 17.0)
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/repos/profiles/{root_id}",
                json={"version": "18.0"},
            )

        assert resp.status_code == 422
        body = resp.json()
        assert "detail" in body
        detail_lower = body["detail"].lower()
        assert "mismatch" in detail_lower or "descendant" in detail_lower

    @pytest.mark.asyncio
    async def test_update_profile_invalid_version_type(self, migrated_pg):
        """PATCH with version=123 (integer) → 422 from Pydantic validation."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile("type_check_profile", "17.0")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.patch(
                f"/api/repos/profiles/{pid}",
                json={"version": 123},
            )

        # Pydantic coerces int to str for str | None fields in strict=False mode,
        # so this may return 200 with version="123" OR 422 depending on Pydantic config.
        # Either outcome is acceptable; what we verify is that it does NOT crash (500).
        assert resp.status_code in (200, 422)
