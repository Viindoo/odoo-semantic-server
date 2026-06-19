# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_pattern_seed_no_test_pollution.py
"""Regression: seeder must not introduce test-fixture pollution into Neo4j,
while still permitting the sanctioned production ``test-*`` patterns.

Found 2026-05-16: 9 PatternExample nodes in production Neo4j with pattern_id
matching 't-*', 'test-*', 'snap-*', 'pipeline-seed-*' or odoo_version_min in
['99.0','93.0'] — leftovers from test runs that wrote directly to the live DB.
At that time NO production pattern used the ``test-`` prefix, so banning it
wholesale was a correct anti-pollution heuristic (PR #108 / e3d61df).

Since PR #323 (595554f) the catalogue ships 8 *production* ``test-*`` patterns
(``category == "test"`` in src/data/patterns.json). The guard's intent is
unchanged - keep test-fixture leftovers out of prod - but ``test-`` is no
longer a reliable pollution signal on its own. This test now asserts that
after a clean seed: the 8 sanctioned ``test-*`` patterns are PERMITTED (they
are real production data), while genuine fixture leftovers (``t-*``, ``snap-*``,
``pipeline-seed-*``, non-sanctioned ``test-*``, and the 99.0/93.0 test
versions) are still flagged.
"""
import json
import os
from pathlib import Path

import pytest

from src.indexer.models import PatternExample
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j

# Forbidden prefixes that indicate test-only data. NOTE: ``test-`` is forbidden
# UNLESS the pattern_id is a sanctioned production pattern (category == "test"
# in src/data/patterns.json - see _PRODUCTION_TEST_IDS below). The other three
# prefixes are unconditionally forbidden: no production pattern uses them.
_FORBIDDEN_PREFIXES = ["t-", "test-", "snap-", "pipeline-seed-"]
# Forbidden odoo_version_min values used exclusively in test suites
_FORBIDDEN_VERSIONS = ["99.0", "93.0"]

# SSOT for the sanctioned production ``test-*`` patterns. Loaded at import time
# from patterns.json (the same file the seeder reads), resolved portably from
# this test file's location - NO hardcoded absolute path. This allowlist
# self-updates when test patterns are added/removed; it is NOT a hardcoded id
# list (the independent oracle is the ``test-bogus`` negative case below).
_PATTERNS_JSON = Path(__file__).resolve().parents[1] / "src" / "data" / "patterns.json"
_PRODUCTION_TEST_IDS = {
    p["pattern_id"]
    for p in json.loads(_PATTERNS_JSON.read_text())
    if p.get("category") == "test"
}


def _count_polluted(session) -> int:
    """Return count of PatternExample nodes that match the pollution criteria.

    A ``test-`` node is pollution ONLY when its pattern_id is NOT a sanctioned
    production pattern. ``t-``, ``snap-``, ``pipeline-seed-`` and the
    99.0/93.0 versions remain unconditionally forbidden.
    """
    prefix_conditions = " OR ".join(
        f"p.pattern_id STARTS WITH '{pfx}'" for pfx in _FORBIDDEN_PREFIXES
    )
    version_list = "[" + ", ".join(f"'{v}'" for v in _FORBIDDEN_VERSIONS) + "]"
    result = session.run(
        f"MATCH (p:PatternExample) WHERE "
        # A forbidden-prefix or forbidden-version match is pollution...
        f"(({prefix_conditions}) OR p.odoo_version_min IN {version_list}) "
        # ...UNLESS the id is a sanctioned production test pattern.
        "AND NOT p.pattern_id IN $production_ids "
        "RETURN count(p) AS cnt",
        production_ids=list(_PRODUCTION_TEST_IDS),
    ).single()
    return result["cnt"] if result else 0


@pytest.fixture
def writer(clean_neo4j, neo4j_driver):
    """Neo4jWriter on isolated test DB.

    `clean_neo4j` only deletes nodes with `odoo_version` property, but
    PatternExample uses `odoo_version_min` (no `odoo_version`), so it does
    not get wiped between tests. Other tests in the suite (test_writer_neo4j,
    test_mcp_pattern_tools, test_output_snapshots) seed `t-*` and `snap-*`
    pattern_ids that persist in the shared Neo4j container - explicitly delete
    POLLUTION PatternExample nodes here to guarantee determinism for the
    pollution-count assertion.

    Sanctioned production ``test-*`` nodes (category=test in patterns.json) are
    DELIBERATELY NOT pre-deleted: leaving them in place is what makes the
    pollution test actually exercise the new allowlist logic instead of being
    blind to production data.
    """
    with neo4j_driver.session() as session:
        prefix_conditions = " OR ".join(
            f"p.pattern_id STARTS WITH '{pfx}'" for pfx in _FORBIDDEN_PREFIXES
        )
        version_list = "[" + ", ".join(f"'{v}'" for v in _FORBIDDEN_VERSIONS) + "]"
        session.run(
            f"MATCH (p:PatternExample) WHERE "
            f"(({prefix_conditions}) OR p.odoo_version_min IN {version_list}) "
            "AND NOT p.pattern_id IN $production_ids "
            "DETACH DELETE p",
            production_ids=list(_PRODUCTION_TEST_IDS),
        )
    w = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    w.setup_indexes()
    yield w
    w.close()


