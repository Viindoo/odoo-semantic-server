"""Auth-contract tests for the odoo:// resource handlers (WI-F4).

Covers AC-F4-2:

  1. Auth-layer design note (documented here):
     MCP protocol authentication happens at the **transport layer** (HTTP
     Authorization header → middleware in src/mcp/middleware.py), NOT at
     the resource-read layer.  Unauthenticated requests are rejected by
     FastAPI/ASGI middleware before they reach any resource handler.
     Therefore, resource handlers never receive a ``401``-equivalent
     signal — by the time ``resources/read`` is dispatched, the caller is
     authenticated.  The correct fallback for a missing api_key_id context
     is "anonymous" (DEFAULT_API_KEY_ID = ``"default"``), which triggers
     version resolution using the latest indexed version rather than any
     per-user session state.  This test file asserts that design.

  2. Cache is per-URI, not per-tenant:
     The resource cache key is the raw URI string (``odoo://<v>/model/<n>``).
     Two different API keys reading the same URI get the same cached body.
     This is intentional — resource bodies are read-only, version-pinned
     content, not personalized.  Per-user session-version differences are
     baked into the resolved version *before* the cache key is formed
     (``odoo://auto/model/sale.order`` resolves to a concrete version on
     first read; subsequent callers get that same resolved body from cache).

DB isolation:
  - Neo4j version "FA_99.0" — distinct from all other test suites.

Markers:
  - All tests are marked ``neo4j``.
"""

from __future__ import annotations

import asyncio
import importlib
import os

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FA_VERSION = "FA_99.0"
FA_MODULE = "fa_sale"
FA_MODEL = "fa.order"

pytestmark = pytest.mark.neo4j


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fa_db(neo4j_driver):
    """Seed a minimal Module + Model for FA_VERSION."""
    with neo4j_driver.session() as s:
        s.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=FA_VERSION)

    from src.indexer.models import (
        FieldInfo,
        ModelInfo,
        ModuleInfo,
        ParseResult,
    )
    from src.indexer.writer_neo4j import Neo4jWriter

    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    module = ModuleInfo(
        name=FA_MODULE,
        odoo_version=FA_VERSION,
        repo="odoo_test",
        path="/tmp/fa_sale",
        depends=["base"],
        edition="community",
    )
    model = ModelInfo(
        name=FA_MODEL,
        module=FA_MODULE,
        odoo_version=FA_VERSION,
        fields=[FieldInfo("name", "char")],
        methods=[],
    )
    writer.write_results([ParseResult(module=module, models=[model])])
    writer.close()

    yield neo4j_driver

    with neo4j_driver.session() as s:
        s.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=FA_VERSION)


