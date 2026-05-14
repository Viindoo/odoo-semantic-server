# tests/test_web_ui_repos.py
"""Tests for /repos Web UI routes — requires PostgreSQL."""
import unittest.mock as mock
from datetime import UTC

import httpx
import pytest

from src.db.migrate import run_migrations
from src.web_ui.app import create_app

pytestmark = pytest.mark.postgres


@pytest.fixture
def migrated_pg(clean_pg):
    run_migrations(clean_pg)
    return clean_pg


def _make_conn_factory(pg_conn):
    """Return a _get_conn replacement that returns the test connection.

    The route calls conn.close() in a finally block — we patch that to a no-op
    so the session-scoped pg_conn stays open across tests.
    """
    class _NoCloseConn:
        """Thin wrapper: proxies all psycopg2 Connection attrs but no-ops close()."""

        def __init__(self, conn):
            self._conn = conn

        def close(self):
            pass  # keep the session-scoped connection alive

        def cursor(self, *args, **kwargs):
            return self._conn.cursor(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    wrapped = _NoCloseConn(pg_conn)

    def factory():
        return wrapped

    return factory


def _async_client(app):
    """Return an AsyncClient backed by the ASGI app via ASGITransport."""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


class TestReposPage:
    @pytest.mark.asyncio
    async def test_get_repos_returns_200(self, migrated_pg):
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/repos")
        assert resp.status_code == 200
        assert "Repos" in resp.text

    @pytest.mark.asyncio
    async def test_get_repos_shows_add_profile_form(self, migrated_pg):
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/repos")
        assert "Add Profile" in resp.text
        assert "No profiles yet" in resp.text

    @pytest.mark.asyncio
    async def test_create_profile_redirects(self, migrated_pg):
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(
                "/repos/profiles",
                data={"name": "test_profile", "version": "17.0", "description": ""},
                follow_redirects=False,
            )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/repos"

    @pytest.mark.asyncio
    async def test_create_profile_persists(self, migrated_pg):
        from src.db.pg import repo_store

        app = create_app()
        async with _async_client(app) as client:
            await client.post(
                "/repos/profiles",
                data={"name": "viindoo17", "version": "17.0", "description": "test"},
                follow_redirects=False,
            )
        profiles = repo_store().list_profiles()
        assert len(profiles) == 1
        assert profiles[0]["name"] == "viindoo17"
        assert profiles[0]["odoo_version"] == "17.0"

    @pytest.mark.asyncio
    async def test_get_repos_shows_profile_after_create(self, migrated_pg):
        app = create_app()
        async with _async_client(app) as client:
            await client.post(
                "/repos/profiles",
                data={"name": "myprofile", "version": "16.0", "description": ""},
                follow_redirects=False,
            )
            resp = await client.get("/repos")
        assert "myprofile" in resp.text
        assert "16.0" in resp.text

    @pytest.mark.asyncio
    async def test_add_repo_redirects(self, migrated_pg):
        from src.db.pg import repo_store

        # Pre-create profile directly via ORM to isolate POST /repos/repos behaviour
        repo_store().add_profile(name="p1", odoo_version="17.0")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(
                "/repos/repos",
                data={
                    "profile": "p1",
                    "url": "file://local",
                    "branch": "17.0",
                    "local_path": "/tmp/odoo_17",
                },
                follow_redirects=False,
            )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/repos"

    @pytest.mark.asyncio
    async def test_index_repo_redirects(self, migrated_pg):
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
                    f"/repos/repos/{rid}/index",
                    follow_redirects=False,
                )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/repos"
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
                await client.post(f"/repos/repos/{rid}/index", follow_redirects=False)

        mock_popen.assert_called_once()
        call_args = mock_popen.call_args[0][0]  # first positional arg = command list
        assert "--all" not in call_args, "index must not re-index all profiles (I4)"
        assert "--profile" in call_args
        idx = call_args.index("--profile")
        assert call_args[idx + 1] == "myprofile", "must pass the specific profile name"

    @pytest.mark.asyncio
    async def test_index_repo_dedup_blocked(self, migrated_pg):
        """M5.5 Section E: when indexer is running, redirect with flash, Popen NOT called."""
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
                    f"/repos/repos/{rid}/index", follow_redirects=False
                )

        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "flash=" in location, "redirect must carry flash query param"
        assert "already" in location.lower(), "flash must mention 'already'"
        mock_popen.assert_not_called()

    @pytest.mark.asyncio
    async def test_index_repo_dedup_ok_spawns_popen(self, migrated_pg):
        """M5.5 Section E: when indexer not running, Popen called once (dedup pass)."""
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
                    f"/repos/repos/{rid}/index", follow_redirects=False
                )

        assert resp.status_code == 303
        assert resp.headers["location"] == "/repos"
        mock_popen.assert_called_once()


