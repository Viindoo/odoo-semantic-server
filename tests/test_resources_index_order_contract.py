# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_resources_index_order_contract.py
"""Deterministic-ORDER-BY contract for resources_index._fetch_top_models.

CLAUDE.md (Neo4j 5.x gotcha): "ORDER BY phải có deterministic tiebreak". The
discovery-index ranking orders models by `dep_count DESC, m.name ASC`; when the
same model name is defined by several modules at the same dep_count, that pair
collides and the row order becomes implementation-defined unless a further
tiebreak is appended.

`_fetch_top_models` was hardened to add `mod.name ASC`. These DB-free tests
capture the actual Cypher the function issues (via a mocked session) and assert
the tiebreak is present in BOTH the primary (is_definition) query and the
no-is_definition fallback query. They fail if the tiebreak is ever dropped.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from src.mcp.resources_index import _fetch_top_models


def _capturing_session(returns_per_call: list[list]):
    """Return a mock Neo4j session whose .run().data() yields successive results.

    `returns_per_call[i]` is the `.data()` payload for the i-th `session.run`.
    Captured query strings are recorded on `session.captured_queries`.

    #287: `_fetch_top_models` now routes through `_data_bounded`, which wraps the
    Cypher in a `neo4j.Query(text, timeout=...)` before calling `session.run`.
    The captured first positional arg is therefore a `neo4j.Query` whose raw text
    lives on `.text` — normalise to the string so the ORDER BY contract assertions
    (unchanged) keep inspecting the actual Cypher.
    """
    captured: list[str] = []
    call = {"i": 0}

    def _run(query, **kwargs):
        captured.append(getattr(query, "text", query))
        idx = call["i"]
        call["i"] += 1
        result = MagicMock()
        payload = returns_per_call[idx] if idx < len(returns_per_call) else []
        result.data.return_value = payload
        return result

    session = MagicMock()
    session.run.side_effect = _run
    session.captured_queries = captured
    return session


def test_primary_query_has_module_name_tiebreak():
    """The is_definition primary query must order with a `mod.name` tiebreak."""
    # Non-empty first result → fallback is never reached.
    session = _capturing_session([[{"model_name": "sale.order", "dep_count": 3}]])

    _fetch_top_models(session, "17.0", scope_params=None)

    primary = session.captured_queries[0]
    order_by = primary[primary.index("ORDER BY"):]
    assert "dep_count DESC" in order_by, order_by
    assert "m.name ASC" in order_by, order_by
    assert "mod.name" in order_by, (
        "Primary query ORDER BY must include a deterministic mod.name tiebreak; "
        f"got: {order_by!r}"
    )


def test_fallback_query_has_module_name_tiebreak():
    """The no-is_definition fallback query must also carry the tiebreak."""
    # First result empty → triggers the fallback branch (second run).
    session = _capturing_session([[], [{"model_name": "res.partner", "dep_count": 0}]])

    _fetch_top_models(session, "17.0", scope_params=None)

    assert len(session.captured_queries) == 2, (
        "Empty primary result must trigger exactly one fallback query"
    )
    fallback = session.captured_queries[1]
    order_by = fallback[fallback.index("ORDER BY"):]
    assert "mod.name" in order_by, (
        "Fallback query ORDER BY must include a deterministic mod.name tiebreak; "
        f"got: {order_by!r}"
    )
