# SPDX-License-Identifier: AGPL-3.0-or-later
"""PR-1 resource-unification timeout tests (no live DB).

Covers the timeout-hardening design §3 (resource contract unification) + §5.3 /
§5.5:
  * Each of the 6 non-model resources (field/method/module/view/pattern/
    stylesheet) returns a CLEAN body on a tx-timeout AND records
    ``nonorm_query_timeout_total`` exactly once (mirrors the already-correct
    model resource).
  * §5.5 ANTI-POISON regression — a resource that times out on the first read
    must NOT cache the timeout body; a second read (resolver recovered) returns
    the REAL body. This is the single most important resource invariant.

No Docker — the underlying server resolver / inline render is monkeypatched to
raise OrmQueryTimeout (the contract the bounded helpers produce on tx-timeout).
"""

from __future__ import annotations

import asyncio
import importlib

import pytest


@pytest.fixture()
def resources_mod():
    """Fresh src.mcp.resources with an empty module-level cache."""
    import src.mcp.resources as mod
    importlib.reload(mod)
    return mod


def _register(mod):
    from fastmcp import FastMCP

    mcp = FastMCP("timeout-resource-test")
    mod.register_resources(mcp)
    return mcp


def _metric_value(tool: str) -> float:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(
        "nonorm_query_timeout_total", {"tool": tool}
    ) or 0.0


def _read(mcp, uri: str) -> str:
    contents = asyncio.run(mcp.read_resource(uri))
    # fastmcp v3 read_resource returns a ResourceResult whose .contents holds the
    # list of ResourceContent; the pre-v3 manager returned that list directly.
    contents = contents.contents if hasattr(contents, "contents") else contents
    first = contents[0] if isinstance(contents, list | tuple) else contents
    return first.content if hasattr(first, "content") else str(first)


# The 8 non-model resources (6 original + 2 new WI-4 test resources), each
# parametrized with:
#   uri, the resources.py symbol to monkeypatch so the resolver times out,
#   the metric tool-name the handler must record under (design §3.1).
_RESOURCE_CASES = [
    pytest.param(
        "odoo://17.0/field/dense.model/x",
        "_render_field", "model_inspect", id="field",
    ),
    pytest.param(
        "odoo://17.0/method/dense.model/m",
        "_render_method", "model_inspect", id="method",
    ),
    pytest.param(
        "odoo://17.0/module/sale",
        "_render_module", "module_inspect", id="module",
    ),
    pytest.param(
        "odoo://17.0/view/sale.view_order_form",
        "_render_view", "entity_lookup", id="view",
    ),
    pytest.param(
        "odoo://17.0/pattern/pat-1",
        "_render_pattern", "suggest_pattern", id="pattern",
    ),
    pytest.param(
        "odoo://17.0/stylesheet/web/static/src/scss/foo.scss",
        "_render_stylesheet", "resolve_stylesheet", id="stylesheet",
    ),
    # WI-4 DEFECT J: _render_test_class and _render_testcoverage must pass
    # _reraise_timeout=True so a transient OrmQueryTimeout propagates BEFORE
    # the LRU put (no-poison contract), matching all 6 existing resource renderers.
    # Red-before-fix: without _reraise_timeout=True the timeout body IS cached
    # and the second read (after recovery) returns the stale error, not the real body.
    pytest.param(
        "odoo://17.0/test/sale/TestSaleOrder",
        "_render_test_class", "test_class_inspect", id="test_class",
    ),
    pytest.param(
        "odoo://17.0/testcoverage/sale.order",
        "_render_testcoverage", "tests_covering", id="testcoverage",
    ),
]


@pytest.mark.parametrize("uri,render_attr,metric_tool", _RESOURCE_CASES)
def test_resource_clean_body_and_metric_once_on_timeout(
    resources_mod, monkeypatch, uri, render_attr, metric_tool,
):
    """Each resource: tx-timeout → clean body + metric recorded exactly once."""
    from src.mcp.orm import OrmQueryTimeout

    monkeypatch.setattr(resources_mod, "_resolved_version_for", lambda v: v)

    def _timing_out(*a, **k):
        raise OrmQueryTimeout("Resource resolution timed out — retry shortly.")

    monkeypatch.setattr(resources_mod, render_attr, _timing_out)

    mcp = _register(resources_mod)
    before = _metric_value(metric_tool)
    text = _read(mcp, uri)
    after = _metric_value(metric_tool)

    assert "timed out" in text.lower(), f"expected clean timeout body; got {text!r}"
    for token in ("MATCH ", "RETURN ", "session.run", "Traceback"):
        assert token not in text, f"leaked internal text {token!r}: {text!r}"
    assert after == before + 1, (
        f"resource timeout must record the metric once under {metric_tool!r}; "
        f"before={before} after={after}"
    )


@pytest.mark.parametrize("uri,render_attr,metric_tool", _RESOURCE_CASES)
def test_resource_timeout_body_is_never_cached(
    resources_mod, monkeypatch, uri, render_attr, metric_tool,
):
    """§5.5 ANTI-POISON: a timeout body is never cached.

    First read times out (clean body, NOT cached). Then the resolver "recovers"
    (monkeypatch flipped to a healthy render) and the second read returns the
    REAL body — proving the transient timeout never poisoned the LRU for the TTL.
    """
    from src.mcp.orm import OrmQueryTimeout

    monkeypatch.setattr(resources_mod, "_resolved_version_for", lambda v: v)

    state = {"healthy": False}
    real_body = "REAL-BODY-AFTER-RECOVERY"

    def _maybe_timeout(*a, **k):
        if state["healthy"]:
            return real_body, resources_mod.MIME_MARKDOWN
        raise OrmQueryTimeout("Resource resolution timed out — retry shortly.")

    monkeypatch.setattr(resources_mod, render_attr, _maybe_timeout)

    mcp = _register(resources_mod)

    # First read: times out, returns a clean degraded body.
    first = _read(mcp, uri)
    assert "timed out" in first.lower(), f"expected timeout body; got {first!r}"

    # Recover, read again: MUST recompute and return the real body (no poison).
    state["healthy"] = True
    second = _read(mcp, uri)
    assert second == real_body, (
        f"timeout body was POISONED into the cache — second read returned "
        f"{second!r} instead of the recovered real body"
    )
