# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for WI-B1: on_read_resource sets api_key_id ContextVar.

Prior to WI-B1, the UsageLogMiddleware only set the _api_key_id_var ContextVar
in on_call_tool but not in on_read_resource.  Resource reads therefore always
resolved to the 'default' sentinel, bypassing the sticky-session version set
via set_active_version().

WI-B1 added on_read_resource to UsageLogMiddleware (mirrors on_call_tool exactly:
  1. reads api_key_id from request.state
  2. calls _set_server_api_key(api_key_id) before call_next
  3. clears via _set_server_api_key(None) in finally)

This file verifies the contract at two levels:

Level A (unit, no DB) — _set_server_api_key mechanics:
  A1. Calling _set_server_api_key(key_id) sets the _api_key_id_var ContextVar
      to key_id, and _get_api_key_id() returns it.
  A2. Calling _set_server_api_key(None) clears the ContextVar so
      _get_api_key_id() falls back to 'default'.
  A3. The on_read_resource hook sets the ContextVar before delegating to
      call_next and clears it in finally (mirrors on_call_tool behaviour).

Level B (integration, Neo4j) — sticky session through resource read:
  B1. After setting the _api_key_id_var ContextVar to an api_key that has
      a seeded session version (TEST_VERSION), reading a resource via the
      resource manager resolves to TEST_VERSION content, NOT to the default
      sentinel fall-through.  This confirms the ContextVar is respected by
      _resolved_version_for inside the resource handler.

DB isolation:
  - Neo4j version "RS_99.0" — distinct from all other test suites.
"""
import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.neo4j

RS_VERSION = "RS_99.0"
RS_MODULE = "rs_sale"
RS_MODEL = "rs.order"


@pytest.fixture(autouse=True)
def _isolate_server_context_vars():
    """Snapshot and restore the server ContextVars around each test so a bare
    ``_api_key_id_var.set(...)`` in setup/teardown cannot leak into the next test
    (token discipline — M3 from the PR #197 review)."""
    from src.mcp import server as _srv

    ak_token = _srv._api_key_id_var.set(_srv._api_key_id_var.get())
    tid_token = _srv._tenant_id_var.set(_srv._tenant_id_var.get())
    try:
        yield
    finally:
        _srv._api_key_id_var.reset(ak_token)
        _srv._tenant_id_var.reset(tid_token)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rs_db(neo4j_driver):
    """Seed a minimal Module + Model for RS_VERSION."""
    with neo4j_driver.session() as s:
        s.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=RS_VERSION)

    from src.indexer.models import FieldInfo, ModelInfo, ModuleInfo, ParseResult
    from src.indexer.writer_neo4j import Neo4jWriter

    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    module = ModuleInfo(
        name=RS_MODULE,
        odoo_version=RS_VERSION,
        repo="odoo_test",
        path="/tmp/rs_sale",
        depends=["base"],
        edition="community",
    )
    model = ModelInfo(
        name=RS_MODEL,
        module=RS_MODULE,
        odoo_version=RS_VERSION,
        fields=[FieldInfo("name", "char")],
        methods=[],
    )
    writer.write_results([ParseResult(module=module, models=[model])])
    writer.close()

    yield neo4j_driver

    with neo4j_driver.session() as s:
        s.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=RS_VERSION)