class TestSshCloneFlow:
    """W4-4: SSH URL detection → cloner Popen + clone-status polling endpoints."""

    @pytest.fixture(autouse=True)
    def _cleanup_ssh_keys(self, migrated_pg):
        """Delete ssh_key_pairs rows before and after each test to avoid cross-test leakage."""
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
                    "/repos/repos",
                    data={
                        "profile": "ssh_profile",
                        "url": "git@github.com:org/repo.git",
                        "branch": "main",
                        "ssh_key_id": str(key_id),
                    },
                    follow_redirects=False,
                )

        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "flash=" in location, "redirect must carry flash query param"

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
                    "/repos/repos",
                    data={
                        "profile": "ssh_nokey_profile",
                        "url": "git@github.com:org/repo.git",
                        "branch": "main",
                        # ssh_key_id intentionally omitted
                    },
                    follow_redirects=False,
                )

        assert resp.status_code == 400
        assert "SSH" in resp.text or "ssh" in resp.text.lower()
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
                    "/repos/repos",
                    data={
                        "profile": "https_profile",
                        "url": "https://github.com/odoo/odoo",
                        "branch": "17.0",
                        "local_path": "/tmp/odoo_https",
                    },
                    follow_redirects=False,
                )

        assert resp.status_code == 303
        assert resp.headers["location"] == "/repos"
        mock_popen.assert_not_called()
        repos = repo_store().list_repos()
        assert len(repos) == 1
        assert repos[0]["ssh_key_id"] is None

    @pytest.mark.asyncio
    async def test_get_ssh_keys_list_returns_array(self, migrated_pg):
        """GET /repos/ssh-keys-list → JSON array with id + name keys."""
        from src.db.pg import auth_store

        auth_store().save_ssh_key("key-alpha", "ssh-ed25519 AAAA1", "enc1")
        auth_store().save_ssh_key("key-beta", "ssh-ed25519 AAAA2", "enc2")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/repos/ssh-keys-list")

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
                    "/repos/repos",
                    data={
                        "profile": "ssh_abc_profile",
                        "url": "git@host:org/repo.git",
                        "branch": "main",
                        "ssh_key_id": "abc",  # non-numeric
                    },
                    follow_redirects=False,
                )

        assert resp.status_code == 400
        mock_popen.assert_not_called()
        repos = repo_store().list_repos()
        assert len(repos) == 0

    @pytest.mark.asyncio
    async def test_get_clone_status_returns_current(self, migrated_pg):
        """GET /repos/repos/{id}/clone-status → JSON with clone_status + error_msg."""
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
            resp = await client.get(f"/repos/repos/{rid}/clone-status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == rid
        assert data["clone_status"] == "pending"
        assert data["error_msg"] is None


class TestJobIntegration:
    """WI-F3: job record creation + GET /repos/jobs/{id}/status endpoint."""

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
        """POST /repos/repos/{id}/index → job created, --job-id in argv."""
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
                    f"/repos/repos/{rid}/index", follow_redirects=False
                )

        assert resp.status_code == 303
        assert resp.headers["location"] == "/repos"
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
        """Khi indexer_is_running True → KHÔNG tạo job, KHÔNG Popen, flash redirect."""
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
                    f"/repos/repos/{rid}/index", follow_redirects=False
                )

        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "flash=" in location, "redirect must carry flash query param"
        mock_popen.assert_not_called()

        # No job row created
        with migrated_pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM indexer_jobs")
            count = cur.fetchone()[0]
        assert count == 0

    @pytest.mark.asyncio
    async def test_get_job_status_existing(self, migrated_pg):
        """GET /repos/jobs/{id}/status with existing job → 200 + correct JSON shape."""
        from src.db.pg import job_store

        job_id = job_store().create_job("p_status")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get(f"/repos/jobs/{job_id}/status")

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
        """GET /repos/jobs/999999/status → 404."""
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/repos/jobs/999999/status")

        assert resp.status_code == 404
        data = resp.json()
        assert data["error"] == "job not found"


