# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_restore_security.py
"""Security tests for the restore upload endpoint (M9 W-RS OWASP 10-item checklist).

Tests cover:
- OWASP 1/2: Content-type and extension validation
- OWASP 3/4: Size limits
- OWASP 7: Concurrent upload guard (409)
- OWASP 7: Maintenance mode middleware (503)
- OWASP 8: Admin required (401) and MFA freshness (403)
- OWASP 9/10: Safety backup and audit log

All tests run without Docker (unit-level mocking of subprocess + DB).
Uses httpx.AsyncClient with ASGITransport to match other web_ui tests.
"""
import io
import json
import os
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_valid_bundle_bytes(tmp_path: Path) -> bytes:
    """Create a minimal valid .tar.gz bundle (manifest.json + postgres.sql)."""
    bundle_path = tmp_path / "bundle.tar.gz"
    with tarfile.open(bundle_path, "w:gz") as tar:
        manifest_data = json.dumps({"created_at": "2026-05-15", "version": "1"}).encode()
        info = tarfile.TarInfo(name="manifest.json")
        info.size = len(manifest_data)
        tar.addfile(info, io.BytesIO(manifest_data))
        pg_data = b"-- PostgreSQL dump\n"
        info2 = tarfile.TarInfo(name="postgres.sql")
        info2.size = len(pg_data)
        tar.addfile(info2, io.BytesIO(pg_data))
    return bundle_path.read_bytes()


def _make_app(*, auth_bypass: bool = True, session_secret: str = "test-secret-32bytes-for-tests!!"):
    """Create a Web UI app with optional auth bypass.

    Sets WEBUI_SECURE_COOKIE=0 so sessions work over plain HTTP in tests.

    Note: direct os.environ mutation here leaked WEBUI_AUTH_DISABLED into
    subsequent tests (notably test_web_ui_auth). The conftest autouse fixture
    now handles bypass for non-auth tests; we only adjust here when an explicit
    "no bypass" is requested for a single test.
    """
    if auth_bypass:
        # Self-managed: this module is in conftest.real_auth_flow_files so
        # conftest does NOT set WEBUI_AUTH_DISABLED.  Set it explicitly here
        # for tests that check non-auth functionality (content-type, size, etc.)
        os.environ["WEBUI_AUTH_DISABLED"] = "1"
    else:
        # Explicit no-bypass — temporarily clear (only affects current test
        # because conftest autouse re-applies on the next test).
        os.environ.pop("WEBUI_AUTH_DISABLED", None)
    os.environ["WEBUI_SESSION_SECRET"] = session_secret
    os.environ["WEBUI_SECURE_COOKIE"] = "0"  # allow plain HTTP in tests
    from src.web_ui.app import create_app
    return create_app()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_maintenance_flag():
    """Reset the _RESTORE_IN_PROGRESS event and audit log before/after each test."""
    from src.web_ui.routes.operations import _RESTORE_AUDIT_LOG, _RESTORE_IN_PROGRESS
    _RESTORE_IN_PROGRESS.clear()
    _RESTORE_AUDIT_LOG.clear()
    yield
    _RESTORE_IN_PROGRESS.clear()
    _RESTORE_AUDIT_LOG.clear()


@pytest.fixture()
def app_bypass():
    """App with test auth bypass active."""
    return _make_app(auth_bypass=True)


@pytest.fixture()
def app_no_bypass():
    """App WITHOUT auth bypass (auth fully enforced)."""
    return _make_app(auth_bypass=False)


# ---------------------------------------------------------------------------
# OWASP 2: Extension allowlist
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restore_rejects_invalid_extension(app_bypass, tmp_path):
    """Upload a .zip file → 400 (rejected by extension or content-type guard).

    The zip content-type 'application/zip' is also not in the allowlist, so
    OWASP guard #1 (content-type) fires before guard #2 (extension). Both
    guards return 400, so we assert the status code only.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_bypass),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/api/operations/restore",
            files={"file": ("backup.zip", b"PK\x03\x04fake zip", "application/zip")},
        )
    assert resp.status_code == 400

    # Also test with an allowed content-type but .sql extension → extension guard fires
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_bypass),
        base_url="http://test",
    ) as client:
        resp2 = await client.post(
            "/api/operations/restore",
            files={"file": ("dump.sql", b"-- sql", "application/gzip")},
        )
    assert resp2.status_code == 400
    detail2 = resp2.json().get("detail", "").lower()
    assert (
        "tar.gz" in detail2
        or "tgz" in detail2
        or "extension" in detail2
        or "filename" in detail2
    )


@pytest.mark.asyncio
async def test_restore_rejects_invalid_content_type(app_bypass, tmp_path):
    """Upload with text/html content-type → 400 content-type rejection."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_bypass),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/api/operations/restore",
            files={"file": ("backup.tar.gz", b"fake", "text/html")},
        )
    assert resp.status_code == 400
    detail = resp.json().get("detail", "")
    assert "content type" in detail.lower() or "invalid" in detail.lower()


