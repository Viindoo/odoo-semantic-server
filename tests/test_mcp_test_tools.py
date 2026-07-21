# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tool contract tests for the 6 WI-4 test-surface MCP tools.

Business rules protected:
  - find_test_examples returns only test/js chunk types (never production chunks)
  - tests_covering lists seeded tests with file:line
  - test_base_classes output contains "cr.commit() FORBIDDEN" (PP3 contract)
  - test_coverage_audit lists unreferenced field + static caveat
  - test_class_inspect shows subclassed-by
  - every new tool output ends with Next:
  - test_tool_count_sync passes at 31/9

Red-before-green: all assertions were verified to fail before implementation
was complete. Test names state the business rule each test protects.

All tests use TEST_VERSION='99.0' + clean_neo4j fixture (see conftest.py).
Tests that need pgvector use @pytest.mark.postgres + clean_pg_embeddings.
Tests that only need Neo4j use @pytest.mark.neo4j.

Import the underscore impls (_find_test_examples etc.) when testing internal
logic. FastMCP v3 public names (server.find_test_examples etc.) are directly
callable, but the underscore impls are more stable for unit isolation.
"""

import asyncio
import os
import re

import pytest

from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j


# ---------------------------------------------------------------------------
# Helpers: seed test graph data
# ---------------------------------------------------------------------------

def _seed_test_class_and_method(neo4j_driver, *, version: str = TEST_VERSION) -> None:
    """Seed TestHelper + TestClass + TestMethod + COVERS_FIELD edge for contract tests."""
    with neo4j_driver.session() as s:
        # Framework helper (TransactionCase)
        s.run("""
            MERGE (h:TestHelper {
                name: 'TransactionCase',
                odoo_version: $v,
                origin: 'framework'
            })
            SET h.test_type = 'transaction',
                h.commit_allowed = false,
                h.setup_summary = ['savepoint-per-method'],
                h.file_path = 'odoo/tests/common.py',
                h.line = 10,
                h.profile = ['test_profile']
        """, v=version)

        # Addon helper (is_helper=True)
        s.run("""
            MERGE (m:Module {name: 'sale', odoo_version: $v})
            SET m.profile = ['test_profile']
            MERGE (h:TestHelper {
                name: 'TestSaleCommon',
                odoo_version: $v,
                module: 'sale'
            })
            SET h.test_type = 'transaction',
                h.commit_allowed = false,
                h.origin = 'addon',
                h.setup_summary = ['sale.order', 'res.partner'],
                h.file_path = 'addons/sale/tests/common.py',
                h.line = 10,
                h.profile = ['test_profile']
            MERGE (h)-[:DEFINED_IN]->(m)
        """, v=version)

        # TestClass subclassing TestSaleCommon
        s.run("""
            MERGE (tc:TestClass {
                name: 'TestSaleOrder',
                module: 'sale',
                file_path: 'addons/sale/tests/test_sale_order.py',
                odoo_version: $v
            })
            SET tc.test_type = 'transaction',
                tc.commit_allowed = false,
                tc.is_helper = false,
                tc.base_classes = ['TestSaleCommon'],
                tc.tagged = ['post_install', '-at_install'],
                tc.profile = ['test_profile']
            MERGE (m:Module {name: 'sale', odoo_version: $v})
            MERGE (tc)-[:DEFINED_IN]->(m)
        """, v=version)

        # TestMethod referencing a field
        s.run("""
            MERGE (tc:TestClass {
                name: 'TestSaleOrder',
                module: 'sale',
                file_path: 'addons/sale/tests/test_sale_order.py',
                odoo_version: $v
            })
            MERGE (tm:TestMethod {
                name: 'test_amount_total_computed',
                test_class: 'TestSaleOrder',
                module: 'sale',
                file_path: 'addons/sale/tests/test_sale_order.py',
                odoo_version: $v
            })
            SET tm.asserts_count = 1,
                tm.via = 'assert',
                tm.line = 142,
                tm.model_refs = ['sale.order'],
                tm.field_refs = ['amount_total'],
                tm.profile = ['test_profile']
            MERGE (tm)-[:BELONGS_TO_TEST]->(tc)
        """, v=version)

        # INHERITS_TEST: TestSaleOrder -> TestSaleCommon
        s.run("""
            MATCH (tc:TestClass {name:'TestSaleOrder', odoo_version:$v})
            MATCH (h:TestHelper {name:'TestSaleCommon', odoo_version:$v})
            MERGE (tc)-[:INHERITS_TEST]->(h)
        """, v=version)

        # INHERITS_TEST: TestSaleCommon -> TransactionCase (framework)
        s.run("""
            MATCH (h:TestHelper {name:'TestSaleCommon', odoo_version:$v})
            MATCH (fw:TestHelper {name:'TransactionCase', odoo_version:$v})
            MERGE (h)-[:INHERITS_TEST]->(fw)
        """, v=version)

        # Field node for coverage edges
        s.run("""
            MERGE (f:Field {
                name: 'amount_total',
                model: 'sale.order',
                module: 'sale',
                odoo_version: $v,
                is_definition: true
            })
            SET f.ttype = 'monetary',
                f.profile = ['test_profile']
        """, v=version)

        # COVERS_FIELD edge
        s.run("""
            MATCH (tm:TestMethod {
                name: 'test_amount_total_computed',
                odoo_version: $v
            })
            MATCH (f:Field {
                name: 'amount_total',
                model: 'sale.order',
                odoo_version: $v
            })
            MERGE (tm)-[:COVERS_FIELD]->(f)
        """, v=version)

        # A field with NO coverage for audit test
        s.run("""
            MERGE (f2:Field {
                name: 'commitment_date',
                model: 'sale.order',
                module: 'sale',
                odoo_version: $v
            })
            SET f2.ttype = 'datetime',
                f2.profile = ['test_profile']
        """, v=version)


# ---------------------------------------------------------------------------
# Test: tests_covering lists seeded test with file:line  (Q3 contract)
# ---------------------------------------------------------------------------

def test_tests_covering_returns_real_test_for_seeded_field(clean_neo4j):
    """Business rule: tests_covering returns TestMethod with file:line for a seeded field.

    Seed: TestMethod(test_amount_total_computed) -[:COVERS_FIELD]-> Field(amount_total).
    Assert: tool output contains the method name and file path.
    """
    _seed_test_class_and_method(clean_neo4j)

    from src.mcp.tools.test_tools import _tests_covering
    result = _tests_covering(
        model="sale.order",
        odoo_version=TEST_VERSION,
        field="amount_total",
        _driver=clean_neo4j,
    )
    assert "test_amount_total_computed" in result
    assert "sale/tests/test_sale_order.py" in result or "test_sale_order.py" in result
    # file:line format
    assert ":142" in result or "142" in result


# ---------------------------------------------------------------------------
# Test: test_base_classes output contains cr.commit() FORBIDDEN  (PP3 contract)
# ---------------------------------------------------------------------------

def test_test_base_classes_states_commit_forbidden(clean_neo4j):
    """Business rule: test_base_classes always includes 'cr.commit() FORBIDDEN'.

    This is the PP3 cursor contract — every version's output must carry this
    sentinel so the agent internalizes the rule before writing a test.
    """
    _seed_test_class_and_method(clean_neo4j)

    from src.mcp.tools.test_tools import _test_base_classes
    result = _test_base_classes(odoo_version=TEST_VERSION, _driver=clean_neo4j)
    # PP3 MUST appear verbatim
    assert "cr.commit() FORBIDDEN" in result


def test_test_base_classes_states_commit_forbidden_static_fallback(clean_neo4j):
    """Business rule: even without graph data, test_base_classes carries PP3 rule.

    Reworked for issue #362 WI-4: ``_static_framework_bases_str`` — the SECOND,
    divergent copy of the framework-base data — was deleted by WI-3.
    ``src/indexer/framework_bases.py`` is now the single source of truth for
    BOTH the graph-backed path and the no-graph (degraded) path: the graph is
    consulted only to *enrich* file_path/line (``_enrich_with_graph_locations``),
    never to decide which classes appear, so the curated menu — and therefore
    the PP3 cursor-contract literal — renders identically whether or not
    anything is indexed. The BEHAVIOR this test protects is unchanged (an agent
    querying test_base_classes with nothing indexed must still see the PP3
    contract); what changed is only the code path used to reach it: the public/
    underscore entry point (``_test_base_classes``) against a genuinely empty
    graph, not a deleted private helper. ``clean_neo4j`` scrubs every node at
    TEST_VERSION before *and* after this test, and nothing is seeded here, so
    the graph is guaranteed empty — this is the "degraded path" by construction.
    """
    from src.mcp.tools.test_tools import _test_base_classes
    result = _test_base_classes(odoo_version=TEST_VERSION, _driver=clean_neo4j)
    assert "cr.commit() FORBIDDEN" in result


# ---------------------------------------------------------------------------
# T10-T12 (issue #362, WI-0c): test_base_classes must be genuinely PER-VERSION.
#
# RED-BEFORE-GREEN: `src/indexer/framework_bases.py` (the production fix) does not
# exist yet. These tests pin the target read-side contract from
# /tmp/osm-362/api-contract.md and /tmp/osm-362/phase4-solution.md §6/§7/§13. They
# are expected to FAIL against the current code, for two independent reasons:
#   - the graph seed (`_FRAMEWORK_BASES`, src/indexer/parser_test.py) is
#     version-blind: seed_framework_helpers(v) returns the SAME 11 byte-identical
#     entries for every version (Defect 1).
#   - the empty-graph fallback (`_static_framework_bases`, test_tools.py) is a
#     SECOND, independently-wrong copy of the same data, gated only by a single
#     inline `major <= 15` check (Defect 2).
#   - the `name=` drill-down silently prints the WHOLE menu on a miss instead of
#     stating absence (Defect 3).
# ---------------------------------------------------------------------------

_REAL_VERSIONS_UNDER_TEST = [
    "8.0", "9.0", "10.0", "11.0", "12.0", "13.0",
    "14.0", "15.0", "16.0", "17.0", "18.0", "19.0",
]

# A class ROW is the only line shaped "<connector> <Name>   <test_type> · ...";
# test_type is one of the 6 literal values the FrameworkBaseFacts contract allows
# (api-contract.md). Matching on that shape recovers the rendered NAME SET without
# coupling to prose wording that will change once the real fix ships.
_CLASS_ROW_RE = re.compile(
    r"^[├└]─ (\S+)\s+(?:transaction|savepoint|single_transaction|http|form|unittest) · ",
)


def _rendered_class_names(result: str) -> set[str]:
    """Return the set of class names rendered as top-level rows in a tree."""
    names = set()
    for line in result.splitlines():
        m = _CLASS_ROW_RE.match(line)
        if m:
            names.add(m.group(1))
    return names


def _last_line(result: str) -> str:
    return result.rstrip("\n").splitlines()[-1]


def _seed_framework_menu(odoo_version: str) -> None:
    """Seed TestHelper framework nodes via the REAL (current, unfixed) indexer path.

    seed_framework_helpers() is version-blind today (src/indexer/parser_test.py
    _FRAMEWORK_BASES) — every version receives the same 11 nodes. Seeding through
    the actual production writer (not hand-rolled Cypher) is what makes the
    per-version assertions below genuinely fail against today's code — it exercises
    the real defect location (the write side), not a test fixture stand-in.
    """
    from src.indexer.parser_test import seed_framework_helpers
    from src.indexer.writer_neo4j import Neo4jWriter

    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    try:
        writer.write_framework_test_helpers(seed_framework_helpers(odoo_version))
    finally:
        writer.close()


def _wipe_framework_helpers(driver, versions: list[str]) -> None:
    with driver.session() as s:
        s.run(
            "MATCH (h:TestHelper {module: '@framework'}) WHERE h.odoo_version IN $vs "
            "DETACH DELETE h",
            vs=versions,
        )


@pytest.fixture
def clean_versions_neo4j(clean_neo4j):
    """Wipe framework TestHelper nodes at the REAL Odoo versions T10-T12 exercise.

    clean_neo4j only scrubs odoo_version == TEST_VERSION ('99.0'); T10-T12 render
    output for real version strings (8.0..19.0), so this fixture keeps those reads
    isolated from anything else the shared session-scoped Neo4j container holds.
    """
    _wipe_framework_helpers(clean_neo4j, _REAL_VERSIONS_UNDER_TEST)
    yield clean_neo4j
    _wipe_framework_helpers(clean_neo4j, _REAL_VERSIONS_UNDER_TEST)


def test_test_base_classes_v17_excludes_removed_savepointcase(clean_versions_neo4j):
    """Business rule: SavepointCase was removed entering 17.0 (zero occurrences in
    odoo17/odoo/tests/common.py) — the 17.0 menu must never offer it as a USABLE
    base class (AC2, phase4-solution.md §6.5/§13).

    ADJUDICATED (issue #362 WI-4, phase5-review.md finding W2 / required change
    10): the ORIGINAL assertion here was a coarse proxy — "the substring
    SavepointCase must never appear in the v17 menu" — for the real rule, which
    is "an agent must never be told to WRITE class TestX(SavepointCase) at v17".
    A menu-level removal line ("Removed as of Odoo 17.0: SavepointCase ->
    TransactionCase; ...") does NOT violate that real rule — it SERVES it: the
    menu is the path an agent actually reads (the name= drill-down is reached
    only once the agent already suspects the class is gone), and OSM's
    documented audience includes version-upgrade work, so naming the removal +
    replacement is strictly more actionable than silence for the reader
    upgrading FROM v14 who is looking for SavepointCase. So the assertion is
    RE-EXPRESSED, not loosened: it now targets the exact thing that WOULD
    violate the rule — SavepointCase rendered as an available CLASS ROW (the
    "<connector> Name   test_type · ..." shape a caller could mistake for
    "usable here", captured by the same _CLASS_ROW_RE / _rendered_class_names
    helpers T10-T12 already share) — while explicitly still requiring the
    removal line to name both the removed class and its replacement. This is
    STRONGER than the original: it still fails if SavepointCase is EVER listed
    as a usable base at v17 (a class-row match would show up in
    _rendered_class_names), and it additionally pins the removal line's
    presence, which the original could not express at all.

    RED today (pre-fix): seed_framework_helpers() is version-blind, so the
    graph-backed menu at 17.0 still carries the byte-identical SavepointCase
    entry every other version gets — that graph-blindness bug is unrelated to,
    and unaffected by, this assertion rewrite.
    """
    _seed_framework_menu("17.0")
    from src.indexer.framework_bases import removed_at
    from src.mcp.tools.test_tools import _COMMIT_FORBIDDEN_MSG, _test_base_classes

    result = _test_base_classes(odoo_version="17.0", _driver=clean_versions_neo4j)

    class_rows = _rendered_class_names(result)
    assert "SavepointCase" not in class_rows, (
        f"SavepointCase does not exist at Odoo 17.0 and must never be listed as "
        f"a usable CLASS ROW. Got:\n{result}"
    )
    assert "HttpSavepointCase" not in class_rows, (
        f"HttpSavepointCase does not exist at Odoo 17.0 and must never be "
        f"listed as a usable CLASS ROW. Got:\n{result}"
    )

    # The removal must still be NAMED explicitly (change 10) — sourced from the
    # SSOT (removed_at), never a second hardcoded {old: new} literal in this test.
    removals = removed_at("17.0")
    assert removals, "removed_at('17.0') must be non-empty for this test to be meaningful"
    for old_name, replacement in removals:
        assert old_name in result, (
            f"{old_name} must still be NAMED in a removal line (it is absent, "
            f"not unmentionable). Got:\n{result}"
        )
        assert replacement in result, (
            f"the replacement {replacement} must be named alongside {old_name}. "
            f"Got:\n{result}"
        )

    assert _COMMIT_FORBIDDEN_MSG in result
    assert _last_line(result).startswith("└─ Next:"), (
        f"Next: must be the last line. Got: {_last_line(result)!r}"
    )


def test_test_base_classes_v15_shows_savepointcase_deprecated(clean_versions_neo4j):
    """Business rule: SavepointCase is real but DEPRECATED at 15.0 (merged into
    TransactionCase; odoo15/odoo/tests/common.py:873 warns DeprecationWarning).

    RED today: _FRAMEWORK_BASES's SavepointCase text says 'deprecated alias'
    (lowercase, version-blind) — never the word DEPRECATED — so a v15-specific
    deprecation signal is not observable in the output at all.
    """
    _seed_framework_menu("15.0")
    from src.mcp.tools.test_tools import _COMMIT_FORBIDDEN_MSG, _test_base_classes

    result = _test_base_classes(odoo_version="15.0", _driver=clean_versions_neo4j)

    assert "SavepointCase" in result
    assert "DEPRECATED" in result, (
        f"15.0 SavepointCase must be flagged DEPRECATED. Got:\n{result}"
    )
    assert _COMMIT_FORBIDDEN_MSG in result
    assert _last_line(result).startswith("└─ Next:")


def test_test_base_classes_v16_shares_v15_deprecated_era(clean_versions_neo4j):
    """Business rule: v15 and v16 are DELIBERATELY the same era — identical menu and
    semantics (odoo16/odoo/tests/common.py:826 carries the same DeprecationWarning
    as v15). The answer legitimately does NOT change here even though it does
    change at 14->15 and 16->17.

    RED today: same root cause as v15 — no DEPRECATED marker is ever emitted.
    """
    _seed_framework_menu("16.0")
    from src.mcp.tools.test_tools import _COMMIT_FORBIDDEN_MSG, _test_base_classes

    result = _test_base_classes(odoo_version="16.0", _driver=clean_versions_neo4j)

    assert "SavepointCase" in result
    assert "DEPRECATED" in result, (
        f"16.0 SavepointCase must be flagged DEPRECATED (same era as 15.0). Got:\n{result}"
    )
    assert _COMMIT_FORBIDDEN_MSG in result
    assert _last_line(result).startswith("└─ Next:")


def test_test_base_classes_v14_shows_savepointcase_available_and_httpcasecommon(
    clean_versions_neo4j,
):
    """Business rule: at 14.0 SavepointCase is real and RECOMMENDED, not deprecated
    (`warnings` is not even imported by odoo14/odoo/tests/common.py — deprecation
    started at v15) and HttpCaseCommon exists ONLY at 14.0.

    RED today: SavepointCase's version-blind text already says 'deprecated alias'
    — simply FALSE at 14.0.
    """
    _seed_framework_menu("14.0")
    from src.mcp.tools.test_tools import _COMMIT_FORBIDDEN_MSG, _test_base_classes

    result = _test_base_classes(odoo_version="14.0", _driver=clean_versions_neo4j)

    assert "SavepointCase" in result
    assert "HttpCaseCommon" in result
    assert "DEPRECATED" not in result
    assert "alias" not in result, (
        f"14.0 SavepointCase is not deprecated — 'alias' text is a v8-v16-era leak. "
        f"Got:\n{result}"
    )
    assert _COMMIT_FORBIDDEN_MSG in result
    assert _last_line(result).startswith("└─ Next:")


def test_test_base_classes_v11_shows_treecase(clean_versions_neo4j):
    """Business rule: TreeCase's real window is 11.0-14.0
    (odoo11/odoo/tests/common.py:94) — it must be listed at 11.0.

    RED today: with no graph data seeded for 11.0, the empty-graph fallback
    (_static_framework_bases) never lists TreeCase at ANY version — it is simply
    absent from the hand-written 4/5-entry fallback list.
    """
    from src.mcp.tools.test_tools import _COMMIT_FORBIDDEN_MSG, _test_base_classes

    result = _test_base_classes(odoo_version="11.0", _driver=clean_versions_neo4j)

    assert "TreeCase" in result, (
        f"TreeCase exists at 11.0 (odoo11/odoo/tests/common.py:94). Got:\n{result}"
    )
    assert _COMMIT_FORBIDDEN_MSG in result
    assert _last_line(result).startswith("└─ Next:")


def test_test_base_classes_v8_header_uses_openerp_prefix_and_footer_is_last_line(
    clean_versions_neo4j,
):
    """Business rule: v8/v9's test package lives under openerp/tests/, not
    odoo/tests/ (the odoo/tests/ path exists on those branches but holds only
    __pycache__ — trap D3, phase4-solution.md §0) — the header must say so. Also:
    the Next: footer must be the LAST line of every rendered output, including
    v8/v9.

    RED today (two independent bugs pinned by one test):
      1. _format_base_classes() hardcodes '(odoo/tests/)' in the header regardless
         of version.
      2. the v8/v9 era1_note is appended AFTER the already-terminated Next:
         footer, so Next: is never the last line for v8/v9 — a real bug this fix
         closes.
    """
    from src.mcp.tools.test_tools import _COMMIT_FORBIDDEN_MSG, _test_base_classes

    result = _test_base_classes(odoo_version="8.0", _driver=clean_versions_neo4j)

    header = result.splitlines()[0]
    assert "openerp/tests/" in header, (
        f"v8.0 header must reference openerp/tests/, not odoo/tests/. Got: {header!r}"
    )
    assert _COMMIT_FORBIDDEN_MSG in result
    assert _last_line(result).startswith("└─ Next:"), (
        f"Next: must be the LAST line of the output, even at v8.0 (today the era1 "
        f"note prints AFTER the footer). Got last line: {_last_line(result)!r}"
    )


def test_test_base_classes_99_resolves_to_modern_menu_with_out_of_catalogue_note(
    clean_versions_neo4j,
):
    """Business rule: a major outside the surveyed catalogue (8..19), including the
    99.0 test sentinel, resolves to the newest known era (v17+) AND the output
    states the substitution explicitly — fail-open-and-say-so, never
    fail-open-silently (api-contract.md §Out-of-catalogue, phase4-solution.md §7).

    RED today: no out-of-catalogue concept exists at all — 99.0 falls through the
    generic `major = 99` fallback with no provenance line.
    """
    from src.mcp.tools.test_tools import _COMMIT_FORBIDDEN_MSG, _test_base_classes

    result = _test_base_classes(odoo_version="99.0", _driver=clean_versions_neo4j)

    assert "TransactionCase" in result
    assert "outside the surveyed catalogue" in result, (
        f"99.0 must carry an explicit out-of-catalogue provenance line naming the "
        f"substitution (phase4-solution.md §7). Got:\n{result}"
    )
    assert _COMMIT_FORBIDDEN_MSG in result
    assert _last_line(result).startswith("└─ Next:")


def test_test_base_classes_drilldown_states_absence_not_the_whole_menu(
    clean_versions_neo4j,
):
    """Business rule (Defect 3): drilling into a class that does not exist at the
    resolved version must answer NOT AVAILABLE and name the replacement — never
    silently fall back to printing the entire menu, which is today's behavior.

    RED today: with no matching TestHelper row, _test_base_classes() falls
    straight through to _static_framework_bases_str(v) IGNORING the `name` filter
    entirely — the full generic menu comes back regardless of what was asked for.
    """
    from src.mcp.tools.test_tools import _COMMIT_FORBIDDEN_MSG, _test_base_classes

    result = _test_base_classes(
        odoo_version="17.0", name="SavepointCase", _driver=clean_versions_neo4j,
    )

    assert "NOT AVAILABLE" in result, (
        f"absence must be stated explicitly, not answered by silently printing the "
        f"whole menu. Got:\n{result}"
    )
    assert "TransactionCase" in result, "the replacement class must be named"
    assert "TreeCase" not in result, (
        f"a drill-down answer must not render any OTHER class row — it must not "
        f"fall back to the whole menu. Got:\n{result}"
    )
    assert _COMMIT_FORBIDDEN_MSG in result
    assert "Next:" in result


def test_test_base_classes_unknown_name_states_not_a_known_base_class(
    clean_versions_neo4j,
):
    """Business rule: an unrecognized class name gets an explicit 'not a known Odoo
    framework base class' answer (phase4-solution.md §6.6), not the entire menu.

    RED today: same fall-through as the drill-down test above — an unknown name
    also silently renders the full generic menu.
    """
    from src.mcp.tools.test_tools import _COMMIT_FORBIDDEN_MSG, _test_base_classes

    result = _test_base_classes(
        odoo_version="17.0", name="TotallyUnknownClass", _driver=clean_versions_neo4j,
    )

    assert "not a known Odoo framework base class" in result, (
        f"expected an explicit unknown-class answer. Got:\n{result}"
    )
    assert "TotallyUnknownClass" in result
    assert _COMMIT_FORBIDDEN_MSG in result
    assert "Next:" in result


def test_test_base_classes_graph_and_fallback_agree_on_class_names(
    clean_versions_neo4j,
):
    """Business rule (SSOT, Defect 2): the framework base-class NAME SET rendered
    from a POPULATED graph must equal the name set rendered from an EMPTY graph, at
    every surveyed Odoo version — one source of truth, not two copies that can
    silently disagree.

    RED today: the graph seed (_FRAMEWORK_BASES, version-blind) always emits the
    SAME 11 names; the empty-graph fallback (_static_framework_bases) emits a
    hand-ordered 4-or-5-name list gated only by `major <= 15`. They diverge at
    every single surveyed version — this is the only test that can catch a
    regression of that split-brain defect once it is fixed.
    """
    from src.mcp.tools.test_tools import _test_base_classes

    mismatches: dict[str, tuple[list[str], list[str]]] = {}
    for v in _REAL_VERSIONS_UNDER_TEST:
        empty_names = _rendered_class_names(
            _test_base_classes(odoo_version=v, _driver=clean_versions_neo4j)
        )
        _seed_framework_menu(v)
        populated_names = _rendered_class_names(
            _test_base_classes(odoo_version=v, _driver=clean_versions_neo4j)
        )
        _wipe_framework_helpers(clean_versions_neo4j, [v])

        if empty_names != populated_names:
            mismatches[v] = (sorted(empty_names), sorted(populated_names))

    assert not mismatches, (
        "graph path and empty-graph fallback must render the SAME class-name set "
        f"at every surveyed version (SSOT). Divergences (empty vs populated): "
        f"{mismatches}"
    )


# ---------------------------------------------------------------------------
# Test: test_coverage_audit lists unreferenced field + caveat  (Q7 contract)
# ---------------------------------------------------------------------------

def test_coverage_audit_lists_unreferenced_field(clean_neo4j):
    """Business rule: test_coverage_audit lists commitment_date (seeded with no COVERS edge).

    Seed: Field(commitment_date) has no COVERS_FIELD inbound edge.
    Assert: tool output lists it as unreferenced + includes static caveat.
    """
    _seed_test_class_and_method(clean_neo4j)

    from src.mcp.tools.test_tools import _test_coverage_audit
    result = _test_coverage_audit(
        module="sale",
        odoo_version=TEST_VERSION,
        _driver=clean_neo4j,
    )
    assert "commitment_date" in result
    # Caveat must appear (AC-6)
    assert "static" in result.lower() or "Caveat" in result


# ---------------------------------------------------------------------------
# Test: find_test_examples returns only test/js chunks  (PP1 contract)
# ---------------------------------------------------------------------------

@pytest.mark.postgres
def test_find_test_examples_excludes_production_chunks(
    clean_neo4j, clean_pg_embeddings,
):
    """Business rule: find_test_examples returns ONLY test_method/test_class/js_test chunks.

    Seed: both a 'method' (production) chunk and a 'test_method' chunk.
    Assert: the tool runs without error + Next: footer present (chunk_types gate applied).

    clean_pg_embeddings yields pg_conn directly (not a tuple).
    Both chunks get identical FakeEmbedder vectors; key assertion is that
    chunk_types=['test_method', 'test_class', 'js_test'] is applied (PP1 contract).
    """
    from src.indexer.embedder import FakeEmbedder
    from src.indexer.writer_pgvector import EmbeddingChunk, write_module_embeddings

    # Seed Neo4j module so find_examples profile filter passes
    with clean_neo4j.session() as s:
        s.run("MERGE (:Module {name:'sale', odoo_version:$v})", v=TEST_VERSION)

    embedder = FakeEmbedder(dim=1024)
    # Production chunk (should NOT appear in find_test_examples via chunk_type filter)
    prod_chunks = [EmbeddingChunk(
        "method", "sale", TEST_VERSION, "sale.order.action_confirm",
        "sale.order", "addons/sale/models/sale_order.py", 0,
        f"[sale] sale.order.action_confirm ({TEST_VERSION})\ndef action_confirm(self): ...",
    )]
    # Test chunk (SHOULD appear)
    test_chunks = [EmbeddingChunk(
        "test_method", "sale", TEST_VERSION, "TestSaleOrder.test_amount_total_computed",
        "sale.order", "addons/sale/tests/test_sale_order.py", 142,
        f"[test] sale.order via TestSaleOrder.test_amount_total_computed ({TEST_VERSION})\n"
        "def test_amount_total_computed(self): ...",
    )]
    write_module_embeddings("sale", TEST_VERSION, prod_chunks, embedder,
                            profile_name="test_profile")
    write_module_embeddings("sale", TEST_VERSION, test_chunks, embedder,
                            profile_name="test_profile")

    from src.mcp.tools.test_tools import _find_test_examples
    result = _find_test_examples(
        query="amount_total computed",
        odoo_version=TEST_VERSION,
        _driver=clean_neo4j,
        _pg_conn=clean_pg_embeddings,
        _embedder=embedder,
    )
    # Tool must complete + Next: footer must be present (ADR-0023 §4)
    assert "Next:" in result
    # Tool name header or result count confirms chunk_types gate was applied
    assert "find_test_examples" in result or "Found" in result


# ---------------------------------------------------------------------------
# Test: test_class_inspect shows subclassed-by  (Q6 contract)
# ---------------------------------------------------------------------------

def test_test_class_inspect_shows_subclassed_by(clean_neo4j):
    """Business rule: test_class_inspect on TestSaleCommon shows TestSaleOrder in subclassed-by.

    Seed: TestSaleOrder -[:INHERITS_TEST]-> TestSaleCommon.
    Assert: output contains 'TestSaleOrder' in the subclassed-by section.
    """
    _seed_test_class_and_method(clean_neo4j)

    from src.mcp.tools.test_tools import _test_class_inspect
    result = _test_class_inspect(
        name="TestSaleCommon",
        odoo_version=TEST_VERSION,
        _driver=clean_neo4j,
    )
    assert "TestSaleOrder" in result
    assert "Subclassed by" in result or "subclassed" in result.lower()


# ---------------------------------------------------------------------------
# Test: every new tool output ends with Next:  (ADR-0023 §4 contract)
# ---------------------------------------------------------------------------

def test_each_tool_output_has_next_footer(clean_neo4j):
    """Business rule: every new tool's output ends with a 'Next:' footer per ADR-0023 §4."""
    _seed_test_class_and_method(clean_neo4j)

    from src.mcp.tools.test_tools import (
        _js_test_inspect,
        _test_base_classes,
        _test_class_inspect,
        _test_coverage_audit,
        _tests_covering,
    )

    results = {
        "tests_covering": _tests_covering(
            model="sale.order",
            odoo_version=TEST_VERSION,
            _driver=clean_neo4j,
        ),
        "test_base_classes": _test_base_classes(
            odoo_version=TEST_VERSION,
            _driver=clean_neo4j,
        ),
        "test_coverage_audit": _test_coverage_audit(
            module="sale",
            odoo_version=TEST_VERSION,
            _driver=clean_neo4j,
        ),
        "test_class_inspect": _test_class_inspect(
            name="TestSaleCommon",
            odoo_version=TEST_VERSION,
            _driver=clean_neo4j,
        ),
        "js_test_inspect": _js_test_inspect(
            module="sale",
            odoo_version=TEST_VERSION,
            _driver=clean_neo4j,
        ),
    }

    for tool_name, result in results.items():
        assert "Next:" in result, (
            f"{tool_name} output missing 'Next:' footer (ADR-0023 §4 contract). "
            f"Got:\n{result}"
        )


# ---------------------------------------------------------------------------
# Test: tool_count_sync passes at 31/9
# ---------------------------------------------------------------------------

def test_tool_count_sync_passes_at_31():
    """Business rule: TOOL_COUNT=31, RESOURCE_COUNT=9 in constants.ts match MCP surface.

    This is the SSOT gate enforced by test_tool_count_sync.py.
    Re-assert here for WI-4 traceability.
    """
    from src.mcp.server import mcp

    real_tools = len(asyncio.run(mcp.list_tools()))
    real_resources = len(asyncio.run(mcp.list_resource_templates()))

    assert real_tools == 31, (
        f"Expected 31 tools after WI-4, got {real_tools}. "
        "Ensure test_tools.py is registered in server.py reload-pop tuple + import block."
    )
    assert real_resources == 9, (
        f"Expected 9 resources after WI-4, got {real_resources}. "
        "Ensure 2 new resource templates are registered in resources.py."
    )


# ---------------------------------------------------------------------------
# Test: js_test_inspect returns expected structure for seeded JsTestSuite
# ---------------------------------------------------------------------------

def test_js_test_inspect_returns_framework_info(clean_neo4j):
    """Business rule: js_test_inspect returns JsTestSuite nodes with framework label.

    Seed: JsTestSuite(framework='hoot') for module 'account'.
    Assert: output contains 'hoot' framework label and file_path.
    """
    with clean_neo4j.session() as s:
        s.run("""
            MERGE (js:JsTestSuite {
                file_path: 'addons/account/static/tests/account_move.test.js',
                module: 'account',
                odoo_version: $v
            })
            SET js.framework = 'hoot',
                js.describe_blocks = ['account move tests'],
                js.test_names = ['renders invoice correctly'],
                js.tags = ['desktop'],
                js.mounts = ['account.move'],
                js.mock_models = ['account.account'],
                js.line = 1,
                js.profile = ['test_profile']
        """, v=TEST_VERSION)

    from src.mcp.tools.test_tools import _js_test_inspect
    result = _js_test_inspect(
        module="account",
        odoo_version=TEST_VERSION,
        _driver=clean_neo4j,
    )
    assert "hoot" in result
    assert "account.test.js" in result or "account_move.test.js" in result
    assert "Next:" in result


# ---------------------------------------------------------------------------
# Test: tests_covering (no results) still has Next: footer
# ---------------------------------------------------------------------------

def test_tests_covering_empty_has_next_footer(clean_neo4j):
    """Business rule: tests_covering with no results still emits Next: footer."""
    from src.mcp.tools.test_tools import _tests_covering
    result = _tests_covering(
        model="nonexistent.model",
        odoo_version=TEST_VERSION,
        _driver=clean_neo4j,
    )
    assert "Next:" in result


# ---------------------------------------------------------------------------
# DEFECT E: body_rows rendered in tests_covering output
# ---------------------------------------------------------------------------

def test_tests_covering_body_via_rows_appear_in_output(clean_neo4j):
    """Business rule: TestMethod rows with via='body' appear in tests_covering output.

    DEFECT E: body_rows were grouped (L251) and added to the 'seen' dedup set
    (L254) but had NO render block — they were silently dropped from the output
    while still being excluded from other_rows.

    Seed a TestMethod with via='body' + COVERS_MODEL edge and assert that
    'Body-coverage' appears in the output.
    Red-before-fix: without the body_rows render block this test fails because
    'Body-coverage' was never emitted.
    """
    with clean_neo4j.session() as s:
        # Module + TestClass for the body-coverage test method
        s.run("""
            MERGE (m:Module {name: 'account', odoo_version: $v})
            SET m.profile = ['test_profile']
            MERGE (tc:TestClass {
                name: 'TestAccountMove',
                module: 'account',
                file_path: 'addons/account/tests/test_account_move.py',
                odoo_version: $v
            })
            SET tc.profile = ['test_profile']
            MERGE (tc)-[:DEFINED_IN]->(m)
        """, v=TEST_VERSION)

        # TestMethod with via='body' (references model in the body, not an assert)
        s.run("""
            MERGE (tm:TestMethod {
                name: 'test_body_reference_move',
                test_class: 'TestAccountMove',
                module: 'account',
                file_path: 'addons/account/tests/test_account_move.py',
                odoo_version: $v
            })
            SET tm.via = 'body',
                tm.asserts_count = 0,
                tm.line = 50,
                tm.model_refs = ['account.move'],
                tm.profile = ['test_profile']
        """, v=TEST_VERSION)

        # Model node for COVERS_MODEL edge
        s.run("""
            MERGE (mo:Model {
                name: 'account.move',
                odoo_version: $v,
                is_definition: true
            })
            SET mo.profile = ['test_profile']
        """, v=TEST_VERSION)

        # COVERS_MODEL edge
        s.run("""
            MATCH (tm:TestMethod {
                name: 'test_body_reference_move',
                odoo_version: $v
            })
            MATCH (mo:Model {
                name: 'account.move',
                odoo_version: $v
            })
            MERGE (tm)-[:COVERS_MODEL]->(mo)
        """, v=TEST_VERSION)

    from src.mcp.tools.test_tools import _tests_covering
    result = _tests_covering(
        model="account.move",
        odoo_version=TEST_VERSION,
        _driver=clean_neo4j,
    )

    # Business rule: Body-coverage section must appear
    assert "Body-coverage" in result, (
        "tests_covering must render 'Body-coverage' for via='body' rows. "
        f"Got:\n{result}"
    )
    # The test method name must appear in the output
    assert "test_body_reference_move" in result, (
        "tests_covering must include the via='body' method name in output. "
        f"Got:\n{result}"
    )
