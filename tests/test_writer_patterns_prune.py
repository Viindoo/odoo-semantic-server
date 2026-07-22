# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for the R1 orphan-on-rename fix in ``write_pattern_examples``.

Requires Neo4j (testcontainers or the CI service container). Mark: pytest.mark.neo4j.

Root cause (issue #362 follow-up, R1): ``src/data/patterns.json`` renamed a
``pattern_id`` (``odoo-module-owl2-component-v15`` ->
``odoo-module-owl1-component-v15``). ``PatternExample`` is MERGE-keyed on
``pattern_id`` ALONE, and ``write_pattern_examples`` was MERGE-only with no
prune, so the OLD id's node survived every reseed forever and stayed reachable
via the ``odoo://{version}/pattern/{pattern_id}`` resource (direct-id fetch).
pgvector is already a clean DELETE-then-INSERT, so this was Neo4j-only.

These tests protect BEHAVIOR, not internals:
  1. a full-catalogue reseed with an id renamed/removed leaves NO stale node,
     while every kept pattern survives (the R1 fix);
  2. a PARTIAL (version-filtered, prune=False) reseed must NOT delete patterns
     absent from the partial batch (the defensive contract - a global prune on
     ``seed-patterns --version 15.0`` would wipe every other version);
  3. an empty incoming list never prunes (never-prune-to-empty safety).

Self-isolating: ``PatternExample`` has no ``odoo_version`` property, so
``clean_neo4j`` (which deletes by ``odoo_version``) does NOT wipe these nodes.
The ``writer`` fixture seeds and tears down its own ``t-r1prune-*`` marker nodes
explicitly, and every assertion is scoped to that prefix so it is deterministic
regardless of anything else in the shared session container.
"""
import os

import pytest

from src.indexer.models import PatternExample
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j

# ``t-`` + ``99.0`` are BOTH flagged as pollution by
# tests/test_pattern_seed_no_test_pollution.py, so any leak of these nodes is
# actively cleaned by that guard's fixture too - belt and suspenders on top of
# this file's own teardown.
_MARKER_PREFIX = "t-r1prune-"


def _pattern(pid: str) -> PatternExample:
    """A minimal, schema-valid PatternExample carrying the marker prefix."""
    return PatternExample(
        pattern_id=pid,
        intent_keywords=["prune", "rename"],
        file_ref="addons/foo/models/foo.py:1",
        snippet_text="class Foo(models.Model):\n    _name = 'foo'",
        gotchas=["gotcha one", "gotcha two", "gotcha three"],
        odoo_version_min=TEST_VERSION,
        odoo_version_max=None,
        language="python",
        core_symbol_names=[],
        category=None,
    )


def _delete_markers(driver) -> None:
    with driver.session() as session:
        session.run(
            "MATCH (pe:PatternExample) WHERE pe.pattern_id STARTS WITH $pfx "
            "DETACH DELETE pe",
            pfx=_MARKER_PREFIX,
        )


def _marker_ids_present(driver) -> set[str]:
    with driver.session() as session:
        row = session.run(
            "MATCH (pe:PatternExample) WHERE pe.pattern_id STARTS WITH $pfx "
            "RETURN collect(pe.pattern_id) AS ids",
            pfx=_MARKER_PREFIX,
        ).single()
    return set(row["ids"]) if row is not None else set()


@pytest.fixture
def writer(clean_neo4j, neo4j_driver):
    """Neo4jWriter on the isolated test DB, with marker nodes cleaned around it."""
    _delete_markers(neo4j_driver)
    w = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    w.setup_pattern_indexes()
    yield w
    _delete_markers(neo4j_driver)
    w.close()


def test_full_reseed_prunes_pattern_orphaned_by_id_rename(writer, neo4j_driver):
    """Business rule: on a FULL-catalogue reseed, a PatternExample whose
    pattern_id no longer appears in the catalogue (renamed or removed) MUST be
    deleted from the graph, while every kept pattern remains.

    This is the exact R1 scenario: patterns.json renamed owl2 -> owl1. Without
    the prune the owl2 node survives forever and ``odoo://{v}/pattern/owl2``
    keeps serving stale content.
    """
    old_id = _MARKER_PREFIX + "owl2-component-v15"   # the pre-rename id
    new_id = _MARKER_PREFIX + "owl1-component-v15"   # the rename target
    kept_a = _MARKER_PREFIX + "computed-field"
    kept_b = _MARKER_PREFIX + "constrains-guard"

    # Catalogue as it was BEFORE the rename (plain MERGE, no prune yet).
    writer.write_pattern_examples([_pattern(old_id), _pattern(kept_a), _pattern(kept_b)])
    assert _marker_ids_present(neo4j_driver) == {old_id, kept_a, kept_b}

    # Reseed with the renamed catalogue: owl2 -> owl1, kept_a/kept_b unchanged.
    # This is the full-catalogue path, so prune=True.
    writer.write_pattern_examples(
        [_pattern(new_id), _pattern(kept_a), _pattern(kept_b)],
        prune=True,
    )

    present = _marker_ids_present(neo4j_driver)
    assert old_id not in present, (
        f"stale PatternExample {old_id!r} survived a full reseed - this is the "
        f"R1 orphan-on-rename bug. Present markers: {sorted(present)}"
    )
    assert new_id in present, (
        f"rename target {new_id!r} missing after reseed. Present: {sorted(present)}"
    )
    assert {kept_a, kept_b} <= present, (
        f"kept patterns were wrongly pruned. Present: {sorted(present)}"
    )
    assert present == {new_id, kept_a, kept_b}, (
        f"graph must equal exactly the reseeded catalogue. Present: {sorted(present)}"
    )


def test_partial_reseed_with_prune_false_spares_absent_patterns(writer, neo4j_driver):
    """Defensive contract: ``write_pattern_examples(..., prune=False)`` - the
    version-filtered / partial-batch path - MUST NOT delete patterns absent from
    the batch.

    ``write_pattern_examples`` is called with a PARTIAL list by
    ``seed-patterns --version 15.0`` (``_load_patterns`` filters by
    ``odoo_version_min``). A naive GLOBAL prune there would wipe every other
    version's live patterns - so partial callers pass prune=False, and this test
    locks that a prune=False write never deletes.
    """
    v15 = _MARKER_PREFIX + "v15-only"
    v16 = _MARKER_PREFIX + "v16-only"

    # Establish both versions via a full pruning reseed.
    writer.write_pattern_examples([_pattern(v15), _pattern(v16)], prune=True)
    assert _marker_ids_present(neo4j_driver) == {v15, v16}

    # A partial (version-filtered) reseed carrying ONLY v15 must NOT prune v16.
    writer.write_pattern_examples([_pattern(v15)], prune=False)

    present = _marker_ids_present(neo4j_driver)
    assert v16 in present, (
        f"partial (prune=False) reseed wrongly deleted {v16!r} - the defensive "
        f"contract for version-filtered batches was violated. Present: {sorted(present)}"
    )
    assert v15 in present


def test_empty_catalogue_with_prune_never_wipes(writer, neo4j_driver):
    """Safety: an empty incoming list must NEVER prune. A transient empty load
    (DB unreachable, filter matched nothing) must not wipe the live catalogue.
    """
    survivor = _MARKER_PREFIX + "survivor"
    writer.write_pattern_examples([_pattern(survivor)], prune=True)
    assert survivor in _marker_ids_present(neo4j_driver)

    # Empty list + prune=True must be a no-op (early return, no delete-all).
    writer.write_pattern_examples([], prune=True)
    assert survivor in _marker_ids_present(neo4j_driver), (
        "empty incoming catalogue with prune=True wiped a live pattern - the "
        "never-prune-to-empty safety guard is missing"
    )
