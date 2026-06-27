# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_find_deprecated_usage_version_gate.py
"""graph LOW-1 / integration MED-1 — the find_deprecated_usage decorator
version-gate must never raise on a non-numeric version.

`normalize_version_arg` (src/mcp/session.py) does NOT validate an explicit
version arg as `\\d+\\.\\d+` — it only collapses the 6 sentinels. So an LLM can
pass `find_deprecated_usage(odoo_version="saas-17.4")`, which previously reached
a raw `float("saas-17.4")` → uncaught ValueError → FastMCP protocol error with
internals leaked (ADR-0023 clean-text break). The gate now fails closed: on a
non-numeric version it skips the decorator leg (returns an empty allow-list); the
call-based leg still runs.

These are pure no-DB unit tests on the extracted gate helper — no Neo4j needed,
so they run in the `not neo4j and not pg` lane.
"""
import pytest

from src.mcp.tools.spec import _removed_decorators_for_version


def test_numeric_version_still_gates_correctly():
    """The numeric gate is unchanged: api.one removed in 10.0, api.multi in 13.0."""
    # >= 13.0: both api.one and api.multi removed (sorted).
    assert _removed_decorators_for_version("17.0") == ["api.multi", "api.one"]
    # 12.0: only api.one removed; api.multi still a valid framework decorator.
    assert _removed_decorators_for_version("12.0") == ["api.one"]
    # 9.0: nothing removed yet.
    assert _removed_decorators_for_version("9.0") == []
    # Exactly at the boundary: api.multi removed AS OF 13.0 (>=).
    assert _removed_decorators_for_version("13.0") == ["api.multi", "api.one"]


@pytest.mark.parametrize(
    "bad_version",
    ["saas-17.4", "saas~17.2", "17.x", "v17", "", "auto", "latest", "default"],
)
def test_non_numeric_version_does_not_raise(bad_version):
    """A non-numeric explicit version must fail closed to an empty allow-list,
    NOT raise ValueError (which would 500 find_deprecated_usage)."""
    assert _removed_decorators_for_version(bad_version) == []


def test_none_version_does_not_raise():
    """A None version (defensive) must also fail closed, not raise TypeError."""
    assert _removed_decorators_for_version(None) == []  # type: ignore[arg-type]