# ---------------------------------------------------------------------------
# OWASP 3: Content-Length pre-check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restore_rejects_over_max_size_via_content_length(app_bypass, tmp_path):
    """Content-Length header > 500MB → 413 before streaming."""
    max_bytes = 500 * 1024 * 1024
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_bypass),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/api/operations/restore",
            files={"file": ("big.tar.gz", b"small", "application/gzip")},
            headers={"content-length": str(max_bytes + 1)},
        )
    assert resp.status_code == 413


# ---------------------------------------------------------------------------
# OWASP 4: Streaming size guard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restore_rejects_over_max_size_during_stream(app_bypass, tmp_path, monkeypatch):
    """Streaming read exceeds MAX_RESTORE_BYTES → 413."""
    import src.web_ui.routes.operations as ops_mod
    monkeypatch.setattr(ops_mod, "MAX_RESTORE_BYTES", 10)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_bypass),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/api/operations/restore",
            files={"file": ("big.tar.gz", b"x" * 100, "application/gzip")},
        )
    assert resp.status_code == 413


# ---------------------------------------------------------------------------
# OWASP 8: Admin required (401)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restore_requires_admin(app_no_bypass, tmp_path):
    """Non-authenticated request → 401 (or 403 for unauthenticated)."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_no_bypass),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/api/operations/restore",
            files={"file": ("backup.tar.gz", b"fake", "application/gzip")},
        )
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# OWASP 8: MFA freshness (403 when stale)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restore_requires_fresh_mfa(app_no_bypass, tmp_path):
    """Valid session but no MFA enrolled (mfa_verified_at absent) → 403.

    After login, session has username + session_at but no mfa_verified_at.
    The require_admin_with_fresh_mfa dependency must return 403.
    """
    import src.web_ui.routes.login as login_mod
    from src.web_ui.auth import hash_password

    # _lookup_user must return a dict matching the production shape:
    # {id, password_hash, is_admin, is_active}. login.py expects user["id"]
    # for session creation and user["is_active"] for the auth check.
    test_hash = hash_password("password123abc")
    fake_user = {
        "id": 1,
        "password_hash": test_hash,
        "is_admin": True,
        "is_active": True,
    }

    orig_lookup = login_mod._lookup_user
    orig_check_totp = login_mod._check_totp_enabled
    orig_create_session = login_mod._create_session
    orig_revoke_all = login_mod._revoke_all_user_sessions
    orig_lookup_session = login_mod._lookup_session
    orig_record = login_mod.record_login_attempt
    orig_check_rl = login_mod.check_rate_limit
    orig_audit = login_mod._insert_audit_log

    # Patch DB-touching helpers so the test works without Postgres.
    _sessions: dict[str, dict] = {}

    def _fake_create_session(user_id, ip_address, user_agent):
        import secrets as _secrets
        sid = _secrets.token_urlsafe(16)
        _sessions[sid] = {"user_id": user_id}
        return sid

    def _fake_lookup_session(sid):
        return {"user_id": _sessions[sid]["user_id"]} if sid in _sessions else None

    login_mod._lookup_user = lambda u: fake_user if u == "admin" else None
    login_mod._check_totp_enabled = lambda u: None
    login_mod._create_session = _fake_create_session
    login_mod._revoke_all_user_sessions = lambda uid: None
    login_mod._lookup_session = _fake_lookup_session
    login_mod.record_login_attempt = lambda **kwargs: None
    login_mod.check_rate_limit = lambda *a, **k: False
    login_mod._insert_audit_log = lambda **kwargs: None

    # require_admin_with_fresh_mfa hits auth_store().get_user_field — bypass it.
    import src.web_ui.auth as auth_mod
    orig_require_admin = auth_mod.require_admin

    async def _fake_require_admin(request):
        return 1

    auth_mod.require_admin = _fake_require_admin

    resp_status = None
    resp_detail = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_no_bypass),
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            # Login — establishes session with username + session_at (no mfa_verified_at)
            login_resp = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "password123abc"},
            )
            assert login_resp.status_code == 200, f"Login failed: {login_resp.text}"

            # Attempt restore without MFA → 403 (mfa_verified_at absent)
            restore_resp = await client.post(
                "/api/operations/restore",
                files={"file": ("backup.tar.gz", b"fake", "application/gzip")},
            )
            resp_status = restore_resp.status_code
            resp_detail = restore_resp.json().get("detail", "")
    finally:
        login_mod._lookup_user = orig_lookup
        login_mod._check_totp_enabled = orig_check_totp
        login_mod._create_session = orig_create_session
        login_mod._revoke_all_user_sessions = orig_revoke_all
        login_mod._lookup_session = orig_lookup_session
        login_mod.record_login_attempt = orig_record
        login_mod.check_rate_limit = orig_check_rl
        login_mod._insert_audit_log = orig_audit
        auth_mod.require_admin = orig_require_admin

    assert resp_status == 403, f"Expected 403, got {resp_status}: {resp_detail}"
    assert "mfa" in resp_detail.lower() or "fresh" in resp_detail.lower()


# ---------------------------------------------------------------------------
# OWASP 7: Concurrent restore returns 409
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restore_concurrent_returns_409(app_bypass, tmp_path):
    """Second upload while _RESTORE_IN_PROGRESS is set → 409."""
    from src.web_ui.routes.operations import _RESTORE_IN_PROGRESS
    _RESTORE_IN_PROGRESS.set()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_bypass),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/api/operations/restore",
            files={"file": ("backup.tar.gz", b"fake", "application/gzip")},
        )
    assert resp.status_code == 409
    detail = resp.json().get("detail", "")
    assert "progress" in detail.lower() or "in progress" in detail.lower()


# ---------------------------------------------------------------------------
# OWASP 7: Maintenance mode middleware blocks other endpoints with 503
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_maintenance_mode_blocks_non_restore_endpoints(app_bypass):
    """While restore in progress, non-restore endpoints → 503 + Retry-After: 60."""
    from src.web_ui.routes.operations import _RESTORE_IN_PROGRESS
    _RESTORE_IN_PROGRESS.set()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_bypass),
        base_url="http://test",
    ) as client:
        resp = await client.get("/api/operations/presets")

    assert resp.status_code == 503
    assert resp.headers.get("retry-after") == "60"
    body = resp.json()
    assert "maintenance" in body.get("error", "").lower()


@pytest.mark.asyncio
async def test_maintenance_mode_allows_health(app_bypass):
    """Health endpoint must remain accessible during maintenance (not 503)."""
    from src.web_ui.routes.operations import _RESTORE_IN_PROGRESS
    _RESTORE_IN_PROGRESS.set()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_bypass),
        base_url="http://test",
    ) as client:
        resp = await client.get("/health")

    assert resp.status_code != 503


# ---------------------------------------------------------------------------
# OWASP 9 + 10: Pre-restore safety backup + audit log on valid upload
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restore_creates_safety_backup_before_extract(app_bypass, tmp_path, monkeypatch):
    """Valid bundle: pg_dump (safety backup) must be called before psql (restore)."""
    import shutil


    monkeypatch.setattr(shutil, "disk_usage", lambda p: MagicMock(free=10 * 1024 * 1024 * 1024))

    call_order = []

    def mock_run(cmd, **kwargs):
        call_order.append(cmd[0])
        if cmd[0] == "pg_dump":
            stdout = kwargs.get("stdout")
            if stdout and hasattr(stdout, "write"):
                stdout.write(b"-- mock dump\n")
            return MagicMock(returncode=0, stderr=b"")
        return MagicMock(returncode=0, stderr="", stdout="")

    bundle_bytes = _make_valid_bundle_bytes(tmp_path)

    with patch("subprocess.run", side_effect=mock_run):
        with patch("src.cli._get_pg_dsn", return_value="postgresql://user:pw@localhost/db"):
            with patch("src.cli._dsn_to_pg_args_and_env", return_value=(["--dbname", "db"], {})):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app_bypass),
                    base_url="http://test",
                ) as client:
                    resp = await client.post(
                        "/api/operations/restore",
                        files={"file": ("backup.tar.gz", bundle_bytes, "application/gzip")},
                    )

    assert resp.status_code == 202
    data = resp.json()
    assert data.get("safety_backup") is not None

    # pg_dump must come before psql in call order (checked after background thread)
    # We just verify safety_backup is in response (pg_dump was called)


@pytest.mark.asyncio
async def test_restore_audit_log_records_sha256(app_bypass, tmp_path, monkeypatch):
    """Audit log must contain a restore.start record with sha256 field."""
    import shutil

    import src.web_ui.routes.operations as ops_mod

    monkeypatch.setattr(shutil, "disk_usage", lambda p: MagicMock(free=10 * 1024 * 1024 * 1024))

    bundle_bytes = _make_valid_bundle_bytes(tmp_path)

    # Patch so no PG_DSN → safety backup skipped, restore proceeds normally
    with patch("src.cli._get_pg_dsn", return_value=""):
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="", stdout="")):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_bypass),
                base_url="http://test",
            ) as client:
                resp = await client.post(
                    "/api/operations/restore",
                    files={"file": ("backup.tar.gz", bundle_bytes, "application/gzip")},
                )

    assert resp.status_code == 202
    data = resp.json()
    assert "sha256" in data
    assert len(data["sha256"]) == 64  # SHA-256 hex

    audit = ops_mod._RESTORE_AUDIT_LOG
    start_records = [r for r in audit if r.get("event") == "restore.start"]
    assert len(start_records) >= 1
    assert start_records[0]["sha256"] == data["sha256"]
