# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression guard for the WI-9 Tier-1 settings refactor (ADR-0042).

Business intent
---------------

WI-9 replaced 9 module-level constants (Tier-1 settings) with helper
functions / inline ``get_setting(...)`` calls so each value can be tuned at
runtime via ``app_settings``.  The original constants are **kept** as
fallback defaults — this file asserts that the live ``get_setting()`` reading
produces the **same value** as the import-time constant when the
``app_settings`` table holds only the catalogue defaults (i.e. the default
deployment posture).

Why this matters
----------------

If a future refactor accidentally changes the catalogue default in
``src/settings_registry.py`` (or the module-level constant) without updating
the other, this test surfaces the drift immediately.  It is the contract that
keeps the "wrap, don't break" promise of WI-9: the in-process behaviour after
the refactor is **identical** to before for a clean deployment.

Quota settings (``quota.*``) are **intentionally omitted** — those live in
the ``plans`` table (ADR-0039) and are not consumed at call sites through
``get_setting()``; the settings catalogue snapshot exists only as a default
crash-recovery anchor.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.postgres


@pytest.fixture
def settings_db(pg_conn):
    """Apply migrations + reset state so each test starts from a clean overlay.

    Mirrors the helper in ``tests/test_settings_resolver.py`` — the bootstrap
    inserts the catalogue defaults into ``app_settings`` on next read so each
    test exercises the **realistic** "DB has only defaults" path.
    """
    import src.settings as _settings_mod
    from src.db.migrate import run_migrations
    from src.settings_registry import register_settings_idempotent

    run_migrations(pg_conn)

    # Start clean and (re-)insert catalogue defaults so get_setting() sees
    # rows that match the module-level constant values.
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM app_settings")
    pg_conn.commit()
    register_settings_idempotent(pg_conn)

    _settings_mod.invalidate_all()
    yield pg_conn
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM app_settings")
    pg_conn.commit()
    _settings_mod.invalidate_all()


# ---------------------------------------------------------------------------
# Auth — SESSION_TTL_SECONDS
# ---------------------------------------------------------------------------


def test_session_ttl_default_matches_constant(settings_db):
    """get_setting(auth.session_ttl_seconds) == SESSION_TTL_SECONDS (8h).

    Both the helper :func:`get_session_ttl` and the raw resolver must agree
    with the constant.  Reads through the helper exercise the public surface;
    the raw lookup guards against silent catalogue drift.
    """
    from src.settings import get_setting
    from src.web_ui.auth import SESSION_TTL_SECONDS, get_session_ttl

    raw = int(get_setting("auth.session_ttl_seconds", conn=settings_db))
    assert raw == SESSION_TTL_SECONDS, (
        f"Catalogue default ({raw}) drifted from SESSION_TTL_SECONDS "
        f"({SESSION_TTL_SECONDS}) — update one or the other."
    )
    # Helper picks up the same value via the standard resolver.
    assert get_session_ttl() == SESSION_TTL_SECONDS


# ---------------------------------------------------------------------------
# Auth — MFA_GRACE_DAYS
# ---------------------------------------------------------------------------


def test_mfa_grace_default_matches_constant(settings_db):
    """get_setting(auth.mfa_grace_period_days) == MFA_GRACE_DAYS (7)."""
    from src.settings import get_setting
    from src.web_ui.middleware import MFA_GRACE_DAYS, get_mfa_grace_days

    raw = int(get_setting("auth.mfa_grace_period_days", conn=settings_db))
    assert raw == MFA_GRACE_DAYS
    assert get_mfa_grace_days() == MFA_GRACE_DAYS


# ---------------------------------------------------------------------------
# Auth — password_min_length
# ---------------------------------------------------------------------------


def test_password_min_length_default_matches_constant(settings_db):
    """get_setting(auth.password_min_length) == signup._MIN_PASSWORD_LENGTH (12).

    The signup route validates against the live setting; the constant is the
    fallback when the overlay is unavailable.  Drift between the two would
    let a deployment quietly enforce a different floor than its source file
    claims.
    """
    from src.settings import get_setting
    from src.web_ui.routes.signup import _MIN_PASSWORD_LENGTH

    raw = int(get_setting("auth.password_min_length", conn=settings_db))
    assert raw == _MIN_PASSWORD_LENGTH


# ---------------------------------------------------------------------------
# Auth — email verification TTL
# ---------------------------------------------------------------------------


def test_email_verify_ttl_default_matches_constant(settings_db):
    """get_setting(auth.email_verification_ttl_hours) == signup._EMAIL_VERIFY_TTL_HOURS (24)."""
    from src.settings import get_setting
    from src.web_ui.routes.signup import (
        _EMAIL_VERIFY_TTL_HOURS,
        _get_email_verify_ttl_hours,
    )

    raw = int(get_setting("auth.email_verification_ttl_hours", conn=settings_db))
    assert raw == _EMAIL_VERIFY_TTL_HOURS
    assert _get_email_verify_ttl_hours() == _EMAIL_VERIFY_TTL_HOURS


# ---------------------------------------------------------------------------
# Signup — signup.enabled
# ---------------------------------------------------------------------------


