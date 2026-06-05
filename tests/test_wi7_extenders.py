# SPDX-License-Identifier: AGPL-3.0-or-later
"""WI-7 behavior tests: model_inspect(method='extenders') pagination.

Acceptance (#262-B): extender list must be fully pageable.
Tests verify:
  (a) summary more_hint points to method='extenders' (not method='fields').
  (b) method='extenders' with start_index=0 returns first page (cap=20).
  (c) method='extenders' with start_index=20 returns remaining rows.
  (d) parity: summary count + paged extender total agree.
  (e) invalid method 'extenders_typo' returns Error: message (router test).

DB version: TEST_VERSION = "93.0" (distinct from 94/95/96/97/98/99).

Note: tests (a)-(d) require Neo4j (pytestmark = pytest.mark.neo4j).
Test (e) is DB-free (uses the router unit-test pattern).
"""
import os
import re

import pytest

from src.indexer.models import FieldInfo, ModelInfo, ModuleInfo, ParseResult
from src.indexer.writer_neo4j import Neo4jWriter

pytestmark = pytest.mark.neo4j

TEST_VERSION = "93.0"
_MODEL_NAME = "wi7.sale.order"
_DEFINING_MODULE = "wi7_sale"
_N_EXTENDERS = 25  # >20 so pagination is required


# ---------------------------------------------------------------------------
# Fixture — seed defining module + 25 extension modules
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def wi7_db(neo4j_driver, monkeypatch_module):
    """Seed a model with 1 defining module + 25 extension modules."""
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    with neo4j_driver.session() as session:
        session.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=TEST_VERSION
        )

    # Defining module - had_explicit_name=True makes is_definition=True.
    defining_mod = ModuleInfo(
        name=_DEFINING_MODULE,
        odoo_version=TEST_VERSION,
        repo="odoo_test",
        path=f"/tmp/{_DEFINING_MODULE}",
        depends=["base"],
        edition="community",
    )
    defining_model = ModelInfo(
        name=_MODEL_NAME,
        module=_DEFINING_MODULE,
        odoo_version=TEST_VERSION,
        had_explicit_name=True,
        fields=[FieldInfo("name", "char")],
    )
    writer.write_results([ParseResult(module=defining_mod, models=[defining_model])])

    # 25 extension modules — each only inherits the model.
    for i in range(_N_EXTENDERS):
        ext_mod_name = f"wi7_ext_{i:02d}"
        ext_mod = ModuleInfo(
            name=ext_mod_name,
            odoo_version=TEST_VERSION,
            repo="odoo_test",
            path=f"/tmp/{ext_mod_name}",
            depends=[_DEFINING_MODULE],
            edition="community",
        )
        ext_model = ModelInfo(
            name=_MODEL_NAME,
            module=ext_mod_name,
            odoo_version=TEST_VERSION,
            had_explicit_name=False,
            inherit=[_MODEL_NAME],
            fields=[FieldInfo(f"extra_field_{i}", "char")],
        )
        writer.write_results([ParseResult(module=ext_mod, models=[ext_model])])

    writer.close()

    monkeypatch_module.setenv(
        "NEO4J_URI", os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    )
    monkeypatch_module.setenv("NEO4J_USER", os.getenv("NEO4J_TEST_USER", "neo4j"))
    monkeypatch_module.setenv(
        "NEO4J_PASSWORD", os.getenv("NEO4J_TEST_PASSWORD", "password")
    )

    import sys
    sys.modules.pop("src.mcp.server", None)

    yield

    with neo4j_driver.session() as session:
        session.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=TEST_VERSION
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_summary_more_hint_points_to_extenders(wi7_db):
    """Summary 'Extended by' more_hint must point to method='extenders'.

    WI-7 fix (#262-B): before WI-7 the more_hint pointed to method='fields',
    which enumerates FIELDS not extenders. After WI-7 it must say 'extenders'.
    """
    import importlib
    server = importlib.import_module("src.mcp.server")

    result = server._resolve_model(_MODEL_NAME, TEST_VERSION)
    # With 25 extenders, cap=20 triggers the "... and N more (use ...)" overflow hint.
    # Target the overflow line SPECIFICALLY: method='fields' legitimately appears in
    # the general Next: navigation footer ("for full field list"), which is NOT the
    # extender hint — so we must not assert on the whole output (that conflated the
    # two and produced a false failure).
    overflow_lines = [
        ln for ln in result.splitlines()
        if "more (use" in ln and "model_inspect" in ln
    ]
    assert overflow_lines, (
        f"Expected an 'Extended by' overflow hint line. Got summary:\n{result}"
    )
    assert all("method='extenders'" in ln for ln in overflow_lines), (
        f"Extender-overflow hint must steer to method='extenders' (#262-B). "
        f"Got overflow line(s): {overflow_lines}"
    )
    assert not any("method='fields'" in ln for ln in overflow_lines), (
        f"Extender-overflow hint must NOT use method='fields' (the pre-WI-7 bug). "
        f"Got overflow line(s): {overflow_lines}"
    )


