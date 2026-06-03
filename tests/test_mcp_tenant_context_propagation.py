# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression for the #248 follow-up PARALLEL bug — the tenant-isolation bypass.

Background (security-critical)
------------------------------
``_get_tenant_id()`` reads ``_tenant_id_var``, which the FastMCP
``UsageLogMiddleware.on_call_tool`` sets in the SAME line as ``api_key_id``
(``_set_server_tenant_id(tenant_id)``). It therefore suffers the IDENTICAL
context-boundary loss as the api_key_id bug fixed in commit ddada46: on the
stateful streamable-HTTP transport the middleware's ``.set()`` lands in a
context that is NOT an ancestor of the tool-body context, so the tool body
reads ``None`` for EVERY HTTP call — even a tenant-scoped key.

Consequence: ``None`` flows ``_get_allowed_profiles`` → ``_effective_allowed``
→ ``_allowed_to_guc(None) = '*'`` → the RLS ``app.allowed_profiles`` GUC becomes
``'*'`` → the policy reads ALL profiles across ALL tenants. i.e. a tenant-scoped
key reads other tenants' data over streamable-HTTP (ADR-0034 bypass).

The fix adds a per-request header-recovery fallback INSIDE ``_get_tenant_id()``:
recover the tenant from the current request's own ``X-API-Key`` header via the
warm tenant cache, disambiguating the overloaded ``None`` and FAILING CLOSED
(raising ``TenantResolutionDenied``) on the cold-cache edge of an authenticated
key. The cross-request tests prove no tenant bleed.

Pure unit test — NO DB, NO postgres/neo4j marker. The only mocks are the HTTP
boundary (``get_http_request``), the warm-cache lookups, and (for the
fail-closed edge) the ``auth_store().verify_api_key_full`` authoritative path.

NOTE: every module is imported LAZILY inside the helpers/fixtures — other tests
in the suite pop+re-import ``src.mcp.server``, so a top-level binding could point
at a stale module while ``patch`` targets the live one.
"""
import contextvars
from unittest.mock import MagicMock, patch

import pytest

RAW_KEY = "osm_test_248ctx_tenant_rawkey"
TENANT = 7  # tenant-scoped key under test


@pytest.fixture(autouse=True)
def _isolate_ctx():
    """Snapshot/restore the server tenant_id ContextVar so a test that sets it
    cannot leak into a sibling test."""
    from src.mcp import server as _srv

    tok = _srv._tenant_id_var.set(_srv._tenant_id_var.get())
    try:
        yield
    finally:
        _srv._tenant_id_var.reset(tok)


def _force_none_tenant_contextvar():
    """Put ``_tenant_id_var`` in the ``None`` state, faithfully reproducing the
    tool-body context where the middleware's ``.set()`` (done in a DIFFERENT,
    non-ancestor context) is invisible."""
    from src.mcp import server as _srv

    _srv._tenant_id_var.set(None)


def _fake_request(headers: dict):
    req = MagicMock()
    req.headers = headers
    return req


def _read_tenant_with(headers: dict, *, cache_hit, cache_tenant):
    """Read ``_get_tenant_id()`` while the ContextVar is ``None`` (foreign-context
    condition) and the HTTP/warm-cache boundary is mocked.

    Runs inside a freshly captured ``contextvars.Context`` to mirror the FastMCP
    per-connection context boundary.
    """
    from src.mcp import server as _srv

    def _body():
        _force_none_tenant_contextvar()
        assert _srv._tenant_id_var.get() is None  # confirm the broken state
        with patch("fastmcp.server.dependencies.get_http_request",
                   return_value=_fake_request(headers)), \
             patch("src.mcp.middleware._cache_get_tenant",
                   return_value=(cache_hit, cache_tenant)):
            return _srv._get_tenant_id()

    return contextvars.copy_context().run(_body)


def test_tool_body_recovers_tenant_when_contextvar_is_none():
    """PARALLEL #248 CORE (security): ContextVar still None in the tool body +
    warm cache returns tenant_id=7 + header → _get_tenant_id() recovers 7, NOT
    None.

    Pre-fix this returned None → _allowed_to_guc(None)='*' → reads ALL tenants'
    profiles (the tenant-isolation bypass). This is the test that FAILS pre-fix.
    """
    got = _read_tenant_with(
        {"X-API-Key": RAW_KEY}, cache_hit=True, cache_tenant=TENANT
    )
    assert got == TENANT, (
        f"tool body must recover tenant {TENANT} from the X-API-Key header when "
        f"the ContextVar did not propagate; got {got!r} — this is the GUC='*' "
        f"tenant-isolation bypass"
    )
    assert got is not None


def test_genuine_admin_key_stays_none():
    """A genuine admin/global key: warm cache HIT with tenant_id=None + header
    → returns None (correct unrestricted access; tenant_id IS NULL in DB).
    Must NOT be mistaken for the bypass — a cache HIT is authoritative."""
    got = _read_tenant_with(
        {"X-API-Key": RAW_KEY}, cache_hit=True, cache_tenant=None
    )
    assert got is None


def test_no_header_stays_none_graceful():
    """Negative: no X-API-Key header (stdio/CLI/local admin) → None, no recovery,
    no raise. Legitimate unrestricted/local path — unchanged."""
    got = _read_tenant_with({}, cache_hit=False, cache_tenant=None)
    assert got is None


def test_explicit_contextvar_wins_over_header_fallback():
    """No regression: when the ContextVar DID propagate (a real int), that value
    is returned and the header fallback is NOT consulted — even if the cache
    holds a different tenant. The fallback only fires on None."""
    from src.mcp import server as _srv

    propagated_tenant = 42

    def _body():
        _srv._tenant_id_var.set(propagated_tenant)
        with patch("fastmcp.server.dependencies.get_http_request",
                   return_value=_fake_request({"X-API-Key": RAW_KEY})), \
             patch("src.mcp.middleware._cache_get_tenant", return_value=(True, 999)):
            return _srv._get_tenant_id()

    assert contextvars.copy_context().run(_body) == propagated_tenant


def test_two_requests_resolve_independently_no_cross_leak():
    """SECURITY: two simulated requests with DIFFERENT keys (tenant 7 and tenant
    9) each resolve to their OWN tenant from their OWN header — never each
    other's. Re-reading the first afterwards still returns 7, proving no
    sticky/global bleed. The fallback derives the tenant solely from the current
    request's X-API-Key."""
    from src.mcp import server as _srv

    key_a, tenant_a = "osm_key_tenantA", 7
    key_b, tenant_b = "osm_key_tenantB", 9

    def _cache_by_key(raw):
        return {key_a: (True, tenant_a), key_b: (True, tenant_b)}.get(raw, (False, None))

    def _read(header_key):
        def _body():
            _force_none_tenant_contextvar()
            with patch("fastmcp.server.dependencies.get_http_request",
                       return_value=_fake_request({"X-API-Key": header_key})), \
                 patch("src.mcp.middleware._cache_get_tenant", side_effect=_cache_by_key):
                return _srv._get_tenant_id()
        return contextvars.copy_context().run(_body)

    assert _read(key_a) == tenant_a
    assert _read(key_b) == tenant_b
    # Re-read A again after B — still A's tenant, proving no sticky/global bleed.
    assert _read(key_a) == tenant_a


