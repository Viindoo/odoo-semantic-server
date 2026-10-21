# SPDX-License-Identifier: AGPL-3.0-or-later
"""test_class_inspect(method='hierarchy') subclass paging (issue #380 item 5).

Fast lane (no Neo4j): the graph read is replaced by a stub that serves a fixed,
ordered subclass list through the SAME ``subclass_skip`` / ``subclass_cap``
parameters the real Cypher slice uses, so these tests pin the contract the
caller sees - page boundaries, the exact next ``start_index``, the last page,
a cursor past the end - while ``test_test_class_inspect_contract.py``
(neo4j-marked) pins the real Cypher slice.

Business rules under test (ADR-0028 ``start_index``/``limit``, ADR-0023 tree):
  P1  Walking the pages by the printed ``start_index`` visits every subclass
      exactly once, in order (no gap, no repeat).
  P2  A truncated page names the next ``start_index``; the last page names none.
  P3  A cursor past the end says so and points at the real last page.
  P4  ``limit`` is clamped to 1..50 and a negative ``start_index`` acts as 0.
  P5  Paging never changes the TRUE total, and the default call is unchanged.
  P6  summary mode keeps its fixed 6-row preview and ignores paging params.
"""

from __future__ import annotations

import re

import pytest

import src.mcp.server as srv
from src.constants import LIST_PREVIEW_MAX_ITEMS
from src.mcp.tools.test_tools import _TEST_CLASS_SUBCLASS_PAGE_MAX, _test_class_inspect

V = "99.0"
TOTAL = 47  # 20 + 20 + 7 at the default page size


class _Sess:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Driver:
    def session(self, *a, **k):
        return _Sess()


def _children(n: int) -> list[dict]:
    return [{"name": f"TestChild{i:03d}", "module": "mod_a"} for i in range(n)]


@pytest.fixture
def seeded(monkeypatch):
    """Serve ``TOTAL`` ordered subclasses; record the params the tool sent."""
    calls: list[dict] = []
    kids = _children(TOTAL)

    def _bounded(session, cypher, label, **params):
        calls.append(params)
        skip = params["subclass_skip"]
        cap = params["subclass_cap"]
        page = kids[skip:] if cap is None else kids[skip:skip + cap]
        return [{
            "name": "BaseCase", "module": "mod_a", "file_path": "addons/mod_a/tests/c.py",
            "line": 3, "test_type": "transaction", "commit_allowed": False,
            "is_helper": False, "docstring": None, "base_classes": [],
            "setup_summary": [], "all_bases": [], "methods": [],
            "subclassed_by": page, "subclassed_total": len(kids),
        }]

    monkeypatch.setattr(srv, "_resolve_version", lambda v, s: V)
    monkeypatch.setattr(srv, "_data_bounded", _bounded)
    monkeypatch.setattr(srv, "_scope", lambda p: {})
    return calls


def _inspect(**kw) -> str:
    return _test_class_inspect(
        name="BaseCase", odoo_version=V, method=kw.pop("method", "hierarchy"),
        _driver=_Driver(), **kw,
    )


def _names(out: str) -> list[str]:
    return re.findall(r"\[mod_a\] (TestChild\d{3})", out)


def _next_start(out: str) -> int | None:
    m = re.search(r"start_index=(\d+)", out)
    return int(m.group(1)) if m else None


def test_walking_pages_by_printed_start_index_visits_every_subclass_once(seeded):
    """P1 + P2: follow the printed cursor until none is printed."""
    seen: list[str] = []
    start = 0
    for _ in range(10):  # bound the walk; a broken cursor must not hang the suite
        out = _inspect(start_index=start)
        seen += _names(out)
        nxt = _next_start(out)
        if nxt is None:
            break
        assert nxt > start, f"cursor must advance, got {nxt} after {start}:\n{out}"
        start = nxt
    assert seen == [f"TestChild{i:03d}" for i in range(TOTAL)]