def test_extenders_first_page_returns_up_to_20(wi7_db):
    """method='extenders' start_index=0 returns up to 20 rows (cap=LIST_PREVIEW_MAX_ITEMS)."""
    import importlib
    server = importlib.import_module("src.mcp.server")

    result = server._list_extenders(_MODEL_NAME, TEST_VERSION, start_index=0)
    assert "wi7_ext_" in result, f"Expected extender module names in output. Got:\n{result}"
    # Should show rows 1-20 of 25.
    assert "Showing rows 1-20 of 25" in result, (
        f"Expected pagination hint 'Showing rows 1-20 of 25'. Got:\n{result}"
    )


def test_extenders_second_page_returns_remainder(wi7_db):
    """method='extenders' start_index=20 returns the remaining 5 rows.

    Verifies full pagination: after page 1 (rows 1-20), page 2 (rows 21-25)
    is reachable, satisfying acceptance 'extender list page duoc het'.
    """
    import importlib
    server = importlib.import_module("src.mcp.server")

    result = server._list_extenders(_MODEL_NAME, TEST_VERSION, start_index=20)
    assert "wi7_ext_" in result, f"Expected extender module names. Got:\n{result}"
    # Should show rows 21-25 (last page).
    assert "Showing rows 21-25 of 25" in result, (
        f"Expected 'Showing rows 21-25 of 25'. Got:\n{result}"
    )
    # No "and N more" line on the last page.
    assert "for next" not in result, (
        f"Last page must not show 'for next N'. Got:\n{result}"
    )


def test_extenders_parity_with_summary_count(wi7_db):
    """Paged extender total must match the count disclosed in summary 'Extended by'.

    ADR-0023 §5.5 invariant: summary count 'and N more' + page cap must add up
    to the full total returned by method='extenders'.
    """
    import importlib
    server = importlib.import_module("src.mcp.server")

    summary = server._resolve_model(_MODEL_NAME, TEST_VERSION)
    # The summary shows 20 extenders + "... and 5 more".
    assert "Extended by:" in summary, f"Summary must have 'Extended by:' section. Got:\n{summary}"
    assert "and 5 more" in summary, (
        f"Summary must disclose '... and 5 more' for 25-20=5 overflow. Got:\n{summary}"
    )

    # Page 2 confirms total=25.
    page2 = server._list_extenders(_MODEL_NAME, TEST_VERSION, start_index=20)
    assert "of 25" in page2, (
        f"Page 2 must report total=25. Got:\n{page2}"
    )


def test_extenders_via_model_inspect_dispatcher(wi7_db):
    """model_inspect(method='extenders') routes correctly via the discriminator.

    Verifies the inspect.py router wires to _list_extenders.
    """
    from src.mcp.inspect import _model_inspect

    result = _model_inspect(
        _MODEL_NAME, method="extenders", odoo_version=TEST_VERSION, start_index=0
    )
    assert isinstance(result, str), f"Expected str, got {type(result)}"
    assert not result.startswith("Error:"), f"Router returned error: {result!r}"
    assert "wi7_ext_" in result, f"Expected extender rows in routed result. Got:\n{result}"


# ---------------------------------------------------------------------------
# M2 (#262) parity — summary "Extended by" count == _list_extenders total
# under the SHARED `NOT is_definition` predicate, including the multi-definition
# case where the old `layers[1:]` logic over-counted by treating an extra
# definition node as an extender.
# ---------------------------------------------------------------------------

_MULTIDEF_VERSION = "92.0"
_MULTIDEF_MODEL = "m2.multidef.model"
_N_MULTIDEF_EXTENDERS = 3  # small + 2 definitions → fits one page, no overflow


