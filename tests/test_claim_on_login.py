# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for WI-6: claim-on-login wiring (M10B P1, ADR-0039 D3 §5).

Business rules verified:
  C1  Purchase-first flow: an active unclaimed sub with buyer_email=X is claimed
      and the user's key is upgraded to the purchased plan when a VERIFIED user
      with email=X completes email verification (/api/auth/verify-email).
  C2  Purchase-first flow via password login: same claim happens on successful
      password login, but ONLY when email_verified=TRUE.
  C3  Password login does NOT claim when email_verified=FALSE (anti-spoof gate).
  C4  Idempotent: a second login/verify for the same user leaves the subscription
      still claimed and does not provision a second key.
  C5  Best-effort: if there is no matching subscription, login/verify succeeds
      with no error.

All tests require PostgreSQL (pytestmark = pytest.mark.postgres).
Tests drive real endpoints via httpx.AsyncClient(ASGITransport) so the full
request→handler→DB round-trip is exercised, including the claim side effect.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.postgres

os.environ.setdefault("WEBUI_SESSION_SECRET", "test-secret-for-claim-on-login-32bytes!!")
os.environ.setdefault("WEBUI_SECURE_COOKIE", "0")  # allow plain HTTP in tests


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _make_app():
    """Create the full FastAPI app (real routers, including entitlements)."""
    from src.web_ui.app import create_app
    return create_app()


def _make_app_no_loopback():
    """Create the full app with the loopback-only middleware disabled.

    POST /api/auth/oauth-login is normally restricted to loopback callers
    (Astro SSR callback). httpx ASGI transport has no real client IP, so the
    loopback check is patched to pass-through for these tests — mirrors the
    helper in test_oauth.py but keeps the real routers + DB round-trip.
    """
    import src.web_ui.app as app_mod

    original_dispatch = app_mod._LoopbackOnlyMiddleware.dispatch

    async def _passthrough(self, request, call_next):
        return await call_next(request)

    app_mod._LoopbackOnlyMiddleware.dispatch = _passthrough  # type: ignore[method-assign]
    try:
        app = app_mod.create_app()
    finally:
        app_mod._LoopbackOnlyMiddleware.dispatch = original_dispatch  # type: ignore[method-assign]
    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_BILLING_TABLES = ["billing_webhook_events", "subscriptions"]


def _reset_billing_tables(conn) -> None:
    """Truncate billing tables in place (NON-destructive — keeps schema).

    Uses TRUNCATE ... RESTART IDENTITY CASCADE rather than DROP TABLE so a
    full-suite run cannot tear down schema that other modules' fixtures rely
    on. Skips silently when a table does not yet exist (fresh DB before
    migrations have run for this connection).
    """
    for tbl in _BILLING_TABLES:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass(%s)", (f"public.{tbl}",)
            )
            if cur.fetchone()[0] is None:
                continue
            cur.execute(f"TRUNCATE {tbl} RESTART IDENTITY CASCADE")
    conn.commit()


@pytest.fixture(autouse=True)
def _reset_billing_state(pg_conn):
    """Clean billing-table rows before and after each test (non-destructive)."""
    _reset_billing_tables(pg_conn)
    yield
    _reset_billing_tables(pg_conn)