# ---------------------------------------------------------------------------
# FAIL-CLOSED edge (the most important test): authenticated header + cache MISS
# ---------------------------------------------------------------------------
def _read_tenant_cold_cache(*, verify_return=None, verify_raises=None):
    """Read ``_get_tenant_id()`` with ContextVar None, header present, but the
    warm tenant cache MISSING — so the authoritative DB path is exercised. The
    ``verify_api_key_full`` boundary is mocked (no real DB)."""
    from src.mcp import server as _srv

    store = MagicMock()
    if verify_raises is not None:
        store.verify_api_key_full.side_effect = verify_raises
    else:
        store.verify_api_key_full.return_value = verify_return

    def _body():
        _force_none_tenant_contextvar()
        with patch("fastmcp.server.dependencies.get_http_request",
                   return_value=_fake_request({"X-API-Key": RAW_KEY})), \
             patch("src.mcp.middleware._cache_get_tenant",
                   return_value=(False, None)), \
             patch("src.db.pg.auth_store", return_value=store), \
             patch("src.mcp.middleware._cache_set"), \
             patch("src.mcp.middleware._cache_set_tenant"), \
             patch("src.mcp.middleware._cache_set_owner"):
            return _srv._get_tenant_id()

    return contextvars.copy_context().run(_body)


def test_cold_cache_authoritative_resolves_tenant_scoped():
    """FAIL-CLOSED edge — DB-resolve path: authenticated key, cache MISS, but
    verify_api_key_full authoritatively returns a tenant-scoped key
    (key_id, tenant_id=7, ...) → _get_tenant_id() returns 7, NOT None.

    This proves the cold-cache TTL race still scopes a tenant key (does not
    widen to unrestricted)."""
    got = _read_tenant_cold_cache(verify_return=(33, TENANT, 5, False))
    assert got == TENANT
    assert got is not None


def test_cold_cache_authoritative_confirms_admin_returns_none():
    """FAIL-CLOSED edge — admin confirmation: cache MISS but verify
    authoritatively confirms tenant_id IS NULL (genuine admin/global key) →
    returns None. Only an authoritative NULL may widen to unrestricted."""
    got = _read_tenant_cold_cache(verify_return=(33, None, None, True))
    assert got is None


def test_cold_cache_db_unavailable_denies_not_widens():
    """FAIL-CLOSED edge (MOST IMPORTANT): authenticated key, cache MISS, AND the
    authoritative lookup is UNAVAILABLE (verify raises) → _get_tenant_id() RAISES
    TenantResolutionDenied. It does NOT return None-as-unrestricted, so the read
    paths fail closed (no cross-tenant data served).

    Pre-fix _get_tenant_id() returned None here unconditionally → GUC '*' →
    cross-tenant read. This asserts the bypass is impossible on the DB-edge."""
    from src.mcp import server as _srv

    with pytest.raises(_srv.TenantResolutionDenied):
        _read_tenant_cold_cache(verify_raises=RuntimeError("pg pool down"))


def test_cold_cache_verify_none_denies_not_widens():
    """FAIL-CLOSED edge: cache MISS and verify returns None (key vanished /
    deactivated mid-window) → RAISES TenantResolutionDenied rather than widening
    an unknown-tenant authenticated key to unrestricted access."""
    from src.mcp import server as _srv

    with pytest.raises(_srv.TenantResolutionDenied):
        _read_tenant_cold_cache(verify_return=None)
