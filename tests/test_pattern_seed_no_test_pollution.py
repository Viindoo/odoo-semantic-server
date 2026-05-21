# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_pattern_seed_no_test_pollution.py
"""Regression: seeder must not introduce test-prefixed PatternExample nodes
into Neo4j after a clean seed run.

Found 2026-05-16: 9 PatternExample nodes in production Neo4j with pattern_id
matching 't-*', 'test-*', 'snap-*', 'pipeline-seed-*' or odoo_version_min in
['99.0','93.0'] — leftovers from test runs that wrote directly to the live DB.

This test asserts that after the writer seeds a clean set of PatternExamples,
none of the disallowed prefixes or test version strings are present.
"""
import os

import pytest

from src.indexer.models import PatternExample
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j

# Forbidden prefixes that indicate test-only data
_FORBIDDEN_PREFIXES = ["t-", "test-", "snap-", "pipeline-seed-"]
# Forbidden odoo_version_min values used exclusively in test suites
_FORBIDDEN_VERSIONS = ["99.0", "93.0"]


def _count_polluted(session) -> int:
    """Return count of PatternExample nodes that match the pollution criteria."""
    prefix_conditions = " OR ".join(
        f"p.pattern_id STARTS WITH '{pfx}'" for pfx in _FORBIDDEN_PREFIXES
    )
    version_list = "[" + ", ".join(f"'{v}'" for v in _FORBIDDEN_VERSIONS) + "]"
    result = session.run(
        f"MATCH (p:PatternExample) WHERE {prefix_conditions} "
        f"OR p.odoo_version_min IN {version_list} "
        "RETURN count(p) AS cnt"
    ).single()
    return result["cnt"] if result else 0


@pytest.fixture
def writer(clean_neo4j, neo4j_driver):
    """Neo4jWriter on isolated test DB.

    `clean_neo4j` only deletes nodes with `odoo_version` property, but
    PatternExample uses `odoo_version_min` (no `odoo_version`), so it does
    not get wiped between tests. Other tests in the suite (test_writer_neo4j,
    test_mcp_pattern_tools, test_output_snapshots) seed `t-*` and `snap-*`
    pattern_ids that persist in the shared Neo4j container — explicitly
    delete forbidden-prefix PatternExample nodes here to guarantee
    determinism for the pollution-count assertion.
    """
    with neo4j_driver.session() as session:
        prefix_conditions = " OR ".join(
            f"p.pattern_id STARTS WITH '{pfx}'" for pfx in _FORBIDDEN_PREFIXES
        )
        version_list = "[" + ", ".join(f"'{v}'" for v in _FORBIDDEN_VERSIONS) + "]"
        session.run(
            f"MATCH (p:PatternExample) WHERE {prefix_conditions} "
            f"OR p.odoo_version_min IN {version_list} "
            "DETACH DELETE p"
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


def test_no_forbidden_prefix_after_seed(writer, neo4j_driver):
    """After seeding legitimate patterns, zero forbidden-prefix nodes exist."""
    legit = [_make_legit_pattern(i) for i in range(3)]
    writer.write_pattern_examples(legit)

    with neo4j_driver.session() as session:
        count = _count_polluted(session)

    assert count == 0, (
        f"Found {count} PatternExample node(s) with forbidden prefix/version — "
        "test data leaked into Neo4j. Remove t-*, test-*, snap-*, pipeline-seed-* "
        "prefixes and 99.0/93.0 version values before writing to Neo4j."
    )


def test_forbidden_prefix_patterns_written_and_detected(writer, neo4j_driver):
    """Detector correctly identifies forbidden-prefix nodes if they exist.

    Regression guard: ensures the _count_polluted helper is not trivially broken
    by a silent cypher error (returns 0 for everything).
    """
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
    ]
    writer.write_pattern_examples(polluted)

    with neo4j_driver.session() as session:
        count = _count_polluted(session)

    assert count >= 1, (
        "Expected at least 1 forbidden-prefix node after writing a 't-' pattern — "
        "_count_polluted helper may be broken."
    )
