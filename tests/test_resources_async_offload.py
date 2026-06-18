# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure-unit tests: odoo:// resource handlers offload off the event loop (#284).

No Docker / Neo4j — the render function is fully stubbed. This file has NO
module-level marker so it runs in the fast unit lane (`make test`).

Business contract protected (ADR-0046 anti-wedge, the #227 root cause): FastMCP
2.x calls sync resource handlers directly on the event loop thread, so a blocking
cache-miss render would freeze ALL concurrent MCP requests. After #284 every
``@mcp.resource`` handler is ``async def`` + ``asyncio.to_thread``, so a slow
render no longer blocks the loop.
"""

from __future__ import annotations

import asyncio
import importlib
import threading

import pytest


@pytest.fixture()
def resources_mod():
    """Fresh src.mcp.resources with an empty cache."""
    import src.mcp.resources as mod
    importlib.reload(mod)
    return mod


def _register(mod):
    from fastmcp import FastMCP

    mcp = FastMCP("async-offload-test")
    mod.register_resources(mcp)
    return mcp


def test_all_resource_handlers_are_coroutine_functions(resources_mod):
    """Every registered odoo:// handler must be an async (coroutine) function.

    A sync handler would be invoked on the event loop thread by FastMCP — the
    #227 wedge. This asserts the async conversion held for ALL 7 handlers, not
    just the model one.
    """
    mcp = _register(resources_mod)
    templates = asyncio.run(mcp.list_resource_templates())
    assert templates, "no resource templates registered"
    for template in templates:
        uri = template.uri_template
        assert asyncio.iscoroutinefunction(template.fn), (
            f"resource handler for {uri!r} must be async def (offload off loop); "
            f"got sync {template.fn!r}"
        )
    # Sanity: all 7 odoo:// kinds present.
    kinds = {t.uri_template.split("/")[3] for t in templates}
    assert {"model", "field", "method", "module", "view", "pattern", "stylesheet"} <= kinds


def test_slow_render_does_not_block_concurrent_loop_task(resources_mod, monkeypatch):
    """A blocking model render must not freeze a concurrent event-loop task.

    Stub ``_render_model`` to block on a threading.Event for a bounded window.
    While the resource read is in-flight (offloaded to a worker thread), a
    concurrent asyncio task increments a counter every 1ms. If the handler ran
    the blocking render ON the loop, the counter could not advance until the
    render returned — so a non-trivial tick count proves the loop stayed free.
    """
    release = threading.Event()
    render_entered = threading.Event()

    def _blocking_render(version, name):
        render_entered.set()
        # Block the worker thread (NOT the loop) until released or timeout.
        release.wait(timeout=2.0)
        return f"model({name!r}, {version})\n└─ ok", resources_mod.MIME_MARKDOWN

    # Bypass sentinel resolution (no Neo4j) — return the version unchanged.
    monkeypatch.setattr(resources_mod, "_resolved_version_for", lambda v: v)
    monkeypatch.setattr(resources_mod, "_render_model", _blocking_render)

    mcp = _register(resources_mod)

    async def _drive():
        ticks = 0

        async def _ticker():
            nonlocal ticks
            while not render_entered.is_set() or not release.is_set():
                ticks += 1
                await asyncio.sleep(0.001)

        ticker = asyncio.create_task(_ticker())
        read = asyncio.create_task(
            mcp.read_resource("odoo://17.0/model/sale.order")
        )
        # Wait until the worker thread is inside the blocking render, then let
        # the ticker run a few iterations to prove the loop is alive.
        for _ in range(200):
            if render_entered.is_set():
                break
            await asyncio.sleep(0.001)
        assert render_entered.is_set(), "render never started (offload broken?)"
        ticks_during_block = ticks
        await asyncio.sleep(0.02)  # loop must keep ticking while render blocks
        progressed = ticks - ticks_during_block
        release.set()
        body = await read
        ticker.cancel()
        return progressed, body

    progressed, contents = asyncio.run(_drive())

    assert progressed > 0, (
        "event loop was BLOCKED during the render — the handler did not offload "
        "(this is the #227 wedge regression)"
    )
    # FastMCP returns an iterable of ReadResourceContents; .content holds the body.
    first = contents[0] if isinstance(contents, list | tuple) else contents
    text = first.content if hasattr(first, "content") else str(first)
    assert "sale.order" in text, f"unexpected resource body: {text!r}"


def test_model_resource_timeout_records_metric_once(resources_mod, monkeypatch):
    """A model-resource timeout records nonorm_query_timeout_total once (#284 D).

    The render raises OrmQueryTimeout (the _reraise_timeout=True contract). The
    resource handler must catch it, record the metric exactly once under
    tool='model_inspect', return the clean message, and NOT cache it.
    """
    from prometheus_client import REGISTRY

    from src.mcp.orm import OrmQueryTimeout

    def _label_value():
        return REGISTRY.get_sample_value(
            "nonorm_query_timeout_total", {"tool": "model_inspect"}
        ) or 0.0

    def _timing_out_render(version, name):
        raise OrmQueryTimeout("Model resolution timed out — retry the dense model.")

    monkeypatch.setattr(resources_mod, "_resolved_version_for", lambda v: v)
    monkeypatch.setattr(resources_mod, "_render_model", _timing_out_render)

    mcp = _register(resources_mod)
    before = _label_value()
    contents = asyncio.run(
        mcp.read_resource("odoo://17.0/model/sale.order")
    )
    after = _label_value()

    assert after == before + 1, (
        f"resource-path timeout must increment the counter once; "
        f"before={before} after={after}"
    )
    # fastmcp v3 read_resource returns a ResourceResult whose .contents holds the
    # list of ResourceContent; the pre-v3 manager returned that list directly.
    contents = contents.contents if hasattr(contents, "contents") else contents
    first = contents[0] if isinstance(contents, list | tuple) else contents
    text = first.content if hasattr(first, "content") else str(first)
    assert "timed out" in text.lower(), f"expected clean timeout body; got {text!r}"
    # The transient timeout must NOT be cached.
    cache = resources_mod.get_cache()
    assert len(cache) == 0, "timeout body must never be cached"