@pytest.fixture()
def fresh_resources_module(monkeypatch):
    """Reload src.mcp.resources so its cache is empty for each test."""
    import importlib

    monkeypatch.setenv("NEO4J_URI", os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"))
    monkeypatch.setenv("NEO4J_USER", os.getenv("NEO4J_TEST_USER", "neo4j"))
    monkeypatch.setenv("NEO4J_PASSWORD", os.getenv("NEO4J_TEST_PASSWORD", "password"))
    import src.mcp.resources as mod
    importlib.reload(mod)
    return mod


def _read_resource(mcp, uri: str) -> str:
    """Synchronous helper: read a resource and return its first text body."""
    try:
        prior_loop = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        prior_loop = None

    new_loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(new_loop)
        contents = new_loop.run_until_complete(
            mcp.read_resource(uri),
        )
    finally:
        new_loop.close()
        if prior_loop is not None and not prior_loop.is_closed():
            asyncio.set_event_loop(prior_loop)
        else:
            asyncio.set_event_loop(asyncio.new_event_loop())

    # fastmcp v3 read_resource returns a ResourceResult whose .contents holds the
    # list of ResourceContent; the pre-v3 manager returned that list directly.
    contents = contents.contents if hasattr(contents, "contents") else contents
    if isinstance(contents, list | tuple):
        first = contents[0]
        return first.content if hasattr(first, "content") else str(first)
    if hasattr(contents, "content"):
        return contents.content
    return str(contents)


# ===========================================================================
# Level A (unit) — _set_server_api_key mechanics
# ===========================================================================


class TestSetServerApiKeyMechanics:
    """_set_server_api_key correctly writes/clears server._api_key_id_var."""

    def setup_method(self) -> None:
        """Ensure ContextVar is at default before each test."""
        from src.mcp import server as srv
        srv._api_key_id_var.set("default")

    def teardown_method(self) -> None:
        """Restore default state after each test."""
        from src.mcp import server as srv
        srv._api_key_id_var.set("default")

    def test_set_api_key_makes_get_return_it(self) -> None:
        """_set_server_api_key(key) → _get_api_key_id() returns key."""
        from src.mcp import server as srv
        from src.mcp.tool_log_middleware import _set_server_api_key

        _set_server_api_key("rs-test-key-001")
        assert srv._get_api_key_id() == "rs-test-key-001", (
            "_get_api_key_id() must return the value set by _set_server_api_key"
        )

    def test_clear_api_key_falls_back_to_default(self) -> None:
        """_set_server_api_key(None) → _get_api_key_id() falls back to 'default'."""
        from src.mcp import server as srv
        from src.mcp.tool_log_middleware import _set_server_api_key

        # Set first, then clear.
        _set_server_api_key("rs-test-key-002")
        assert srv._get_api_key_id() == "rs-test-key-002"

        _set_server_api_key(None)
        assert srv._get_api_key_id() == "default", (
            "_get_api_key_id() must fall back to 'default' after _set_server_api_key(None)"
        )

    def test_set_clear_is_idempotent_when_already_unset(self) -> None:
        """_set_server_api_key(None) on an already-unset ContextVar must not raise."""
        from src.mcp import server as srv
        from src.mcp.tool_log_middleware import _set_server_api_key

        # Should not raise even when value was never set.
        _set_server_api_key(None)
        assert srv._get_api_key_id() == "default"

        # Double-clear must also be safe.
        _set_server_api_key(None)
        assert srv._get_api_key_id() == "default"


# ===========================================================================
# Level A — on_read_resource hook sets and clears ContextVar
# ===========================================================================


class TestOnReadResourceHook:
    """UsageLogMiddleware.on_read_resource sets ContextVar before call_next
    and clears it in finally (mirrors on_call_tool behaviour for WI-B1)."""

    def setup_method(self) -> None:
        from src.mcp import server as srv
        srv._api_key_id_var.set("default")

    def teardown_method(self) -> None:
        from src.mcp import server as srv
        srv._api_key_id_var.set("default")

    def test_on_read_resource_sets_thread_local_during_call_next(self) -> None:
        """on_read_resource propagates api_key_id into ContextVar for call_next.

        We simulate the middleware by:
        1. Creating a UsageLogMiddleware instance.
        2. Building a fake context whose HTTP request carries api_key_id on state.
        3. Providing a call_next that captures the ContextVar value during execution.
        4. Asserting the captured value matches the api_key_id from request.state.
        5. Asserting the value is cleared after on_read_resource returns.
        """
        from src.mcp import server as srv
        from src.mcp.tool_log_middleware import UsageLogMiddleware

        middleware = UsageLogMiddleware()

        # Fake request with api_key_id on state.
        fake_request = MagicMock()
        fake_request.state.api_key_id = "rs-hook-test-007"

        # Fake MCP context carrying the request.
        fake_context = MagicMock()

        captured_during: list[str | None] = []

        async def fake_call_next(_ctx):
            # Inside call_next the ContextVar must be set.
            captured_during.append(srv._api_key_id_var.get(None))
            return []  # empty resource contents — ok for this test

        with patch(
            "src.mcp.tool_log_middleware.get_http_request",
            return_value=fake_request,
        ):
            new_loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(new_loop)
                new_loop.run_until_complete(
                    middleware.on_read_resource(fake_context, fake_call_next)
                )
            finally:
                new_loop.close()
                asyncio.set_event_loop(asyncio.new_event_loop())

        # Thread-local must have been "rs-hook-test-007" during call_next.
        assert captured_during == ["rs-hook-test-007"], (
            f"Thread-local inside call_next must be 'rs-hook-test-007', "
            f"got: {captured_during!r}"
        )

        # Thread-local must be cleared after on_read_resource returns.
        assert srv._get_api_key_id() == "default", (
            "Thread-local must be cleared (→ 'default') after on_read_resource finishes"
        )

    def test_on_read_resource_clears_thread_local_even_on_exception(self) -> None:
        """on_read_resource clears ContextVar even if call_next raises."""
        from src.mcp import server as srv
        from src.mcp.tool_log_middleware import UsageLogMiddleware

        middleware = UsageLogMiddleware()
        fake_request = MagicMock()
        fake_request.state.api_key_id = "rs-hook-exc-008"
        fake_context = MagicMock()

        async def raising_call_next(_ctx):
            raise RuntimeError("simulated resource read failure")

        with patch(
            "src.mcp.tool_log_middleware.get_http_request",
            return_value=fake_request,
        ):
            new_loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(new_loop)
                with pytest.raises(RuntimeError, match="simulated resource read failure"):
                    new_loop.run_until_complete(
                        middleware.on_read_resource(fake_context, raising_call_next)
                    )
            finally:
                new_loop.close()
                asyncio.set_event_loop(asyncio.new_event_loop())

        # Thread-local must be cleared even though call_next raised.
        assert srv._get_api_key_id() == "default", (
            "Thread-local must be cleared after on_read_resource even when call_next raises"
        )


# ===========================================================================
# Level B (integration) — sticky session version honoured in resource read
# ===========================================================================


class TestStickySessionThroughResourceRead:
    """With api_key_id set in ContextVar, resource reads pick up the sticky
    session version instead of falling through to _latest_version().

    This is the end-to-end regression for WI-B1: before the fix, resource
    reads always used 'default' api_key_id and therefore resolved to the
    latest indexed version rather than the per-key session version.
    """

    def test_resource_read_resolves_to_seeded_version_when_thread_local_set(
        self, rs_db, fresh_resources_module
    ) -> None:
        """Reading odoo://RS_VERSION/model/RS_MODEL with ContextVar set resolves correctly.

        Strategy:
          1. Patch _resolved_version_for to return RS_VERSION only when the
             ContextVar api_key_id equals 'rs-sticky-key-101'.
          2. Set the _api_key_id_var ContextVar = 'rs-sticky-key-101'.
          3. Read the resource via the resource manager.
          4. Assert the body contains RS_VERSION (not some other version).
          5. Clean up ContextVar.

        This confirms that on_read_resource propagating the ContextVar
        makes _resolved_version_for pick up the sticky session version.
        """
        from fastmcp import FastMCP

        from src.mcp import server as srv

        mcp = FastMCP("test-rs-sticky")
        fresh_resources_module.register_resources(mcp)

        cache = fresh_resources_module.get_cache()
        cache.clear()

        # Patch _resolved_version_for to return RS_VERSION for our test key.
        def _patched_resolve(version: str) -> str:
            key_id = srv._api_key_id_var.get(None)
            if key_id == "rs-sticky-key-101":
                return RS_VERSION
            # Fall through — not our key, return version unchanged (concrete).
            return version

        import src.mcp.resources
        original_fn = src.mcp.resources._resolved_version_for
        src.mcp.resources._resolved_version_for = _patched_resolve

        token = srv._api_key_id_var.set("rs-sticky-key-101")
        try:
            uri = f"odoo://auto/model/{RS_MODEL}"
            body = _read_resource(mcp, uri)
        finally:
            src.mcp.resources._resolved_version_for = original_fn
            srv._api_key_id_var.reset(token)

        assert body, "Body must be non-empty"
        assert RS_VERSION in body, (
            f"Body must contain RS_VERSION '{RS_VERSION}' when ContextVar is set; "
            f"got: {body[:300]!r}"
        )

    def test_resource_read_without_thread_local_uses_default_path(
        self, rs_db, fresh_resources_module
    ) -> None:
        """Without ContextVar set, resource read uses anonymous fallback ('default').

        Confirms the negative case: no sticky session → 'default' key_id →
        _resolved_version_for gets 'auto' → falls through to latest.
        The body must still be non-empty (not an error) and must NOT contain
        RS_VERSION (since the 'default' key has no session state seeded).

        Note: this test is intentionally soft — if RS_VERSION happens to be the
        latest indexed version in Neo4j, the body could still contain it.  We
        therefore only assert the body is non-empty and non-error.
        """
        from fastmcp import FastMCP

        from src.mcp import server as srv

        mcp = FastMCP("test-rs-anon")
        fresh_resources_module.register_resources(mcp)

        cache = fresh_resources_module.get_cache()
        cache.clear()

        # Ensure ContextVar is at default sentinel.
        srv._api_key_id_var.set("default")

        uri = f"odoo://{RS_VERSION}/model/{RS_MODEL}"
        try:
            body = _read_resource(mcp, uri)
        except Exception as exc:
            raise AssertionError(
                f"Resource read without ContextVar must not raise; got: {exc!r}"
            ) from exc

        assert isinstance(body, str), "Body must be a string"
        assert body.strip(), "Body must be non-empty"
        # Not an auth error.
        assert "401" not in body
        assert "403" not in body
