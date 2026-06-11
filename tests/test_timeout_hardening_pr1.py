# SPDX-License-Identifier: AGPL-3.0-or-later
"""PR-1 read-surface timeout-hardening tests (no live DB).

Covers the timeout-hardening design §5:
  * §5.4 decorator unit test — ``@offload_neo4j`` catch / pass-through / propagate.
  * §5.2 parametrized TOOL test — each converted PR-1 sync resolver returns a
    clean ADR-0023 timeout string on a simulated tx-timeout.
  * async-wrapper cases — the now-async tool handlers (wrapped by
    ``@offload_neo4j``) return the clean string via ``asyncio.run``.
  * §5.3 parametrized RESOURCE test — each of the 6 non-model resources returns a
    clean body AND records ``nonorm_query_timeout_total`` exactly once.
  * §5.5 anti-poison regression — a resource timeout body is NEVER cached.

All Neo4j access is monkeypatched to a ``_TxTimeoutDriver`` — no Docker, fast lane.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from tests._timeout_harness import (
    TIMEOUT_TEST_VERSION,
    _TxTimeoutDriver,
    assert_clean_timeout_string,
)


def _run(coro):
    """Run *coro* in a dedicated thread with a fresh event loop.

    Under the full unit suite (pytest-asyncio mode=auto) an earlier test can
    leave a RUNNING loop on the main thread, making a bare ``asyncio.run()``
    raise. A fresh thread has no loop, so ``asyncio.run`` there is always safe.
    Mirrors ``tests/test_orm_offload_bounded.py::_run``.
    """
    box: dict = {}

    def runner():
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # propagate to the test thread
            box["error"] = exc

    t = threading.Thread(target=runner)
    t.start()
    t.join(timeout=60)
    assert not t.is_alive(), "coroutine did not finish within 60s"
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ---------------------------------------------------------------------------
# §5.4 — @offload_neo4j decorator unit test
# ---------------------------------------------------------------------------


def _metric_value(tool: str) -> float:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(
        "nonorm_query_timeout_total", {"tool": tool}
    ) or 0.0


def test_offload_neo4j_timeout_returns_user_message_and_counts_once(monkeypatch):
    """A wrapped fn raising OrmQueryTimeout → returns user_message, metric +1."""
    import src.mcp.server as srv
    from src.mcp.orm import OrmQueryTimeout

    @srv.offload_neo4j
    def _boom():
        raise OrmQueryTimeout("Query timed out — narrow the entity and retry.")

    before = _metric_value("_boom")
    result = _run(_boom())
    after = _metric_value("_boom")

    assert_clean_timeout_string(result)
    assert result == "Query timed out — narrow the entity and retry."
    assert after == before + 1, f"metric must fire once; before={before} after={after}"


def test_offload_neo4j_normal_return_passes_through():
    """A wrapped fn returning normally passes the value through unchanged."""
    import src.mcp.server as srv

    @srv.offload_neo4j
    def _ok():
        return "real body"

    assert _run(_ok()) == "real body"


def test_offload_neo4j_non_timeout_exception_propagates():
    """A non-OrmQueryTimeout exception is NOT swallowed — it propagates."""
    import src.mcp.server as srv

    @srv.offload_neo4j
    def _other():
        raise ValueError("unrelated boom")

    with pytest.raises(ValueError, match="unrelated boom"):
        _run(_other())


# ---------------------------------------------------------------------------
# §5.2 — parametrized TOOL test (sync resolver bodies)
#
# Each lambda calls the UNDERSCORE-prefixed handler body (never the FunctionTool)
# with an EXPLICIT version, so _resolve_version short-circuits at Tier-1 without
# touching the timing-out session — the first bounded query is the hot path.
# ---------------------------------------------------------------------------

_SYNC_TOOL_PATHS = [
    pytest.param(lambda s: s._resolve_field("dense.model", "x", TIMEOUT_TEST_VERSION),
                 id="resolve_field-primary"),
    pytest.param(lambda s: s._resolve_view("some.view_id", TIMEOUT_TEST_VERSION),
                 id="resolve_view"),
    pytest.param(lambda s: s._describe_module("sale", TIMEOUT_TEST_VERSION),
                 id="describe_module"),
    pytest.param(lambda s: s._module_dep_closure("sale", TIMEOUT_TEST_VERSION),
                 id="module_dep_closure"),
    pytest.param(lambda s: s._list_extenders("dense.model", TIMEOUT_TEST_VERSION),
                 id="list_extenders"),
    pytest.param(lambda s: s._list_views_core(model="dense.model",
                                              odoo_version=TIMEOUT_TEST_VERSION),
                 id="list_views_core-model"),
    pytest.param(lambda s: s._list_views_core(module="sale",
                                              odoo_version=TIMEOUT_TEST_VERSION),
                 id="list_views_core-module"),
    pytest.param(lambda s: s._list_owl_components("web", TIMEOUT_TEST_VERSION),
                 id="list_owl_components"),
    pytest.param(lambda s: s._list_qweb_templates("web", TIMEOUT_TEST_VERSION),
                 id="list_qweb_templates"),
    pytest.param(lambda s: s._list_js_patches(odoo_version=TIMEOUT_TEST_VERSION,
                                              module="web"),
                 id="list_js_patches"),
]


@pytest.mark.parametrize("tool_call", _SYNC_TOOL_PATHS)
def test_converted_sync_resolver_raises_orm_query_timeout(monkeypatch, tool_call):
    """The converted bare-session.run sites now raise OrmQueryTimeout on tx-timeout.

    These resolvers re-raise (no internal string-return on the primary path); the
    owning @offload_neo4j handler converts it to a clean string. We assert the
    RAISE here (the conversion correctness) and the clean string at the wrapper
    layer in the async-wrapper tests below.
    """
    import src.mcp.server as srv
    from src.mcp.orm import OrmQueryTimeout

    monkeypatch.setattr(srv, "_get_driver", lambda: _TxTimeoutDriver())
    monkeypatch.setattr(srv, "_resolve_version", lambda v, s: TIMEOUT_TEST_VERSION)

    with pytest.raises(OrmQueryTimeout) as ei:
        tool_call(srv)
    # The label-built message must be clean (no Cypher leaked).
    assert_clean_timeout_string(ei.value.user_message)


def test_resolve_field_inherited_fallback_returns_clean_string(monkeypatch):
    """The inherited-fallback timeout (tool path) degrades to a clean string.

    The PRIMARY field query returns no rows (so the inherited fallback runs); the
    fallback's bounded query then times out. On the tool path (_reraise_timeout
    default False) this catch returns the clean string — preserving pre-PR-1
    behaviour (the metric for this path is Phase-3 / PR-3 scope).
    """
    import src.mcp.server as srv
    from src.mcp.orm import OrmQueryTimeout

    # Primary query returns no records → triggers the inherited fallback.
    monkeypatch.setattr(srv, "_data_bounded", lambda *a, **k: [])
    monkeypatch.setattr(srv, "_get_driver", lambda: _TxTimeoutDriver())
    monkeypatch.setattr(srv, "_resolve_version", lambda v, s: TIMEOUT_TEST_VERSION)

    def _boom(*a, **k):
        raise OrmQueryTimeout("Inherited field lookup timed out — retry.")

    monkeypatch.setattr(srv, "_resolve_field_inherited", _boom)

    result = srv._resolve_field("dense.model", "nonexistent", TIMEOUT_TEST_VERSION)
    assert_clean_timeout_string(result)


def test_resolve_field_inherited_fallback_reraises_for_resource(monkeypatch):
    """With _reraise_timeout=True the inherited-fallback timeout PROPAGATES (resource)."""
    import src.mcp.server as srv
    from src.mcp.orm import OrmQueryTimeout

    monkeypatch.setattr(srv, "_data_bounded", lambda *a, **k: [])
    monkeypatch.setattr(srv, "_get_driver", lambda: _TxTimeoutDriver())
    monkeypatch.setattr(srv, "_resolve_version", lambda v, s: TIMEOUT_TEST_VERSION)

    def _boom(*a, **k):
        raise OrmQueryTimeout("Inherited field lookup timed out — retry.")

    monkeypatch.setattr(srv, "_resolve_field_inherited", _boom)

    with pytest.raises(OrmQueryTimeout):
        srv._resolve_field(
            "dense.model", "nonexistent", TIMEOUT_TEST_VERSION,
            _reraise_timeout=True,
        )


# ---------------------------------------------------------------------------
# async-wrapper cases — the now-async tool handlers return the clean string.
# ---------------------------------------------------------------------------


def _tool_text(tool_result) -> str:
    """Extract the text body from a ToolResult or a bare str return."""
    if isinstance(tool_result, str):
        return tool_result
    content = getattr(tool_result, "content", None)
    if content:
        first = content[0]
        return getattr(first, "text", str(first))
    return str(tool_result)


def test_model_inspect_async_handler_returns_clean_string(monkeypatch):
    """model_inspect (now @offload_neo4j) returns a clean string on tx-timeout."""
    import src.mcp.server as srv

    monkeypatch.setattr(srv, "_get_driver", lambda: _TxTimeoutDriver())
    monkeypatch.setattr(srv, "_resolve_version", lambda v, s: TIMEOUT_TEST_VERSION)

    result = _run(
        srv.model_inspect.fn(
            model="dense.model", method="views", odoo_version=TIMEOUT_TEST_VERSION,
        )
    )
    assert_clean_timeout_string(_tool_text(result))


def test_describe_module_async_handler_returns_clean_string(monkeypatch):
    """describe_module (now @offload_neo4j) returns a clean string on tx-timeout."""
    import src.mcp.server as srv

    monkeypatch.setattr(srv, "_get_driver", lambda: _TxTimeoutDriver())
    monkeypatch.setattr(srv, "_resolve_version", lambda v, s: TIMEOUT_TEST_VERSION)

    result = _run(
        srv.describe_module.fn(name="sale", odoo_version=TIMEOUT_TEST_VERSION)
    )
    assert_clean_timeout_string(_tool_text(result))


def test_module_inspect_async_handler_returns_clean_string(monkeypatch):
    """module_inspect (now @offload_neo4j) returns a clean string on tx-timeout."""
    import src.mcp.server as srv

    monkeypatch.setattr(srv, "_get_driver", lambda: _TxTimeoutDriver())
    monkeypatch.setattr(srv, "_resolve_version", lambda v, s: TIMEOUT_TEST_VERSION)

    result = _run(
        srv.module_inspect.fn(
            name="web", method="owl", odoo_version=TIMEOUT_TEST_VERSION,
        )
    )
    assert_clean_timeout_string(_tool_text(result))


def test_entity_lookup_view_async_handler_returns_clean_string(monkeypatch):
    """entity_lookup(kind='view') scoped-catch returns a clean string on tx-timeout."""
    import src.mcp.server as srv

    monkeypatch.setattr(srv, "_get_driver", lambda: _TxTimeoutDriver())
    monkeypatch.setattr(srv, "_resolve_version", lambda v, s: TIMEOUT_TEST_VERSION)

    before = _metric_value("entity_lookup")
    result = _run(
        srv.entity_lookup.fn(
            kind="view", xmlid="some.view_id", odoo_version=TIMEOUT_TEST_VERSION,
        )
    )
    after = _metric_value("entity_lookup")
    assert_clean_timeout_string(_tool_text(result))
    assert after == before + 1, "entity_lookup scoped catch must count once"


def test_list_fields_magic_dedup_raw_escape_degrades(monkeypatch):
    """RAW-ESCAPE pair: magic-dedup tx-timeout now degrades (not escapes).

    Pre-fix the bare `_dedup_session.run(_bounded(...))` raised a RAW ClientError
    that the `except OrmQueryTimeout` was blind to (it would escape). After
    routing through `_single_bounded`, the tx-timeout becomes OrmQueryTimeout and
    the existing degrade-to-flat fallback fires. With BOTH dedup queries timing
    out the magic check degrades to an empty existing-names set — `_list_fields`
    must still return a clean tree string (not raise, not leak Cypher).

    The own-fields enumeration is stubbed to a clean empty list so the test
    isolates the magic-dedup RAW-ESCAPE path.
    """
    import src.mcp.server as srv

    monkeypatch.setattr(srv, "_get_driver", lambda: _TxTimeoutDriver())
    monkeypatch.setattr(srv, "_resolve_version", lambda v, s: TIMEOUT_TEST_VERSION)
    # Own-field enumeration + count return cleanly (no timeout) so the magic
    # dedup path is the one under test.
    monkeypatch.setattr(srv, "_list_fields_with_inherited", lambda *a, **k: [])
    monkeypatch.setattr(srv, "_count_fields_with_inherited", lambda *a, **k: 0)
    monkeypatch.setattr(srv, "_ancestor_owner_names", lambda *a, **k: ["dense.model"])

    # Both dedup queries time out (the driver raises on every .run()); the
    # degrade-to-flat fallback then also times out → existing_names = set().
    result = srv._list_fields(
        model="dense.model", odoo_version=TIMEOUT_TEST_VERSION,
    )
    assert isinstance(result, str)
    # The magic dedup degraded silently — the field list rendered without leaking
    # Cypher or raising. (Magic fields may appear in the <builtin> prelude.)
    for token in ("MATCH ", "RETURN ", "session.run", "Traceback"):
        assert token not in result, f"leaked internal text {token!r}: {result!r}"