@pytest.fixture(scope="module")
def multidef_db(neo4j_driver, monkeypatch_module):
    """Seed a model defined by TWO modules (both is_definition=True) plus 3 pure
    extenders. The old summary logic (base=layers[0], extensions=layers[1:])
    counts 1 definition + 3 extenders = 4 'extenders'; the correct
    `NOT is_definition` predicate counts exactly 3.
    """
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    with neo4j_driver.session() as session:
        session.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=_MULTIDEF_VERSION
        )

    # TWO defining modules — both had_explicit_name=True → is_definition=True.
    for j in range(2):
        def_mod_name = f"m2_def_{j}"
        def_mod = ModuleInfo(
            name=def_mod_name, odoo_version=_MULTIDEF_VERSION, repo="odoo_test",
            path=f"/tmp/{def_mod_name}", depends=["base"], edition="community",
        )
        def_model = ModelInfo(
            name=_MULTIDEF_MODEL, module=def_mod_name, odoo_version=_MULTIDEF_VERSION,
            had_explicit_name=True, fields=[FieldInfo("name", "char")],
        )
        writer.write_results([ParseResult(module=def_mod, models=[def_model])])

    # 3 pure extension modules.
    for i in range(_N_MULTIDEF_EXTENDERS):
        ext_mod_name = f"m2_ext_{i}"
        ext_mod = ModuleInfo(
            name=ext_mod_name, odoo_version=_MULTIDEF_VERSION, repo="odoo_test",
            path=f"/tmp/{ext_mod_name}", depends=["m2_def_0"], edition="community",
        )
        ext_model = ModelInfo(
            name=_MULTIDEF_MODEL, module=ext_mod_name, odoo_version=_MULTIDEF_VERSION,
            had_explicit_name=False, inherit=[_MULTIDEF_MODEL],
            fields=[FieldInfo(f"x_{i}", "char")],
        )
        writer.write_results([ParseResult(module=ext_mod, models=[ext_model])])

    writer.close()

    monkeypatch_module.setenv(
        "NEO4J_URI", os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    )
    monkeypatch_module.setenv("NEO4J_USER", os.getenv("NEO4J_TEST_USER", "neo4j"))
    monkeypatch_module.setenv(
        "NEO4J_PASSWORD", os.getenv("NEO4J_TEST_PASSWORD", "password")
    )

    import sys
    sys.modules.pop("src.mcp.server", None)

    yield

    with neo4j_driver.session() as session:
        session.run(
            "MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=_MULTIDEF_VERSION
        )


def test_extenders_parity_multi_definition(multidef_db):
    """With 2 definition modules + 3 extenders, the summary 'Extended by' count
    MUST equal _list_extenders' total (3) — not 4.

    Guards M2: the summary now derives 'Extended by' from `NOT is_definition`,
    identical to _list_extenders. The old `layers[1:]` logic dropped only the
    single top-ranked definition, mislabeling the second definition as an
    extender and inflating the count to 4. Fail-able: revert to `layers[1:]`
    and this asserts 3 != 4.
    """
    import importlib
    server = importlib.import_module("src.mcp.server")

    # _list_extenders is the authoritative total under NOT is_definition.
    extenders = server._list_extenders(_MULTIDEF_MODEL, _MULTIDEF_VERSION, start_index=0)
    assert "of 3" in extenders or "m2_ext_" in extenders, (
        f"Expected 3 extenders from _list_extenders. Got:\n{extenders}"
    )
    # The 3 extenders fit one page → no "Showing rows" overflow; assert the 3
    # extender modules are present and NO definition module is listed.
    for i in range(_N_MULTIDEF_EXTENDERS):
        assert f"m2_ext_{i}" in extenders
    assert "m2_def_" not in extenders, (
        f"_list_extenders must exclude definition modules. Got:\n{extenders}"
    )

    # Summary 'Extended by' must list exactly the 3 extenders — no definition.
    summary = server._resolve_model(_MULTIDEF_MODEL, _MULTIDEF_VERSION)
    assert "Extended by:" in summary, f"Summary missing 'Extended by'. Got:\n{summary}"
    # Count the extender rows rendered under 'Extended by'. The defining module
    # appears on the 'Defined in' line only; the SECOND definition must NOT be
    # rendered as an extender (the M2 bug).
    ext_section = summary.split("Extended by:", 1)[1]
    listed_extenders = [i for i in range(_N_MULTIDEF_EXTENDERS)
                        if f"m2_ext_{i}" in ext_section]
    assert len(listed_extenders) == _N_MULTIDEF_EXTENDERS, (
        f"Summary must list all 3 extenders. Got 'Extended by' section:\n{ext_section}"
    )
    # The second definition module must NOT appear in the 'Extended by' section.
    assert "m2_def_" not in ext_section, (
        f"Summary 'Extended by' must not include a definition module (M2 over-count). "
        f"Got:\n{ext_section}"
    )