@pytest.fixture()
def fresh_resources_module(monkeypatch):
    """Reload src.mcp.resources so its cache is empty for each test."""
    monkeypatch.setenv("NEO4J_URI", os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"))
    monkeypatch.setenv("NEO4J_USER", os.getenv("NEO4J_TEST_USER", "neo4j"))
    monkeypatch.setenv("NEO4J_PASSWORD", os.getenv("NEO4J_TEST_PASSWORD", "password"))
    import src.mcp.resources as mod
    importlib.reload(mod)
    return mod


@pytest.fixture()
def mcp_with_resources(fa_db, fresh_resources_module):
    """Create a fresh FastMCP with all 7 handlers registered."""
    from fastmcp import FastMCP

    mcp = FastMCP("test-fa-resources-auth")
    fresh_resources_module.register_resources(mcp)
    return mcp


def _read(mcp, uri: str) -> str:
    """Synchronous helper: read a resource and return its first text body."""
    try:
        prior_loop = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        prior_loop = None

    new_loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(new_loop)
        contents = new_loop.run_until_complete(
            mcp._resource_manager.read_resource(uri),
        )
    finally:
        new_loop.close()
        if prior_loop is not None and not prior_loop.is_closed():
            asyncio.set_event_loop(prior_loop)
        else:
            asyncio.set_event_loop(asyncio.new_event_loop())

    if isinstance(contents, list | tuple):
        first = contents[0]
        return first.content if hasattr(first, "content") else str(first)
    if hasattr(contents, "content"):
        return contents.content
    return str(contents)


# ===========================================================================
# AC-F4-2a: Missing api_key_id context → "anonymous" fallback (not 401)
# ===========================================================================


def test_missing_api_key_id_uses_anonymous_fallback(
    mcp_with_resources, fresh_resources_module,
) -> None:
    """resources/read with no api_key_id set falls back to 'default' not 401.

    Design note:
        MCP auth is at the transport layer (middleware rejects unauthenticated
        requests before resources/read is dispatched).  Inside the handler,
        _get_api_key_id() returns the module-level DEFAULT_API_KEY_ID
        ("default") when no thread-local is set.  This means the resource
        handler runs in "anonymous" mode — version resolves to the latest
        indexed version for that key.

        The handler must NOT raise or return a 401-style error string.  It
        must return content (either the model tree or a "not found" message).
    """
    import src.mcp.server as _srv

    # Ensure no thread-local api_key_id is set for this thread.
    try:
        del _srv._api_key_id_local.value
    except AttributeError:
        pass  # Already absent — that is fine.

    uri = f"odoo://{FA_VERSION}/model/{FA_MODEL}"
    body = _read(mcp_with_resources, uri)

    # The handler must return content, not raise, not return "401" or "forbidden".
    assert body, "Body must be non-empty for anonymous read"
    assert "401" not in body, "Resource handler must never emit HTTP 401 text"
    assert "403" not in body, "Resource handler must never emit HTTP 403 text"
    assert "unauthorized" not in body.lower(), (
        "Resource handler must not surface 'unauthorized' — auth is transport-layer"
    )

    # The body should be meaningful content (model tree or not-found message).
    has_content = FA_MODEL in body or "not found" in body.lower()
    assert has_content, (
        f"Body should be a model tree or not-found message; got: {body[:300]!r}"
    )


def test_anonymous_fallback_returns_content_not_exception(
    mcp_with_resources,
) -> None:
    """Calling resources/read without a session never surfaces an exception body.

    Verifies the handler-level try/except wraps all version-resolution errors
    so the MCP client always receives a string body even when 'default' key
    has no session state and there is no indexed version at all.
    """
    import src.mcp.server as _srv

    # Clear any residual thread-local.
    try:
        del _srv._api_key_id_local.value
    except AttributeError:
        pass

    # Use a version that doesn't exist — resolution will fail with ValueError
    # or return "not found", but must never propagate an unhandled exception.
    uri = "odoo://99.9.9.9/model/completely.nonexistent"
    try:
        body = _read(mcp_with_resources, uri)
    except Exception as exc:
        raise AssertionError(
            f"resources/read must not raise — handler must catch resolution "
            f"errors and return a 'not found' body; raised: {exc!r}"
        ) from exc

    assert isinstance(body, str), "Body must be a string"
    assert body.strip(), "Body must not be empty"


# ===========================================================================
# AC-F4-2b: Cache is per-URI, not per-tenant — two keys get same body
# ===========================================================================


def test_two_api_keys_reading_same_uri_get_same_body(
    mcp_with_resources, fresh_resources_module,
) -> None:
    """API-key-A and API-key-B reading the same URI share one cache entry.

    Design note:
        Cache key = raw URI string.  Version resolution (e.g., 'auto' → '17.0')
        is baked into the concrete URI *before* cache lookup when the version
        sentinel is concrete (like FA_99.0 here).  Two different API keys
        reading odoo://FA_99.0/model/fa.order must get byte-identical bodies
        because the content is not personalized.

        This is the intentional design per ADR-0030 — resource bodies are
        read-only, version-pinned content.  Billing / access-control
        differentiation happens at the transport layer, not inside handlers.
    """
    import src.mcp.server as _srv

    cache = fresh_resources_module.get_cache()
    cache.clear()
    uri = f"odoo://{FA_VERSION}/model/{FA_MODEL}"

    # --- Read as API-key-A ---
    _srv._api_key_id_local.value = "api-key-a-9901"
    try:
        body_a = _read(mcp_with_resources, uri)
    finally:
        try:
            del _srv._api_key_id_local.value
        except AttributeError:
            pass

    # --- Read as API-key-B ---
    _srv._api_key_id_local.value = "api-key-b-9902"
    try:
        body_b = _read(mcp_with_resources, uri)
    finally:
        try:
            del _srv._api_key_id_local.value
        except AttributeError:
            pass

    # Both must be non-empty.
    assert body_a, "API-key-A read must return a non-empty body"
    assert body_b, "API-key-B read must return a non-empty body"

    # Same URI → same cached body (cache is content-addressed, not tenant-addressed).
    assert body_a == body_b, (
        "Two different API keys reading the same URI must get identical bodies "
        "(cache is per-URI, not per-tenant).  "
        f"body_a[:100]={body_a[:100]!r}, body_b[:100]={body_b[:100]!r}"
    )

    # The URI must be in the cache after the two reads.
    assert uri in cache, "URI must be present in the cache after reads"

    # Only one cache entry for this URI (not two, one per tenant).
    uri_count = sum(1 for k in list(cache._data.keys()) if k == uri)
    assert uri_count == 1, (
        f"Cache must hold exactly one entry for URI {uri!r}; found {uri_count}"
    )


# ===========================================================================
# AC-FFIX-4: No tenant leakage on sentinel URI — two keys with different
# active versions must get version-distinct bodies, not share the first body.
# ===========================================================================


def test_two_keys_different_active_versions_get_their_own_bodies(
    mcp_with_resources, fresh_resources_module,
) -> None:
    """Sentinel odoo://auto/* resolves per-tenant before cache key is formed.

    Regression test for the cache-key tenant leakage bug identified in the
    Wave F Opus final gate review:

        BUG: handlers formed the cache key from the raw ``version`` segment
        (``"auto"``) THEN called ``_render_*(version, ...)`` which resolved
        internally.  First caller's body (resolved to 17.0) was stored under
        key ``odoo://auto/model/X``.  Second caller (active_version=16.0) hit
        the same cache entry and received 17.0 content.

        FIX: each handler now calls ``_resolved_version_for(version)`` first,
        then forms the cache key from the resolved concrete version
        (``odoo://17.0/model/X`` vs ``odoo://16.0/model/X``).

    Test strategy:
        * Seed two distinct Model nodes (same name, different fake versions
          FA_99A.0 and FA_99B.0) so we can tell which version body we got.
        * Mock ``_resolved_version_for`` to return different versions for the
          two "tenants" without touching real session state.
        * Assert body_A contains FA_99A.0 and body_B contains FA_99B.0 — if
          the bug were present, body_B would still contain FA_99A.0.
    """
    import src.mcp.server as _srv

    FA_VER_A = "FA_99A.0"
    FA_VER_B = "FA_99B.0"

    # ------------------------------------------------------------------
    # Seed two distinct Module + Model nodes at FA_VER_A and FA_VER_B.
    # ------------------------------------------------------------------
    import os

    from src.indexer.models import FieldInfo, ModelInfo, ModuleInfo, ParseResult
    from src.indexer.writer_neo4j import Neo4jWriter

    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()
    try:
        for ver in (FA_VER_A, FA_VER_B):
            mod = ModuleInfo(
                name="fa_leak_module",
                odoo_version=ver,
                repo="odoo_test",
                path="/tmp/fa_leak",
                depends=["base"],
                edition="community",
            )
            mdl = ModelInfo(
                name="fa.leak.model",
                module="fa_leak_module",
                odoo_version=ver,
                fields=[FieldInfo("name", "char")],
                methods=[],
            )
            writer.write_results([ParseResult(module=mod, models=[mdl])])
    finally:
        writer.close()

    # ------------------------------------------------------------------
    # Clear the module cache and patch _resolved_version_for to return
    # distinct concrete versions for our two "tenants".
    # ------------------------------------------------------------------
    cache = fresh_resources_module.get_cache()
    cache.clear()

    # Patch _resolved_version_for to return version keyed on the current
    # thread-local api_key_id so each tenant gets the right version
    # without touching real session state.
    _TENANT_VERSIONS = {
        "api-key-tenant-a-7701": FA_VER_A,
        "api-key-tenant-b-7702": FA_VER_B,
    }

    def _patched_resolve(version: str) -> str:
        # Peek at which api_key_id is active in this thread.
        key_id = getattr(_srv._api_key_id_local, "value", None)
        return _TENANT_VERSIONS.get(key_id, FA_VER_A)

    import src.mcp.resources
    original_fn = src.mcp.resources._resolved_version_for
    src.mcp.resources._resolved_version_for = _patched_resolve

    try:
        # Both reads use the sentinel "auto" URI — bug would collapse them.
        uri_sentinel = "odoo://auto/model/fa.leak.model"

        # Tenant A reads first.
        _srv._api_key_id_local.value = "api-key-tenant-a-7701"
        try:
            body_a = _read(mcp_with_resources, uri_sentinel)
        finally:
            try:
                del _srv._api_key_id_local.value
            except AttributeError:
                pass

        # Tenant B reads second — must NOT get tenant A's cached body.
        _srv._api_key_id_local.value = "api-key-tenant-b-7702"
        try:
            body_b = _read(mcp_with_resources, uri_sentinel)
        finally:
            try:
                del _srv._api_key_id_local.value
            except AttributeError:
                pass
    finally:
        src.mcp.resources._resolved_version_for = original_fn
        # Cleanup seeded nodes.
        import neo4j as _neo4j_pkg
        drv = _neo4j_pkg.GraphDatabase.driver(
            os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
            auth=(
                os.getenv("NEO4J_TEST_USER", "neo4j"),
                os.getenv("NEO4J_TEST_PASSWORD", "password"),
            ),
        )
        with drv.session() as s:
            for ver in (FA_VER_A, FA_VER_B):
                s.run(
                    "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=ver,
                )
        drv.close()

    # ------------------------------------------------------------------
    # Assertions: each tenant must see their own version in the body.
    # ------------------------------------------------------------------
    assert body_a, "Tenant A body must be non-empty"
    assert body_b, "Tenant B body must be non-empty"

    assert FA_VER_A in body_a, (
        f"Tenant A body must contain {FA_VER_A!r} — "
        f"got: {body_a[:200]!r}"
    )
    assert FA_VER_B in body_b, (
        f"Tenant B body must contain {FA_VER_B!r} (not {FA_VER_A!r}) — "
        f"got: {body_b[:200]!r}"
    )
    assert FA_VER_A not in body_b, (
        f"Tenant B body must NOT contain tenant A's version {FA_VER_A!r} — "
        f"cache-key tenant leakage detected. body_b[:200]={body_b[:200]!r}"
    )
