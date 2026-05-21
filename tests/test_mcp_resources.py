# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for ``src.mcp.resources`` — odoo:// URI handlers (WI-F1).

Covers AC-F1-4 + AC-F1-5:

  AC-F1-4: ≥5 integration tests for 3 handlers (model, field, module).
           Each seeds Neo4j with a tiny fixture under ``F1_99.0`` and asserts
           that ``resources/read`` returns the expected markdown body.

  AC-F1-5: When ``set_active_version_db(api_key, '17.0')`` was called,
           ``odoo://auto/model/sale.order`` resolves to ``17.0`` via the
           session resolver (Wave E, ADR-0029).

Markers:
  - All tests are marked ``neo4j``.
  - AC-F1-5 also requires Postgres for ``api_key_session_state``.

DB isolation:
  - Neo4j version ``F1_99.0`` — wipe before/after each test.
  - Postgres api_key_id range ``[9701]`` — distinct from E2/E4 ranges.
"""

from __future__ import annotations

import asyncio
import importlib
import os
from contextlib import contextmanager
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

F1_VERSION = "F1_99.0"
F1_MODULE = "f1_sale"
F1_MODEL = "f1.order"
F1_FIELD = "amount_total"
F1_PARTNER_MODEL = "f1.partner"  # used to assert "Inherits from:" branch absence

F1_SESSION_KEY = "9701"


pytestmark = pytest.mark.neo4j


# ---------------------------------------------------------------------------
# Fixtures — seed a tiny Neo4j fixture for F1_VERSION
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def f1_db(neo4j_driver):
    """Seed one Module, one Model with 2 Fields + 1 Method under F1_VERSION."""
    # Wipe any leftover F1 data from previous runs.
    with neo4j_driver.session() as s:
        s.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n",
            v=F1_VERSION,
        )

    from src.indexer.models import (
        FieldInfo,
        MethodInfo,
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
        name=F1_MODULE,
        odoo_version=F1_VERSION,
        repo="odoo_test",
        path="/tmp/f1_sale",
        depends=["base"],
        edition="community",
    )
    model = ModelInfo(
        name=F1_MODEL,
        module=F1_MODULE,
        odoo_version=F1_VERSION,
        fields=[
            FieldInfo(F1_FIELD, "monetary", compute="_compute_total", stored=True),
            FieldInfo("partner_id", "many2one"),
        ],
        methods=[
            MethodInfo(
                name="action_confirm",
                decorators=["api.multi"],
                has_super_call=True,
            ),
        ],
    )
    writer.write_results([ParseResult(module=module, models=[model])])
    writer.close()

    yield neo4j_driver

    # Teardown
    with neo4j_driver.session() as s:
        s.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n",
            v=F1_VERSION,
        )


@pytest.fixture()
def fresh_resources_module(monkeypatch):
    """Reload src.mcp.resources so its cache is empty for each test."""
    monkeypatch.setenv(
        "NEO4J_URI", os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
    )
    monkeypatch.setenv(
        "NEO4J_USER", os.getenv("NEO4J_TEST_USER", "neo4j"),
    )
    monkeypatch.setenv(
        "NEO4J_PASSWORD", os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    import src.mcp.resources as mod
    importlib.reload(mod)
    return mod


@pytest.fixture()
def mcp_with_resources(f1_db, fresh_resources_module):
    """Create a fresh FastMCP, register the 7 odoo:// handlers, return it."""
    from fastmcp import FastMCP

    mcp = FastMCP("test-resources")
    fresh_resources_module.register_resources(mcp)
    return mcp


def _read(mcp, uri: str) -> str:
    """Synchronous helper: read a resource and return its first text body.

    We deliberately avoid :func:`asyncio.run` here — it clears the thread's
    default event loop on exit, which breaks downstream tests that call
    ``asyncio.get_event_loop()`` (e.g. ``test_totp.py``).  Instead, snapshot
    the existing loop (if any), create a private loop for the read, then
    restore the original policy state so the suite's event-loop state is
    unchanged after the call.
    """
    # Snapshot the prior loop so we restore it after the read.
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
        # Restore the prior loop so tests that rely on a thread-default
        # loop (test_totp.py) see the same state they had before.
        if prior_loop is not None and not prior_loop.is_closed():
            asyncio.set_event_loop(prior_loop)
        else:
            # No prior loop, or it was closed — install a fresh one so
            # downstream callers of asyncio.get_event_loop() do not crash.
            asyncio.set_event_loop(asyncio.new_event_loop())

    # FastMCP returns an iterable of ReadResourceContents with .content set;
    # for str-returning handlers this becomes a single text body.
    if isinstance(contents, list | tuple):
        first = contents[0]
        return first.content if hasattr(first, "content") else str(first)
    if hasattr(contents, "content"):
        return contents.content
    return str(contents)


# ---------------------------------------------------------------------------
# Test 1 — model handler returns markdown tree
# ---------------------------------------------------------------------------


def test_model_resource_returns_markdown_tree(mcp_with_resources) -> None:
    """odoo://F1_99.0/model/f1.order returns the resolve_model tree."""
    uri = f"odoo://{F1_VERSION}/model/{F1_MODEL}"
    body = _read(mcp_with_resources, uri)

    assert F1_MODEL in body, f"Body must mention model name; got: {body[:200]!r}"
    assert F1_VERSION in body, "Body must mention the version"
    assert F1_MODULE in body, "Body must mention the defining module"
    assert "Fields:" in body, "Body must include the Fields: row from resolve_model"