@pytest.fixture
def migrated_pg(clean_pg):
    """Apply all migrations on a clean DB, yield connection."""
    from src.db.migrate import run_migrations
    run_migrations(clean_pg)
    yield clean_pg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plan_id(conn, slug: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM plans WHERE slug = %s", (slug,))
        row = cur.fetchone()
    assert row is not None, f"plan slug={slug!r} must exist after migrations"
    return row[0]


def _make_user(
    conn,
    username: str,
    email: str,
    password_hash: str,
    *,
    verified: bool,
) -> int:
    """Insert a webui_users row. Returns integer id."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO webui_users"
            " (username, email, password_hash, email_verified, is_admin)"
            " VALUES (%s, %s, %s, %s, FALSE)"
            " ON CONFLICT (username) DO UPDATE"
            "   SET email = EXCLUDED.email,"
            "       password_hash = EXCLUDED.password_hash,"
            "       email_verified = EXCLUDED.email_verified"
            " RETURNING id",
            (username, email, password_hash, verified),
        )
        user_id = cur.fetchone()[0]
    conn.commit()
    return user_id


def _insert_token(conn, token: str, user_id: int) -> None:
    """Insert a valid (non-expired, non-used) email-verification token."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires_at = datetime.now(UTC) + timedelta(hours=24)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO email_verifications (token, user_id, purpose, expires_at)"
            " VALUES (%s, %s, 'email_verify', %s)",
            (token_hash, user_id, expires_at),
        )
    conn.commit()


def _sub_claimed_user(conn, sub_id: int):
    """Return claimed_user_id for the subscription (or None if unclaimed)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT claimed_user_id FROM subscriptions WHERE id = %s", (sub_id,)
        )
        row = cur.fetchone()
    return row[0] if row else None


def _key_plan_id(conn, key_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT plan_id FROM api_keys WHERE id = %s", (key_id,))
        return cur.fetchone()[0]


def _user_key_ids(conn, user_id: int) -> list[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM api_keys WHERE user_id = %s ORDER BY id", (user_id,))
        return [r[0] for r in cur.fetchall()]


def _user_id_by_email(conn, email: str) -> int | None:
    """Return webui_users.id for the given email (or None)."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM webui_users WHERE email = %s", (email,))
        row = cur.fetchone()
    return row[0] if row else None


def _insert_active_unclaimed_sub(conn, *, buyer_email: str, plan_id: int) -> int:
    """Insert an active unclaimed subscription with a stable external_ref."""
    from src.db.pg import subscription_store
    return subscription_store().upsert_by_external_ref(
        external_ref=f"test-claim-{secrets.token_hex(6)}",
        plan_id=plan_id,
        source="polar",
        status="active",
        buyer_email=buyer_email,
    )


# ---------------------------------------------------------------------------
# C1: Email-verify path claims and upgrades
# ---------------------------------------------------------------------------


class TestClaimOnEmailVerify:
    @pytest.mark.asyncio
    async def test_purchase_first_claimed_on_email_verify(self, migrated_pg):
        """C1: active unclaimed sub → claimed + key upgraded when email is verified."""
        import httpx

        from src.db.pg import auth_store
        from src.web_ui.auth import hash_password

        pro_id = _plan_id(migrated_pg, "pro")
        email = "c1verify@example.com"
        username = "c1verify"

        # Seed: unverified user + active subscription for that email.
        pw_hash = hash_password("SecurePass123!")
        user_id = _make_user(migrated_pg, username, email, pw_hash, verified=False)

        # Mint a free key so upgrade-in-place applies (mirrors signup flow order).
        _raw, _prefix, free_key_id = auth_store().create_api_key(
            name=f"Default key ({username})", user_id=user_id
        )
        free_id = _plan_id(migrated_pg, "free")
        assert _key_plan_id(migrated_pg, free_key_id) == free_id

        sub_id = _insert_active_unclaimed_sub(migrated_pg, buyer_email=email, plan_id=pro_id)
        assert _sub_claimed_user(migrated_pg, sub_id) is None  # starts unclaimed

        # Drive verify-email endpoint.
        token = secrets.token_urlsafe(32)
        _insert_token(migrated_pg, token, user_id)

        app = _make_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            resp = await client.post("/api/auth/verify-email", json={"token": token})

        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True

        # Assert: subscription is now claimed by this user.
        assert _sub_claimed_user(migrated_pg, sub_id) == user_id

        # Assert: the user's key is on the purchased plan (pro).
        assert _key_plan_id(migrated_pg, free_key_id) == pro_id

    @pytest.mark.asyncio
    async def test_no_sub_verify_still_succeeds(self, migrated_pg):
        """C5 (verify path): no matching subscription → verify still returns 200."""
        import httpx

        from src.web_ui.auth import hash_password

        email = "c5verify@example.com"
        username = "c5verify"
        pw_hash = hash_password("SecurePass123!")
        user_id = _make_user(migrated_pg, username, email, pw_hash, verified=False)

        token = secrets.token_urlsafe(32)
        _insert_token(migrated_pg, token, user_id)

        app = _make_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            resp = await client.post("/api/auth/verify-email", json={"token": token})

        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# C2 + C3: Password login path claims (only when email_verified)