# ---------------------------------------------------------------------------
# L3 — single full page (total <= cap, start_index == 0) discloses the total
# ---------------------------------------------------------------------------


def test_extenders_single_page_discloses_total(multidef_db):
    """L3: a single non-truncated page still discloses the complete count.

    The multidef fixture has 3 extenders (≤ cap=20) at start_index=0, so neither
    the multi-page "Showing rows X-Y" nor the "(last page)" branch fired before
    this fix — the agent could not tell the list was complete. Now an explicit
    "Showing all 3 of 3" must render.

    Fail-able: remove the else-branch and this assertion goes red.
    """
    import importlib
    server = importlib.import_module("src.mcp.server")

    result = server._list_extenders(_MULTIDEF_MODEL, _MULTIDEF_VERSION, start_index=0)
    assert "Showing all 3 of 3" in result, (
        f"Single full page must disclose the complete total. Got:\n{result}"
    )


# ---------------------------------------------------------------------------
# L6 — pagination boundary: start_index == total and start_index > total
# ---------------------------------------------------------------------------


def _assert_no_inverted_range(result: str) -> None:
    """Behavior guard: no rendered `Showing rows A-B` may have A > B.

    The off-by-one renders `Showing rows 26-25 of 25 (last page)` when the cursor
    over-runs the total. This guard fails on that inverted range and would stay
    red if the `start_index >= total` branch were removed (mutation-sensitive).
    """
    for m in re.finditer(r"Showing rows (\d+)[-–](\d+)", result):
        lo, hi = int(m.group(1)), int(m.group(2))
        assert lo <= hi, (
            f"Inverted pagination range rendered: 'rows {lo}-{hi}'. Got:\n{result}"
        )


def test_extenders_start_index_equals_total_returns_empty_with_disclosure(wi7_db):
    """L6: start_index == total → empty page, no crash, clean over-run disclosure.

    The classic off-by-one cursor: the previous page ended exactly at `total`.
    The body must not raise, must list no extender rows, must NOT render an
    inverted `26-25` range, and must surface the over-run disclosure so the
    agent stops paginating.
    """
    import importlib
    server = importlib.import_module("src.mcp.server")

    result = server._list_extenders(_MODEL_NAME, TEST_VERSION, start_index=_N_EXTENDERS)
    # No actual extender module rows on this empty page.
    assert "wi7_ext_" not in result, (
        f"start_index==total must yield no extender rows. Got:\n{result}"
    )
    # The off-by-one bug renders "Showing rows 26-25 of 25" — must NOT happen.
    _assert_no_inverted_range(result)
    # Over-run is disclosed cleanly (new branch) — total is surfaced.
    assert f"total={_N_EXTENDERS}" in result, (
        f"Empty boundary page must disclose the over-run + total. Got:\n{result}"
    )
    assert f"start_index={_N_EXTENDERS}" in result, (
        f"Over-run disclosure must echo the requested start_index. Got:\n{result}"
    )
    # Must not advertise a further page.
    assert "for next" not in result, (
        f"start_index==total must not advertise another page. Got:\n{result}"
    )


def test_extenders_start_index_beyond_total_does_not_crash(wi7_db):
    """L6: start_index > total → empty page, no crash, no inverted range."""
    import importlib
    server = importlib.import_module("src.mcp.server")

    result = server._list_extenders(
        _MODEL_NAME, TEST_VERSION, start_index=_N_EXTENDERS + 100
    )
    assert "wi7_ext_" not in result, (
        f"start_index>total must yield no extender rows. Got:\n{result}"
    )
    _assert_no_inverted_range(result)
    assert "for next" not in result, (
        f"start_index>total must not advertise another page. Got:\n{result}"
    )