def test_first_page_reports_range_total_and_next_cursor(seeded):
    out = _inspect(start_index=0)
    assert f"├─ Subclassed by: {TOTAL} test classes (showing 1-20)" in out
    assert len(_names(out)) == LIST_PREVIEW_MAX_ITEMS
    assert f"... and {TOTAL - 20} more" in out
    assert "method='hierarchy'" in out and "start_index=20)" in out


def test_middle_page_starts_at_the_cursor(seeded):
    out = _inspect(start_index=20)
    assert _names(out)[0] == "TestChild020"
    assert "(showing 21-40)" in out
    assert "start_index=40)" in out


def test_last_page_prints_no_next_cursor_and_no_more_row(seeded):
    out = _inspect(start_index=40)
    assert _names(out) == [f"TestChild{i:03d}" for i in range(40, TOTAL)]
    assert f"(showing 41-{TOTAL})" in out
    assert "... and" not in out
    assert _next_start(out) is None


def test_start_beyond_end_says_so_and_points_at_the_last_page(seeded):
    out = _inspect(start_index=500)
    assert _names(out) == []
    assert f"{TOTAL} test classes" in out
    assert "none at start_index=500" in out
    # last real page for the default page size of 20 starts at 40
    assert "last page" in out and "start_index=40)" in out


def test_start_exactly_at_total_is_past_the_end(seeded):
    out = _inspect(start_index=TOTAL)
    assert _names(out) == []
    assert f"none at start_index={TOTAL}" in out


def test_exact_boundary_page_is_the_last_page_with_no_phantom_next(seeded):
    """The page ends exactly at the total: no 'more' row, no next cursor."""
    out = _inspect(start_index=20, limit=27)
    assert _names(out)[-1] == f"TestChild{TOTAL - 1:03d}"
    assert "... and" not in out and _next_start(out) is None


def test_limit_is_clamped_to_the_maximum_page(seeded):
    out = _inspect(start_index=0, limit=10_000)
    assert seeded[-1]["subclass_cap"] == _TEST_CLASS_SUBCLASS_PAGE_MAX
    assert len(_names(out)) == min(TOTAL, _TEST_CLASS_SUBCLASS_PAGE_MAX)


def test_limit_below_one_is_clamped_to_one_and_cursor_advances_by_one(seeded):
    out = _inspect(start_index=0, limit=0)
    assert seeded[-1]["subclass_cap"] == 1
    assert len(_names(out)) == 1
    assert _next_start(out) == 1


def test_negative_start_index_acts_as_zero(seeded):
    out = _inspect(start_index=-5)
    assert seeded[-1]["subclass_skip"] == 0
    assert _names(out)[0] == "TestChild000"
    assert "(showing 1-20)" in out and _next_start(out) == 20


def test_non_default_limit_is_carried_in_the_next_hint(seeded):
    out = _inspect(start_index=0, limit=5)
    assert "start_index=5, limit=5)" in out


def test_total_is_the_true_count_on_every_page(seeded):
    for start in (0, 20, 40, 500):
        assert f"Subclassed by: {TOTAL} test classes" in _inspect(start_index=start)


def test_default_hierarchy_call_is_page_one(seeded):
    assert _inspect() == _inspect(start_index=0, limit=LIST_PREVIEW_MAX_ITEMS)
    assert seeded[-1]["subclass_skip"] == 0
    assert seeded[-1]["subclass_cap"] == LIST_PREVIEW_MAX_ITEMS


def test_summary_keeps_fixed_preview_and_ignores_paging(seeded):
    out = _inspect(method="summary", start_index=20, limit=50)
    assert seeded[-1]["subclass_skip"] == 0
    assert len(_names(out)) == 6
    assert "(showing" not in out
    assert "method='hierarchy'" in out and "for the first 20" in out


def test_builder_clamps_a_negative_offset_so_cypher_never_slices_from_the_end():
    from src.mcp.test_query import build_test_class_inspect_query

    cypher, params = build_test_class_inspect_query(
        "BaseCase", V, subclass_cap=20, subclass_skip=-3,
    )
    assert params["subclass_skip"] == 0
    assert "children[$subclass_skip..$subclass_skip + $subclass_cap]" in cypher