# ---------------------------------------------------------------------------


class TestClaimOnPasswordLogin:
    @pytest.mark.asyncio
    async def test_verified_login_claims_sub(self, migrated_pg):
        """C2: verified user login → subscription claimed and key upgraded."""
        import httpx

        from src.db.pg import auth_store
        from src.web_ui.auth import hash_password

        pro_id = _plan_id(migrated_pg, "pro")
        email = "c2login@example.com"
        username = "c2login"
        password = "SecurePass123!"

        pw_hash = hash_password(password)
        user_id = _make_user(migrated_pg, username, email, pw_hash, verified=True)

        _raw, _prefix, free_key_id = auth_store().create_api_key(
            name=f"Default key ({username})", user_id=user_id
        )
        free_id = _plan_id(migrated_pg, "free")
        assert _key_plan_id(migrated_pg, free_key_id) == free_id

        sub_id = _insert_active_unclaimed_sub(migrated_pg, buyer_email=email, plan_id=pro_id)
        assert _sub_claimed_user(migrated_pg, sub_id) is None

        app = _make_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            resp = await client.post(
                "/api/auth/login",
                json={"username": username, "password": password},
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True

        # Sub claimed, key upgraded.
        assert _sub_claimed_user(migrated_pg, sub_id) == user_id
        assert _key_plan_id(migrated_pg, free_key_id) == pro_id

    @pytest.mark.asyncio
    async def test_unverified_login_does_not_claim(self, migrated_pg):
        """C3: unverified email → password login succeeds but sub stays unclaimed."""
        import httpx

        from src.db.pg import auth_store
        from src.web_ui.auth import hash_password

        pro_id = _plan_id(migrated_pg, "pro")
        email = "c3login@example.com"
        username = "c3login"
        password = "SecurePass123!"

        pw_hash = hash_password(password)
        user_id = _make_user(migrated_pg, username, email, pw_hash, verified=False)

        auth_store().create_api_key(name=f"Default key ({username})", user_id=user_id)

        sub_id = _insert_active_unclaimed_sub(migrated_pg, buyer_email=email, plan_id=pro_id)

        app = _make_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            resp = await client.post(
                "/api/auth/login",
                json={"username": username, "password": password},
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True

        # Subscription must stay unclaimed (email_verified=FALSE gate).
        assert _sub_claimed_user(migrated_pg, sub_id) is None

    @pytest.mark.asyncio
    async def test_no_sub_login_still_succeeds(self, migrated_pg):
        """C5 (login path): no matching subscription → login still returns 200."""
        import httpx

        from src.db.pg import auth_store
        from src.web_ui.auth import hash_password

        email = "c5login@example.com"
        username = "c5login"
        password = "SecurePass123!"

        pw_hash = hash_password(password)
        user_id = _make_user(migrated_pg, username, email, pw_hash, verified=True)
        auth_store().create_api_key(name=f"Default key ({username})", user_id=user_id)

        app = _make_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            resp = await client.post(
                "/api/auth/login",
                json={"username": username, "password": password},
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# C4: Idempotency — second login/verify does not double-provision
# ---------------------------------------------------------------------------


class TestClaimIdempotent:
    @pytest.mark.asyncio
    async def test_second_login_idempotent(self, migrated_pg):
        """C4: logging in again after claim is a no-op — still one key, sub stays claimed."""
        import httpx

        from src.db.pg import auth_store
        from src.web_ui.auth import hash_password

        pro_id = _plan_id(migrated_pg, "pro")
        email = "c4idem@example.com"
        username = "c4idem"
        password = "SecurePass123!"

        pw_hash = hash_password(password)
        user_id = _make_user(migrated_pg, username, email, pw_hash, verified=True)
        _raw, _prefix, free_key_id = auth_store().create_api_key(
            name=f"Default key ({username})", user_id=user_id
        )

        sub_id = _insert_active_unclaimed_sub(migrated_pg, buyer_email=email, plan_id=pro_id)

        app = _make_app()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
        ) as client:
            # First login — claims the sub.
            resp1 = await client.post(
                "/api/auth/login",
                json={"username": username, "password": password},
            )
            assert resp1.status_code == 200, resp1.text

            # Second login — sub already claimed; must be a no-op.
            resp2 = await client.post(
                "/api/auth/login",
                json={"username": username, "password": password},
            )
            assert resp2.status_code == 200, resp2.text

        # Still one key after two logins.
        assert len(_user_key_ids(migrated_pg, user_id)) == 1

        # Sub still linked to the same user.
        assert _sub_claimed_user(migrated_pg, sub_id) == user_id

        # Key still on the purchased plan (not double-upgraded or reset).
        assert _key_plan_id(migrated_pg, free_key_id) == pro_id


# ---------------------------------------------------------------------------
# C6 + C7: OAuth login path claims ONLY when the provider verified the email
# ---------------------------------------------------------------------------


class TestClaimOnOAuthLogin:
    """OAuth claim-on-login must be gated on provider email_verified.

    SECURITY (account takeover): a brand-new OAuth user is created with
    whatever email_verified the provider reports. Without the gate, an
    attacker registering an OAuth account on a victim's email (reported
    unverified by the provider) would claim the victim's unclaimed
    subscription. The guard mirrors the password-login email_verified gate.
    """

    @pytest.fixture(autouse=True)
    def _enable_signup(self, monkeypatch, migrated_pg):
        """Allow OAuth new-user creation (SIGNUP_ENABLED defaults False).

        signup_enabled() consults the app_settings ``signup.enabled`` overlay
        FIRST (it wins over the monkeypatched constant). When the catalogue
        default row (False) has been seeded into app_settings, patching the
        constant alone is not enough — so write the overlay row True too.
        """
        import json as _json

        monkeypatch.setattr("src.web_ui.config.SIGNUP_ENABLED", True)
        monkeypatch.setattr("src.web_ui.routes.oauth.SIGNUP_ENABLED", True)
        with migrated_pg.cursor() as cur:
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
                (_json.dumps({"v": True}),),
            )
        migrated_pg.commit()
        try:
            from src.settings import invalidate_setting
            invalidate_setting("signup.enabled")
        except Exception:
            pass

    def _oauth_body(self, *, email, email_verified, oauth_id):
        return {
            "provider": "google",
            "oauth_id": oauth_id,
            "email": email,
            "email_verified": email_verified,
            "name": "Test User",
        }

    @pytest.mark.asyncio
    async def test_unverified_oauth_new_user_does_not_claim(self, migrated_pg):
        """C6 (the bug): new OAuth user, provider email_verified=FALSE → sub stays
        UNCLAIMED and the auto-minted key is NOT upgraded (account-takeover guard).
        """
        import httpx

        pro_id = _plan_id(migrated_pg, "pro")
        free_id = _plan_id(migrated_pg, "free")
        email = "victim-unverified@example.com"

        # Victim's purchase: active unclaimed sub keyed on the victim's email.
        sub_id = _insert_active_unclaimed_sub(
            migrated_pg, buyer_email=email, plan_id=pro_id
        )
        assert _sub_claimed_user(migrated_pg, sub_id) is None

        app = _make_app_no_loopback()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            # Attacker logs in via OAuth on the victim's email, provider has
            # NOT verified it. No existing account with this email exists, so
            # the new-user path runs (the merge path's 409 does not apply).
            resp = await client.post(
                "/api/auth/oauth-login",
                json=self._oauth_body(
                    email=email, email_verified=False, oauth_id="attacker_uid_1"
                ),
            )

        # Login itself succeeds (account created); only the claim is gated.
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True

        # The attacker's account was created with an auto-minted free key.
        attacker_id = _user_id_by_email(migrated_pg, email)
        assert attacker_id is not None
        key_ids = _user_key_ids(migrated_pg, attacker_id)
        assert len(key_ids) == 1

        # GUARD: subscription must stay UNCLAIMED and the key NOT upgraded.
        assert _sub_claimed_user(migrated_pg, sub_id) is None
        assert _key_plan_id(migrated_pg, key_ids[0]) == free_id

    @pytest.mark.asyncio
    async def test_verified_oauth_new_user_claims_and_upgrades(self, migrated_pg):
        """C7: new OAuth user, provider email_verified=TRUE + matching unclaimed
        sub → claimed by the user and the auto-minted key upgraded to the plan.
        """
        import httpx

        pro_id = _plan_id(migrated_pg, "pro")
        email = "buyer-verified@example.com"

        sub_id = _insert_active_unclaimed_sub(
            migrated_pg, buyer_email=email, plan_id=pro_id
        )
        assert _sub_claimed_user(migrated_pg, sub_id) is None

        app = _make_app_no_loopback()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/api/auth/oauth-login",
                json=self._oauth_body(
                    email=email, email_verified=True, oauth_id="buyer_uid_1"
                ),
            )

        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True

        user_id = _user_id_by_email(migrated_pg, email)
        assert user_id is not None
        key_ids = _user_key_ids(migrated_pg, user_id)
        assert len(key_ids) == 1

        # Sub claimed by the verified OAuth user; key upgraded to purchased plan.
        assert _sub_claimed_user(migrated_pg, sub_id) == user_id
        assert _key_plan_id(migrated_pg, key_ids[0]) == pro_id

    @pytest.mark.asyncio
    async def test_verified_oauth_returning_user_claims(self, migrated_pg):
        """C7b: a returning (pre-existing) OAuth user whose email is provider-
        verified claims a subscription purchased after the account existed.
        """
        import httpx

        from src.db.pg import auth_store

        pro_id = _plan_id(migrated_pg, "pro")
        free_id = _plan_id(migrated_pg, "free")
        email = "returning-buyer@example.com"
        username = "returning_buyer"

        # Pre-existing OAuth account (oauth match → fast path on next login).
        with migrated_pg.cursor() as cur:
            cur.execute(
                "INSERT INTO webui_users"
                " (username, password_hash, oauth_provider, oauth_id,"
                "  email, email_verified, is_admin, is_active)"
                " VALUES (%s, NULL, 'google', %s, %s, TRUE, FALSE, TRUE)"
                " RETURNING id",
                (username, "returning_uid_1", email),
            )
            user_id = cur.fetchone()[0]
        migrated_pg.commit()

        # User already has a free key (minted at first login, prior session).
        _raw, _prefix, free_key_id = auth_store().create_api_key(
            name=f"Default key ({username})", user_id=user_id
        )
        assert _key_plan_id(migrated_pg, free_key_id) == free_id

        # Purchase happens AFTER the account exists.
        sub_id = _insert_active_unclaimed_sub(
            migrated_pg, buyer_email=email, plan_id=pro_id
        )

        app = _make_app_no_loopback()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/api/auth/oauth-login",
                json=self._oauth_body(
                    email=email, email_verified=True, oauth_id="returning_uid_1"
                ),
            )

        assert resp.status_code == 200, resp.text

        # Returning verified user claims the post-purchase sub; key upgraded.
        assert _sub_claimed_user(migrated_pg, sub_id) == user_id
        assert _key_plan_id(migrated_pg, free_key_id) == pro_id