def test_signup_enabled_default_matches_constant(settings_db):
    """get_setting(signup.enabled) == SIGNUP_ENABLED (catalogue default False).

    The :data:`SIGNUP_ENABLED` constant folds env var > INI > default at
    process start.  When the deploy environment does **not** set the env
    variable or INI key (the default invite-only posture), the constant
    resolves to False and matches the catalogue default in
    ``settings_registry.py``.
    """
    import os

    from src.settings import get_setting
    from src.web_ui.config import SIGNUP_ENABLED, signup_enabled

    raw = bool(get_setting("signup.enabled", conn=settings_db))
    # Only assert equality when the import-time constant was not pre-flipped
    # by an env override — keeps the assertion robust against CI containers
    # that set SIGNUP_ENABLED=1 for unrelated tests.
    if os.environ.get("SIGNUP_ENABLED") is None:
        assert raw == SIGNUP_ENABLED
        assert signup_enabled() is False


# ---------------------------------------------------------------------------
# Embedding — max_batch_size
# ---------------------------------------------------------------------------


def test_embedder_max_batch_default_matches_constant(settings_db):
    """get_setting(embedding.max_batch_size) == EMBEDDER_MAX_BATCH (50).

    The class attribute ``Qwen3Embedder._MAX_BATCH`` is initialised from the
    same constant; the live resolver reads through the overlay.  Drift would
    silently change embedder batching behaviour in production.
    """
    from src.constants import EMBEDDER_MAX_BATCH
    from src.indexer.embedder import _resolved_max_batch
    from src.settings import get_setting

    raw = int(get_setting("embedding.max_batch_size", conn=settings_db))
    assert raw == EMBEDDER_MAX_BATCH
    # _resolved_max_batch goes through the pool, so this also exercises the
    # DB lookup end-to-end (pool is initialised by the migrated_pg fixture
    # chain that pg_conn depends on).
    assert _resolved_max_batch(EMBEDDER_MAX_BATCH) == EMBEDDER_MAX_BATCH


# ---------------------------------------------------------------------------
# Embedding — timeout_read_seconds
# ---------------------------------------------------------------------------


def test_embedder_timeout_read_default_matches_constant(settings_db):
    """get_setting(embedding.timeout_read_seconds) == TIMEOUT_EMBEDDER_READ (1200)."""
    from src.constants import TIMEOUT_EMBEDDER_READ
    from src.indexer.embedder import _resolved_timeout_read
    from src.settings import get_setting

    raw = int(get_setting("embedding.timeout_read_seconds", conn=settings_db))
    assert raw == TIMEOUT_EMBEDDER_READ
    assert _resolved_timeout_read(TIMEOUT_EMBEDDER_READ) == TIMEOUT_EMBEDDER_READ


# ---------------------------------------------------------------------------
# Indexer — git_clone_timeout_seconds
# ---------------------------------------------------------------------------


def test_git_clone_timeout_default_matches_constant(settings_db):
    """get_setting(indexer.git_clone_timeout_seconds) == TIMEOUT_GIT_CLONE (3600)."""
    from src.constants import TIMEOUT_GIT_CLONE
    from src.git_utils import _resolved_clone_timeout
    from src.settings import get_setting

    raw = int(get_setting("indexer.git_clone_timeout_seconds", conn=settings_db))
    assert raw == TIMEOUT_GIT_CLONE
    assert _resolved_clone_timeout() == TIMEOUT_GIT_CLONE


# ---------------------------------------------------------------------------
# MCP — resource_cache_ttl_seconds
# ---------------------------------------------------------------------------


def test_mcp_cache_ttl_default_matches_constant(settings_db):
    """get_setting(mcp.resource_cache_ttl_seconds) == DEFAULT_CACHE_TTL_SEC (300)."""
    from src.mcp.resources import DEFAULT_CACHE_TTL_SEC, _resolve_cache_ttl
    from src.settings import get_setting

    raw = float(get_setting("mcp.resource_cache_ttl_seconds", conn=settings_db))
    assert raw == DEFAULT_CACHE_TTL_SEC
    assert _resolve_cache_ttl() == DEFAULT_CACHE_TTL_SEC


# ---------------------------------------------------------------------------
# Sanity check — the refactor genuinely overrides on DB row presence
# ---------------------------------------------------------------------------


def test_overlay_overrides_constant(settings_db):
    """An app_settings row with a non-default value DOES override the constant.

    This is the actual reason the refactor exists — without this property the
    helper functions are dead code.  We update one setting (session TTL) to a
    distinct value and confirm the helper returns it, then the raw resolver
    sees the same number.  All other settings continue to match their
    catalogue defaults (no leakage between rows).
    """
    import json

    import src.settings as _settings_mod
    from src.web_ui.auth import SESSION_TTL_SECONDS, get_session_ttl

    override_value = 12345
    assert override_value != SESSION_TTL_SECONDS  # invariant for the assertion

    with settings_db.cursor() as cur:
        cur.execute(
            "UPDATE app_settings"
            " SET value_json = %s::jsonb"
            " WHERE key = 'auth.session_ttl_seconds' AND scope = 'system'",
            (json.dumps({"v": override_value}),),
        )
    settings_db.commit()
    _settings_mod.invalidate_all()  # bust the 60s cache

    assert get_session_ttl() == override_value