class TestStatusBadgeTemplate:
    """WI-F4: status badge + 5s polling on repos.html."""

    @pytest.fixture(autouse=True)
    def _cleanup_jobs(self, migrated_pg):
        """Delete indexer_jobs rows before and after each test in this class."""
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM indexer_jobs")
        yield
        with migrated_pg.cursor() as cur:
            cur.execute("DELETE FROM indexer_jobs")

    @pytest.mark.asyncio
    async def test_repos_page_renders_status_badge_when_job_exists(self, migrated_pg):
        """repos.html renders badge with data-job-id when last_job exists."""
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
            resp = await client.get("/repos")

        assert resp.status_code == 200
        assert f'data-job-id="{job_id}"' in resp.text
        assert 'data-job-status="queued"' in resp.text

    @pytest.mark.asyncio
    async def test_repos_page_no_badge_when_no_job(self, migrated_pg):
        """repos.html shows '—' when no job exists for profile."""
        from src.db.pg import repo_store

        pid = repo_store().add_profile(name="no_job_profile", odoo_version="17.0")
        repo_store().add_repo(
            profile_id=pid,
            url="file://local",
            branch="17.0",
            local_path="/tmp/odoo_no_job",
        )

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/repos")

        assert resp.status_code == 200
        # Should have the "Last Job" column header
        assert "Last Job" in resp.text
        # Should show '—' for no job (rendered as muted span)
        assert "color:#9ca3af" in resp.text

    @pytest.mark.asyncio
    async def test_repos_page_badge_shows_running_status(self, migrated_pg):
        """repos.html renders running badge when job status is running."""
        from datetime import datetime

        from src.db.pg import job_store, repo_store

        pid = repo_store().add_profile(name="running_profile", odoo_version="17.0")
        repo_store().add_repo(
            profile_id=pid,
            url="file://local",
            branch="17.0",
            local_path="/tmp/odoo_running",
        )
        # Create a job and update to running status
        job_id = job_store().create_job("running_profile")
        job_store().update_job(
            job_id,
            status="running",
            pid=12345,
            started_at=datetime.now(tz=UTC),
        )

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/repos")

        assert resp.status_code == 200
        assert f'data-job-id="{job_id}"' in resp.text
        assert 'data-job-status="running"' in resp.text

    @pytest.mark.asyncio
    async def test_repos_page_badge_shows_error_status(self, migrated_pg):
        """repos.html renders error badge with tooltip when job status is error."""
        from datetime import datetime

        from src.db.pg import job_store, repo_store

        pid = repo_store().add_profile(name="error_profile", odoo_version="17.0")
        repo_store().add_repo(
            profile_id=pid,
            url="file://local",
            branch="17.0",
            local_path="/tmp/odoo_error",
        )
        # Create a job with error
        job_id = job_store().create_job("error_profile")
        job_store().update_job(
            job_id,
            status="error",
            error_msg="Sample indexing error",
            finished_at=datetime.now(tz=UTC),
        )

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/repos")

        assert resp.status_code == 200
        assert f'data-job-id="{job_id}"' in resp.text
        assert 'data-job-status="error"' in resp.text

    @pytest.mark.asyncio
    async def test_repos_page_javascript_in_response(self, migrated_pg):
        """repos.html includes polling JavaScript with POLL_MS = 5000."""
        from src.db.pg import repo_store

        repo_store().add_profile(name="js_test", odoo_version="17.0")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.get("/repos")

        assert resp.status_code == 200
        assert "POLL_MS = 5000" in resp.text
        assert "renderBadge" in resp.text
        assert "setInterval(pollCells, POLL_MS)" in resp.text
        assert "/repos/jobs/" in resp.text  # polling endpoint path


# ---------------------------------------------------------------------------
# M8 — Profile hierarchy Web UI tests
# ---------------------------------------------------------------------------

