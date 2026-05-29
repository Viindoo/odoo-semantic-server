# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_signup_enabled_db_overlay.py
"""WI-RV F-A: verify signup.enabled DB overlay actually wires the gate.

Business intent:
  The signup_enabled() helper in src/web_ui/config.py is supposed to consult
  the app_settings DB row for "signup.enabled" first and only fall back to
  the SIGNUP_ENABLED constant when the row is absent.  Before WI-RV F-A,
  the register + oauth route handlers read the constant directly, so an
  admin who PATCHed signup.enabled=True via /api/admin/settings/signup.enabled
  could not actually open registration without a service restart — the DB
  overlay was dead code.

  This test proves the wiring is live: write a True row directly into
  app_settings (skipping the admin endpoint to keep the test focused on the
  config layer), call POST /api/auth/register, and assert the registration
  is NOT blocked (anything other than 403 signup_disabled).

All tests require PostgreSQL (pytestmark postgres).
"""
from __future__ import annotations

import json
import os

import httpx
import pytest

from src.db.migrate import run_migrations
from src.settings import invalidate_all
from src.web_ui.app import create_app

pytestmark = pytest.mark.postgres

os.environ.setdefault("WEBUI_SESSION_SECRET", "test-secret-f-a-32-bytes-padding!!")


@pytest.fixture
def migrated_pg(clean_pg):
    run_migrations(clean_pg)
    return clean_pg


@pytest.fixture(autouse=True)
def _clear_caches():
    invalidate_all()
    yield
    invalidate_all()


def _async_client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _write_signup_enabled_row(conn, value: bool) -> None:
    """Insert/update the system-scope app_settings row for signup.enabled."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app_settings (
                key, value_json, category, scope, data_type,
                validation_json, default_value
            )
            VALUES ('signup.enabled', %s::jsonb, 'auth', 'system', 'bool',
                    '{}'::jsonb, '{"v": false}'::jsonb)
            ON CONFLICT (key) WHERE scope = 'system' AND tenant_id IS NULL
            DO UPDATE SET value_json = EXCLUDED.value_json
            """,
            (json.dumps({"v": value}),),
        )
    conn.commit()


@pytest.mark.asyncio
async def test_db_overlay_true_opens_register_endpoint(migrated_pg, monkeypatch):
    """signup.enabled=True row -> POST /register is NOT blocked by 403.

    The module-level SIGNUP_ENABLED constant is left at its default False
    (invite-only).  The only reason the gate opens is the DB overlay row —
    proving the wiring is live, not dead code.
    """
    # Strip any leaked test-bypass + env override so we exercise the real gate.
    monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)
    monkeypatch.setattr("src.web_ui.config.SIGNUP_ENABLED", False)
    monkeypatch.setattr("src.web_ui.routes.signup.SIGNUP_ENABLED", False)

    _write_signup_enabled_row(migrated_pg, True)
    invalidate_all()

    app = create_app()
    async with _async_client(app) as client:
        resp = await client.post(
            "/api/auth/register",
            json={
                "email": "wi-rv-fa@test.invalid",
                "username": "wi_rv_fa_user",
                "password": "SecurePass123!",
                "confirm_password": "SecurePass123!",
                "hcaptcha_token": "",
            },
        )

    # The signup-gate branch MUST NOT short-circuit with 403/signup_disabled.
    # Downstream validation (email/captcha) may still produce 4xx; the only
    # status we forbid here is 403 with error="signup_disabled".
    assert resp.status_code != 403 or resp.json().get("error") != "signup_disabled", (
        f"DB overlay signup.enabled=True did not open the gate: "
        f"status={resp.status_code} body={resp.text}"
    )

    # Cleanup: drop the row so subsequent tests start clean.
    with migrated_pg.cursor() as cur:
        cur.execute(
            "DELETE FROM app_settings WHERE key = 'signup.enabled' "
            "AND scope = 'system' AND tenant_id IS NULL"
        )
    migrated_pg.commit()


@pytest.mark.asyncio
async def test_db_overlay_false_blocks_register_endpoint(migrated_pg, monkeypatch):
    """signup.enabled=False row -> POST /register MUST return 403 signup_disabled.

    Even when the module-level SIGNUP_ENABLED constant is True (e.g. env var
    set during a soft-open beta), an admin must be able to slam the gate shut
    at runtime via the DB row.  This is the critical security property of
    WI-RV F-A.
    """
    monkeypatch.delenv("WEBUI_AUTH_DISABLED", raising=False)
    # Constant says open; DB row says closed; DB row MUST win.
    monkeypatch.setattr("src.web_ui.config.SIGNUP_ENABLED", True)
    monkeypatch.setattr("src.web_ui.routes.signup.SIGNUP_ENABLED", True)

    _write_signup_enabled_row(migrated_pg, False)
    invalidate_all()

    app = create_app()
    async with _async_client(app) as client:
        resp = await client.post(
            "/api/auth/register",
            json={
                "email": "wi-rv-fa-block@test.invalid",
                "username": "wi_rv_fa_block",
                "password": "SecurePass123!",
                "confirm_password": "SecurePass123!",
                "hcaptcha_token": "",
            },
        )

    assert resp.status_code == 403, (
        f"DB overlay signup.enabled=False did not block registration "
        f"(constant=True): status={resp.status_code} body={resp.text}"
    )
    assert resp.json().get("error") == "signup_disabled"

    # Cleanup
    with migrated_pg.cursor() as cur:
        cur.execute(
            "DELETE FROM app_settings WHERE key = 'signup.enabled' "
            "AND scope = 'system' AND tenant_id IS NULL"
        )
    migrated_pg.commit()


def test_no_db_row_falls_back_to_constant(migrated_pg, monkeypatch):
    """No app_settings row -> signup_enabled() reads SIGNUP_ENABLED constant.

    This is the backward-compat path: existing tests monkeypatch the constant
    and expect signup_enabled() to honour it when there is no DB overlay
    explicitly set by an admin.  Falls through via get_overlay_only=None.
    """
    from src.web_ui.config import signup_enabled

    # Ensure no row exists.
    with migrated_pg.cursor() as cur:
        cur.execute(
            "DELETE FROM app_settings WHERE key = 'signup.enabled' "
            "AND scope = 'system' AND tenant_id IS NULL"
        )
    migrated_pg.commit()
    invalidate_all()

    monkeypatch.setattr("src.web_ui.config.SIGNUP_ENABLED", True)
    assert signup_enabled() is True, (
        "No DB row + constant=True must yield signup_enabled() == True"
    )

    monkeypatch.setattr("src.web_ui.config.SIGNUP_ENABLED", False)
    invalidate_all()
    assert signup_enabled() is False, (
        "No DB row + constant=False must yield signup_enabled() == False"
    )
