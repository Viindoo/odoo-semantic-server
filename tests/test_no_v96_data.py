# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Invariant test to prevent v96.0 test data from being re-introduced.

This test verifies that no nodes with odoo_version='96.0' exist in the database.
It guards against regressions after the OBS-5 cleanup script is executed.

EXPECTED BEHAVIOR:
- Before Phase 3 cleanup script runs: TEST FAILS (data still present) — this is intentional
- After Phase 3 cleanup script runs: TEST PASSES (data cleaned)
"""

import pytest

pytestmark = pytest.mark.neo4j


def test_no_v96_test_data_leak(neo4j_driver):
    """
    Assert: No nodes with odoo_version='96.0' exist in the database.

    This test is intentionally marked to fail until the v96.0 cleanup script
    (scripts/cleanup_v96.cypher) is executed in Phase 3. It serves as a guard
    against re-introduction of test data in future runs.
    """
    query = """
        MATCH (n {odoo_version: '96.0'})
        RETURN count(n) AS count
    """
    with neo4j_driver.session() as session:
        result = session.run(query).single()
    count = result["count"]

    assert count == 0, (
        f"Found {count} node(s) with odoo_version='96.0'. "
        "This is test data that should have been cleaned by "
        "scripts/cleanup_v96.cypher (Phase 3 cleanup task)."
    )