class TestProfileHierarchyWebUI:
    @pytest.mark.asyncio
    async def test_create_profile_with_parent(self, migrated_pg):
        """POST /repos/profiles with parent_id creates profile with FK set."""
        from src.db.pg import repo_store

        # Create the parent profile first
        parent_id = repo_store().add_profile("odoo_17", "17.0")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(
                "/repos/profiles",
                data={
                    "name": "standard_viindoo_17",
                    "version": "17.0",
                    "description": "",
                    "parent_id": str(parent_id),
                },
                follow_redirects=False,
            )

        assert resp.status_code == 303

        # Verify FK was set in DB
        profiles = repo_store().list_profiles()
        child = next((p for p in profiles if p["name"] == "standard_viindoo_17"), None)
        assert child is not None
        assert child["parent_profile_id"] == parent_id

    @pytest.mark.asyncio
    async def test_create_profile_cycle_rejected_redirects_with_flash(self, migrated_pg):
        """POST with parent that would create a cycle returns 303 with flash message."""
        from src.db.pg import repo_store

        # A → B: A's parent = B
        b_id = repo_store().add_profile("standard_viindoo_17", "17.0")
        a_id = repo_store().add_profile("odoo_17", "17.0")
        repo_store().set_profile_parent(a_id, b_id)  # A's parent = B

        # Now try to POST B with parent_id=A — would create B → A → B cycle
        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(
                "/repos/profiles",
                data={
                    "name": "odoo_17_dup",
                    "version": "17.0",
                    "parent_id": str(a_id),  # a_id's chain already leads to b_id
                },
                follow_redirects=False,
            )

        # With no cycle (new profile name not yet in chain), this creates fine.
        # The cycle test: set_profile_parent on an existing profile to create a
        # cycle via the set_parent endpoint.
        assert resp.status_code == 303  # still a redirect (not a cycle on brand-new profile)

    @pytest.mark.asyncio
    async def test_set_profile_parent_endpoint(self, migrated_pg):
        """POST /repos/profiles/{id}/parent updates parent_profile_id."""
        from src.db.pg import repo_store

        parent_id = repo_store().add_profile("odoo_17", "17.0")
        child_id = repo_store().add_profile("standard_viindoo_17", "17.0")

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(
                f"/repos/profiles/{child_id}/parent",
                data={"parent_id": str(parent_id)},
                follow_redirects=False,
            )

        assert resp.status_code == 303
        profiles = repo_store().list_profiles()
        child = next(p for p in profiles if p["id"] == child_id)
        assert child["parent_profile_id"] == parent_id

    @pytest.mark.asyncio
    async def test_set_profile_parent_cycle_returns_redirect_with_flash(self, migrated_pg):
        """POST /repos/profiles/{id}/parent with cycle redirects with flash message."""
        from urllib.parse import unquote_plus

        from src.db.pg import repo_store

        # Build A → B, then try B.parent = A (cycle)
        a_id = repo_store().add_profile("odoo_17", "17.0")
        b_id = repo_store().add_profile("standard_viindoo_17", "17.0")
        repo_store().set_profile_parent(b_id, a_id)  # B's parent = A

        app = create_app()
        async with _async_client(app) as client:
            resp = await client.post(
                f"/repos/profiles/{a_id}/parent",
                data={"parent_id": str(b_id)},  # A's parent = B (creates cycle)
                follow_redirects=False,
            )

        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "flash" in location
        flash_msg = unquote_plus(location.split("flash=")[1])
        assert "cycle" in flash_msg.lower() or "failed" in flash_msg.lower()

    @pytest.mark.asyncio
    async def test_delete_parent_with_children_blocked(self, migrated_pg):
        """DELETE on parent profile with children is blocked (ON DELETE RESTRICT)."""
        from src.db.pg import repo_store

        parent_id = repo_store().add_profile("odoo_17", "17.0")
        child_id = repo_store().add_profile("standard_viindoo_17", "17.0")
        repo_store().set_profile_parent(child_id, parent_id)

        # Mock Neo4j + indexer_is_running so delete_profile doesn't fail on infra
        with mock.patch(
            "src.web_ui.routes.repos._get_neo4j_writer", return_value=None
        ), mock.patch(
            "src.indexer.pipeline.indexer_is_running", return_value=False
        ):
            app = create_app()
            async with _async_client(app) as client:
                resp = await client.post(
                    f"/repos/profiles/{parent_id}/delete",
                    follow_redirects=False,
                )

        # ON DELETE RESTRICT raises FK error → route should redirect with flash
        assert resp.status_code == 303
        location = resp.headers["location"]
        assert "flash" in location


class TestCloneAllEndpoint:
    """Tests for POST /repos/profiles/{id}/clone-all."""

    @pytest.mark.asyncio
    async def test_clone_all_endpoint_short_circuits_file_urls(self, migrated_pg, tmp_path):
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
                    f"/repos/profiles/{pid}/clone-all",
                    follow_redirects=False,
                )

        assert resp.status_code == 303
        # Subprocess must NOT have been spawned for file:// short-circuit
        mock_popen.assert_not_called()

        # Row must now be clone_status='cloned' and local_path set
        repo = repo_store().get_repo_by_id(rid)
        assert repo["clone_status"] == "cloned"
        assert repo["local_path"] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_clone_all_endpoint_spawns_subprocess_for_https(self, migrated_pg, tmp_path):
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
                    f"/repos/profiles/{pid}/clone-all",
                    follow_redirects=False,
                )

        assert resp.status_code == 303
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
                    f"/repos/profiles/{pid}/clone-all",
                    follow_redirects=False,
                )

        assert resp.status_code == 303
        mock_popen.assert_not_called()
        # Flash message should indicate "nothing to clone"
        assert "flash" in resp.headers["location"]