# ---------------------------------------------------------------------------
# Test 2 — field handler returns markdown tree
# ---------------------------------------------------------------------------


def test_field_resource_returns_markdown_tree(mcp_with_resources) -> None:
    """odoo://F1_99.0/field/f1.order/amount_total returns resolve_field tree."""
    uri = f"odoo://{F1_VERSION}/field/{F1_MODEL}/{F1_FIELD}"
    body = _read(mcp_with_resources, uri)

    assert F1_FIELD in body, "Body must include the field name"
    assert F1_MODEL in body, "Body must include the host model"
    assert "Type:" in body, "Body must include the Type: branch"
    # Field is monetary in our fixture.
    assert "monetary" in body.lower(), (
        f"Body must include the indexed field type 'monetary'; got: {body[:300]!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — module handler returns describe_module tree
# ---------------------------------------------------------------------------


def test_module_resource_returns_markdown_tree(mcp_with_resources) -> None:
    """odoo://F1_99.0/module/f1_sale returns the describe_module tree."""
    uri = f"odoo://{F1_VERSION}/module/{F1_MODULE}"
    body = _read(mcp_with_resources, uri)

    assert F1_MODULE in body
    assert F1_VERSION in body
    assert "Manifest:" in body, "describe_module tree includes a Manifest: branch"
    assert "Depends" in body, "describe_module tree lists Depends row"


# ---------------------------------------------------------------------------
# Test 4 — cache hit short-circuits a second read
# ---------------------------------------------------------------------------


def test_cache_hit_short_circuits_second_read(
    mcp_with_resources, fresh_resources_module,
) -> None:
    """Second read of the same URI hits the cache and skips _render_model."""
    cache = fresh_resources_module.get_cache()
    cache.clear()
    assert len(cache) == 0

    uri = f"odoo://{F1_VERSION}/model/{F1_MODEL}"

    # Spy on the underlying _render_model — second read must NOT call it.
    call_count = 0
    real_render = fresh_resources_module._render_model

    def _spy_render(version: str, name: str):
        nonlocal call_count
        call_count += 1
        return real_render(version, name)

    with patch.object(fresh_resources_module, "_render_model", _spy_render):
        # Re-register handlers so they bind to the spied helper.
        from fastmcp import FastMCP
        mcp = FastMCP("spy-test")
        fresh_resources_module.register_resources(mcp)

        body1 = _read(mcp, uri)
        body2 = _read(mcp, uri)

    assert body1 == body2, "Cache must return identical body on hit"
    assert call_count == 1, (
        f"_render_model must run exactly once across 2 reads; got {call_count}"
    )
    assert uri in cache, "URI must be stored in the cache after first read"


# ---------------------------------------------------------------------------
# Test 5 — missing entity returns a not-found tree (not an exception)
# ---------------------------------------------------------------------------


def test_unknown_model_returns_not_found_tree(mcp_with_resources) -> None:
    """odoo://F1_99.0/model/does.not.exist returns a 'not found' body, no exception."""
    uri = f"odoo://{F1_VERSION}/model/does.not.exist"
    body = _read(mcp_with_resources, uri)
    assert "not found" in body.lower(), (
        f"Expected a 'not found' message body; got: {body[:200]!r}"
    )


# ---------------------------------------------------------------------------
# Test 6 (AC-F1-5) — sentinel 'auto' resolves via session state
# ---------------------------------------------------------------------------


@pytest.mark.postgres
def test_auto_version_uses_session_state(
    mcp_with_resources, fresh_resources_module, pg_conn,
) -> None:
    """odoo://auto/model/<name> resolves via the per-API-key session version."""
    from src.db.migrate import run_migrations
    from src.mcp.session import _cache as session_cache
    from src.mcp.session import set_active_version_db

    run_migrations(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS api_key_session_state ("
            "api_key_id INTEGER PRIMARY KEY, odoo_version TEXT, "
            "profile_name TEXT, "
            "updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now())",
        )
        cur.execute(
            "DELETE FROM api_key_session_state WHERE api_key_id = %s",
            (int(F1_SESSION_KEY),),
        )

    session_cache.clear()
    fresh_resources_module.get_cache().clear()

    @contextmanager
    def _checkout_pg():
        yield pg_conn

    # 1.  Stamp the session: this API key → F1_VERSION.
    with patch("src.mcp.server._checkout_pg", _checkout_pg):
        set_active_version_db(F1_SESSION_KEY, F1_VERSION)
        session_cache.clear()

        # 2. Pin the thread-local API key so resources read the right session.
        from src.mcp import server as _srv
        _srv._api_key_id_local.value = F1_SESSION_KEY
        try:
            # 3.  Read with sentinel 'auto' — must resolve to F1_VERSION.
            body = _read(
                mcp_with_resources,
                f"odoo://auto/model/{F1_MODEL}",
            )
        finally:
            # Always release the thread-local.
            try:
                del _srv._api_key_id_local.value
            except AttributeError:
                pass

    # Cleanup row.
    with pg_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM api_key_session_state WHERE api_key_id = %s",
            (int(F1_SESSION_KEY),),
        )

    assert F1_MODEL in body
    assert F1_VERSION in body, (
        f"sentinel 'auto' must resolve to session version {F1_VERSION}; "
        f"got body: {body[:300]!r}"
    )
