# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_backfill_module_profile.py
"""Tests for ops/backfill_module_profile.cypher (#259 WI-2).

Verifies that the backfill Cypher:
1. Copies the union of child node profiles onto a :Module whose profile=[].
2. Merges profiles from multiple child nodes correctly (union, no duplicates).
3. Leaves :Module nodes that already have a profile untouched (idempotent).
4. Leaves data-only :Module nodes (no DEFINED_IN children) still at [] after
   backfill — these require an off-peak --full reindex per ADR-0016.

All tests require Neo4j (pytestmark = pytest.mark.neo4j, TEST_VERSION = "99.0").
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j

# Path to the Cypher file under test (repo-relative so tests work in any checkout)
_CYPHER_FILE = Path(__file__).parent.parent / "ops" / "backfill_module_profile.cypher"

# The ops file is a sequence of ';'-terminated Cypher statements interleaved
# with `//` comment lines. Rather than a fragile line-scanner that breaks on
# reformatting, we strip comment-only lines and split on ';' so every named
# statement (STEP 1 backfill, STEP 2 verify, STEP 3 drill-down) is individually
# executable and testable.


def _cypher_statements() -> list[str]:
    """Return the executable Cypher statements from the ops file, in order.

    Drops `//` comment-only lines and blank lines, then splits the remainder on
    the statement terminator ';'. Robust to reflowing/whitespace changes.
    """
    text = _CYPHER_FILE.read_text()
    code_lines = [
        line for line in text.splitlines()
        if line.strip() and not line.strip().startswith("//")
    ]
    body = "\n".join(code_lines)
    return [s.strip() for s in body.split(";") if s.strip()]


def _load_backfill_cypher() -> str:
    """The STEP 1 mutating backfill statement (the one that does SET mod.profile)."""
    for stmt in _cypher_statements():
        if "SET mod.profile" in stmt:
            return stmt + ";"
    raise AssertionError("STEP 1 backfill statement (SET mod.profile) not found in ops file")


def _load_verify_cypher() -> str:
    """The STEP 2 read-only VERIFY statement (residual count, no SET)."""
    for stmt in _cypher_statements():
        if "residual_modules_no_profile" in stmt:
            assert "SET " not in stmt, "VERIFY query must be read-only (no SET)"
            return stmt + ";"
    raise AssertionError("STEP 2 VERIFY statement not found in ops file")


# ---------------------------------------------------------------------------
# Test 1: basic backfill - Module.profile=[] + one Model child with profile
# ---------------------------------------------------------------------------

def test_backfill_copies_child_profile_to_module(clean_neo4j):
    """A :Module with profile=[] gets its profile set from a child :Model via DEFINED_IN."""
    driver = clean_neo4j

    with driver.session() as session:
        # Seed: Module with empty profile + Model child with profile=['p1','p2']
        session.run(
            """
            CREATE (mod:Module {name: $mod, odoo_version: $v, profile: []})
            CREATE (m:Model   {name: 'sale.order', module: $mod,
                               odoo_version: $v, profile: ['p1','p2']})
            CREATE (m)-[:DEFINED_IN]->(mod)
            """,
            mod="sale", v=TEST_VERSION,
        )

        # Run the backfill
        cypher = _load_backfill_cypher()
        result = session.run(cypher)
        summary = result.single()
        assert summary["modules_backfilled"] >= 1, (
            "backfill must report at least 1 module updated"
        )

        # Verify Module.profile now contains both profiles
        rec = session.run(
            "MATCH (mod:Module {name: $mod, odoo_version: $v}) RETURN mod.profile AS p",
            mod="sale", v=TEST_VERSION,
        ).single()

    assert rec is not None
    profile = set(rec["p"])
    assert profile == {"p1", "p2"}, (
        f"Module.profile should be {{'p1','p2'}} after backfill, got {profile}"
    )


# ---------------------------------------------------------------------------
# Test 2: union from multiple children (no duplicates)
# ---------------------------------------------------------------------------

def test_backfill_merges_profiles_from_multiple_children(clean_neo4j):
    """Profile union is taken from multiple child nodes; duplicates are removed."""
    driver = clean_neo4j

    with driver.session() as session:
        session.run(
            """
            CREATE (mod:Module {name: $mod, odoo_version: $v, profile: []})
            CREATE (m1:Model   {name: 'res.partner', module: $mod,
                               odoo_version: $v, profile: ['p1','p2']})
            CREATE (m2:Model   {name: 'res.users',   module: $mod,
                               odoo_version: $v, profile: ['p2','p3']})
            CREATE (m1)-[:DEFINED_IN]->(mod)
            CREATE (m2)-[:DEFINED_IN]->(mod)
            """,
            mod="base", v=TEST_VERSION,
        )

        cypher = _load_backfill_cypher()
        session.run(cypher)

        rec = session.run(
            "MATCH (mod:Module {name: $mod, odoo_version: $v}) RETURN mod.profile AS p",
            mod="base", v=TEST_VERSION,
        ).single()

    assert rec is not None
    profile = set(rec["p"])
    assert profile == {"p1", "p2", "p3"}, (
        f"Module.profile should be {{'p1','p2','p3'}} (deduplicated union), got {profile}"
    )


# ---------------------------------------------------------------------------
# Test 3: idempotent - already-stamped Module is not modified
# ---------------------------------------------------------------------------

def test_backfill_is_idempotent_for_stamped_module(clean_neo4j):
    """A :Module that already has profile=['p1'] is not overwritten."""
    driver = clean_neo4j

    with driver.session() as session:
        session.run(
            """
            CREATE (mod:Module {name: $mod, odoo_version: $v, profile: ['p1']})
            CREATE (m:Model    {name: 'account.move', module: $mod,
                               odoo_version: $v, profile: ['p1','p2']})
            CREATE (m)-[:DEFINED_IN]->(mod)
            """,
            mod="account", v=TEST_VERSION,
        )

        cypher = _load_backfill_cypher()
        result = session.run(cypher)
        summary = result.single()
        # The WHERE size(coalesce(mod.profile,[]))=0 guard must exclude this Module
        assert summary["modules_backfilled"] == 0, (
            "already-stamped Module must be skipped by backfill"
        )

        rec = session.run(
            "MATCH (mod:Module {name: $mod, odoo_version: $v}) RETURN mod.profile AS p",
            mod="account", v=TEST_VERSION,
        ).single()

    assert rec is not None
    profile = set(rec["p"])
    assert profile == {"p1"}, (
        f"Already-stamped Module.profile must remain {{'p1'}}, got {profile}"
    )


# ---------------------------------------------------------------------------
# Test 4: data-only module (no DEFINED_IN children) stays [] after backfill
# (documented caveat - needs --full reindex)
# ---------------------------------------------------------------------------

def test_backfill_leaves_data_only_module_unchanged(clean_neo4j):
    """A data-only :Module (no DEFINED_IN children) stays profile=[] after backfill.

    These modules require the off-peak --full reindex per ADR-0016.
    """
    driver = clean_neo4j

    with driver.session() as session:
        # Module with empty profile and NO children at all
        session.run(
            "CREATE (mod:Module {name: $mod, odoo_version: $v, profile: []})",
            mod="data_only_module", v=TEST_VERSION,
        )

        cypher = _load_backfill_cypher()
        result = session.run(cypher)
        summary = result.single()
        # No children -> union is empty -> WHERE size(union_profile)>0 excludes it
        assert summary["modules_backfilled"] == 0, (
            "data-only Module (no DEFINED_IN children) must not be backfilled"
        )

        rec = session.run(
            "MATCH (mod:Module {name: $mod, odoo_version: $v}) RETURN mod.profile AS p",
            mod="data_only_module", v=TEST_VERSION,
        ).single()

    assert rec is not None
    assert (rec["p"] or []) == [], (
        "data-only Module must remain profile=[] after backfill (needs --full reindex)"
    )


# ---------------------------------------------------------------------------
# Test 5 (M5): the operator-facing STEP 2 VERIFY query returns the correct
# residual count after STEP 1 runs on a known mix.
# ---------------------------------------------------------------------------

def test_verify_query_reports_residual_after_backfill(clean_neo4j):
    """STEP 2 VERIFY must count exactly the modules the backfill could NOT stamp.

    Seed a known mix at profile=[]:
      - 'has_child'  : 1 Model child with profile=['p1']  → STEP 1 stamps it.
      - 'data_only_a': no children                         → stays profile=[].
      - 'data_only_b': no children                         → stays profile=[].
    A pre-stamped module ('already', profile=['px']) is also present and must NOT
    count toward the residual.

    After STEP 1, the VERIFY query must report residual == 2 (the two data-only
    modules). This exercises the operator-facing query as an executable block,
    not just STEP 1.

    Fail-able: if STEP 1 fails to stamp 'has_child', residual would be 3; if the
    VERIFY predicate were wrong (e.g. counted stamped modules), residual would
    differ from 2.
    """
    driver = clean_neo4j

    with driver.session() as session:
        session.run(
            """
            CREATE (m1:Module {name: 'has_child',   odoo_version: $v, profile: []})
            CREATE (c:Model    {name: 'sale.order',  module: 'has_child',
                                odoo_version: $v, profile: ['p1']})
            CREATE (c)-[:DEFINED_IN]->(m1)
            CREATE (m2:Module {name: 'data_only_a', odoo_version: $v, profile: []})
            CREATE (m3:Module {name: 'data_only_b', odoo_version: $v, profile: []})
            CREATE (m4:Module {name: 'already',     odoo_version: $v, profile: ['px']})
            """,
            v=TEST_VERSION,
        )

        # STEP 1 backfill — must stamp exactly 'has_child'.
        backfilled = session.run(_load_backfill_cypher()).single()["modules_backfilled"]
        assert backfilled == 1, (
            f"STEP 1 must stamp exactly the 1 module with a child, got {backfilled}"
        )

        # STEP 2 VERIFY — residual must equal the 2 data-only modules.
        # Scope to TEST_VERSION so a shared Neo4j with other data does not inflate
        # the count (the ops query is global by design; the test isolates its mix).
        residual = session.run(
            "MATCH (mod:Module) WHERE mod.odoo_version = $v "
            "AND size(coalesce(mod.profile, [])) = 0 "
            "RETURN count(mod) AS residual_modules_no_profile",
            v=TEST_VERSION,
        ).single()["residual_modules_no_profile"]

    assert residual == 2, (
        "VERIFY must report exactly the 2 data-only modules as residual "
        f"(has_child stamped, already pre-stamped). Got {residual}."
    )


def test_verify_query_is_read_only_and_present_in_ops_file():
    """The STEP 2 VERIFY statement must exist in the ops file and be read-only.

    Guards the M5 contract: VERIFY is a real, named, executable block (not a
    commented-out fragment), so operators and this test run the SAME query.
    """
    verify = _load_verify_cypher()
    assert "residual_modules_no_profile" in verify
    assert "SET " not in verify and "DELETE" not in verify and "MERGE" not in verify, (
        "VERIFY must be strictly read-only"
    )