def _make_legit_pattern(idx: int) -> PatternExample:
    """Return a legitimate-looking PatternExample (no forbidden prefix/version)."""
    return PatternExample(
        pattern_id=f"computed-field-test-inv-{idx}",
        intent_keywords=["computed", "field"],
        file_ref=f"models/model.py:{idx}",
        snippet_text="x = fields.Char(compute='_compute_x')",
        gotchas=["must store=True for search", "depends path must be full dotted"],
        odoo_version_min="17.0",
        language="python",
    )


def _make_sanctioned_pattern(pattern_id: str) -> PatternExample:
    """Return a sanctioned production ``test-*`` PatternExample node."""
    return PatternExample(
        pattern_id=pattern_id,
        intent_keywords=["test", "odoo"],
        file_ref="addons/foo/tests/test_foo.py:1",
        snippet_text="class TestFoo(TransactionCase): ...",
        gotchas=[
            "assert on observable behaviour, not internals",
            "use SavepointCase only when no commit is needed",
            "tag the class with at-tagged_test",
        ],
        odoo_version_min="16.0",
        language="python",
    )


def test_pollution_guard_allows_sanctioned_test_patterns(writer, neo4j_driver):
    """The 8 sanctioned production ``test-*`` patterns are NOT pollution.

    Writing every sanctioned ``test-*`` pattern (category=test in patterns.json)
    must yield a pollution count of 0. Before the guard was narrowed, the
    predicate flagged every ``test-*`` node, so this would have reported all
    sanctioned patterns as pollution.
    """
    assert _PRODUCTION_TEST_IDS, (
        "Expected sanctioned production test patterns in patterns.json - "
        "category=='test' set is empty; allowlist would be vacuous."
    )
    sanctioned = [_make_sanctioned_pattern(pid) for pid in sorted(_PRODUCTION_TEST_IDS)]
    writer.write_pattern_examples(sanctioned)

    with neo4j_driver.session() as session:
        count = _count_polluted(session)

    assert count == 0, (
        f"Found {count} PatternExample node(s) flagged as pollution after "
        "writing sanctioned production test-* patterns. The pollution guard "
        "must exempt ids whose category=='test' in src/data/patterns.json."
    )


def test_no_pollution_after_legit_seed(writer, neo4j_driver):
    """After seeding non-prefixed legitimate patterns, zero pollution nodes exist."""
    legit = [_make_legit_pattern(i) for i in range(3)]
    writer.write_pattern_examples(legit)

    with neo4j_driver.session() as session:
        count = _count_polluted(session)

    assert count == 0, (
        f"Found {count} PatternExample node(s) with forbidden prefix/version — "
        "test data leaked into Neo4j. Remove t-*, snap-*, pipeline-seed-* "
        "prefixes, non-sanctioned test-*, and 99.0/93.0 version values before "
        "writing to Neo4j."
    )


def test_forbidden_prefix_patterns_written_and_detected(writer, neo4j_driver):
    """Detector still catches genuine fixture pollution (independent oracle).

    This is the negative oracle that proves the guard was not over-broadened
    into exempting every ``test-*`` node. A ``t-`` leftover AND a non-sanctioned
    ``test-bogus`` leftover must both be flagged. If the predicate exempted all
    ``test-*``, the ``test-bogus`` node would slip through and the count would
    drop below 2.
    """
    assert "test-bogus" not in _PRODUCTION_TEST_IDS, (
        "test-bogus must NOT be a sanctioned id for this negative oracle to hold."
    )
    polluted = [
        PatternExample(
            pattern_id="t-bad-pattern",
            intent_keywords=["test"],
            file_ref="tests/test_foo.py:1",
            snippet_text="# test only",
            gotchas=["not for production"],
            odoo_version_min=TEST_VERSION,
            language="python",
        ),
        PatternExample(
            pattern_id="test-bogus",
            intent_keywords=["test"],
            file_ref="tests/test_bar.py:1",
            snippet_text="# fixture leftover, not a sanctioned pattern",
            gotchas=["not for production"],
            odoo_version_min="17.0",
            language="python",
        ),
    ]
    writer.write_pattern_examples(polluted)

    with neo4j_driver.session() as session:
        count = _count_polluted(session)

    assert count >= 2, (
        "Expected both the 't-bad-pattern' and the non-sanctioned 'test-bogus' "
        f"nodes to be flagged as pollution, got count={count}. The pollution "
        "guard must NOT exempt every test-* node - only sanctioned production ids."
    )
