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
  3. an empty incoming list never prunes (never-prune-to-empty safety);
  4. the WIRING at seed_patterns.run() and the seed-patterns CLI - not just the
     Neo4jWriter primitive above - actually passes prune=True/False correctly
     for the full-catalogue vs version-filtered call sites (a future refactor
     that silently inverts or drops `prune=odoo_version_min_filter is None` /
     `prune=args.version is None` would otherwise go uncaught).

Self-isolating: ``PatternExample`` has no ``odoo_version`` property, so
``clean_neo4j`` (which deletes by ``odoo_version``) does NOT wipe these nodes.
The ``writer`` fixture seeds and tears down its own ``t-r1prune-*`` marker nodes
explicitly, and every assertion is scoped to that prefix so it is deterministic
regardless of anything else in the shared session container.

CROSS-FILE ISOLATION (important): every test below issues at least one
``prune=True`` call, and that Cypher (``MATCH (pe:PatternExample) WHERE NOT
pe.pattern_id IN $live_ids DETACH DELETE pe``) is - by the R1 contract itself -
GLOBAL across the *entire* PatternExample label, not scoped to this file's
marker prefix. Several sibling integration test files write real (non-marker)
PatternExample fixtures into the SAME session-scoped ``neo4j_driver`` and never
clean them up, because ``clean_neo4j`` only wipes by ``odoo_version`` and
PatternExample carries no such property (e.g. ``tests/test_writer_neo4j.py``'s
``t-pattern-1``/``t-pattern-idem``/``t-pattern-ce``/``t-pattern-skip``,
``tests/test_pattern_catalogue_invariants.py``'s ``portal-sudo-*`` /
``mail-thread-*`` / ``owl-onmounted-*`` / ``domain-or-operator-*``). Left
unguarded, this file's prune calls would silently DETACH DELETE those other
files' nodes as collateral damage on every full ``make test-integration`` /
CI run (they collect alphabetically before this file). The ``writer`` fixture
therefore snapshots every non-marker PatternExample (+ its USES_CORE_SYMBOL
target names) before each test and restores them afterward, so this file's
genuinely-global prune assertions can never permanently delete a sibling
test's data regardless of pytest collection order.
"""
import json
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


def _pattern(pid: str, *, version: str = TEST_VERSION) -> PatternExample:
    """A minimal, schema-valid PatternExample carrying the marker prefix."""
    return PatternExample(
        pattern_id=pid,
        intent_keywords=["prune", "rename"],
        file_ref="addons/foo/models/foo.py:1",
        snippet_text="class Foo(models.Model):\n    _name = 'foo'",
        gotchas=["gotcha one", "gotcha two", "gotcha three"],
        odoo_version_min=version,
        odoo_version_max=None,
        language="python",
        core_symbol_names=[],
        category=None,
    )


def _pattern_json(pid: str, *, version: str = TEST_VERSION) -> dict:
    """Raw patterns.json-shaped dict (same content as ``_pattern``), for the
    seed_patterns.run() / CLI wiring tests which load through the real
    JSON-schema-validated file path rather than constructing PatternExample
    objects directly."""
    return {
        "pattern_id": pid,
        "intent_keywords": ["prune", "rename"],
        "file_ref": "addons/foo/models/foo.py:1",
        "snippet_text": "class Foo(models.Model):\n    _name = 'foo'",
        "gotchas": ["gotcha one", "gotcha two", "gotcha three"],
        "odoo_version_min": version,
        "language": "python",
    }


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


def _snapshot_foreign_patterns(driver) -> list[dict]:
    """Snapshot every PatternExample node NOT under our marker prefix, plus its
    USES_CORE_SYMBOL target names, so this file's genuinely-global prune=True
    calls can restore whatever sibling test files left behind (see module
    docstring - CROSS-FILE ISOLATION)."""
    with driver.session() as session:
        return session.run(
            """
            MATCH (pe:PatternExample) WHERE NOT pe.pattern_id STARTS WITH $pfx
            OPTIONAL MATCH (pe)-[:USES_CORE_SYMBOL]->(cs:CoreSymbol)
            RETURN pe.pattern_id AS pattern_id,
                   pe.intent_keywords AS intent_keywords,
                   pe.file_ref AS file_ref,
                   pe.snippet_text AS snippet_text,
                   pe.gotchas AS gotchas,
                   pe.odoo_version_min AS odoo_version_min,
                   pe.odoo_version_max AS odoo_version_max,
                   pe.language AS language,
                   pe.category AS category,
                   collect(DISTINCT cs.qualified_name) AS core_symbol_names
            """,
            pfx=_MARKER_PREFIX,
        ).data()


def _restore_foreign_patterns(writer: Neo4jWriter, snapshot: list[dict]) -> None:
    """Re-MERGE every snapshotted foreign PatternExample (prune=False - a plain
    restore must never itself prune)."""
    if not snapshot:
        return
    restored = [
        PatternExample(
            pattern_id=row["pattern_id"],
            intent_keywords=row["intent_keywords"] or [],
            file_ref=row["file_ref"],
            snippet_text=row["snippet_text"],
            gotchas=row["gotchas"] or [],
            odoo_version_min=row["odoo_version_min"],
            odoo_version_max=row["odoo_version_max"],
            language=row["language"],
            core_symbol_names=[n for n in row["core_symbol_names"] if n],
            category=row["category"],
        )
        for row in snapshot
    ]
    writer.write_pattern_examples(restored)


@pytest.fixture
def writer(clean_neo4j, neo4j_driver):
    """Neo4jWriter on the isolated test DB, with marker nodes cleaned around it
    AND every foreign (non-marker) PatternExample snapshotted + restored around
    the test - see module docstring, CROSS-FILE ISOLATION."""
    _delete_markers(neo4j_driver)
    foreign_snapshot = _snapshot_foreign_patterns(neo4j_driver)
    w = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    w.setup_pattern_indexes()
    yield w
    _delete_markers(neo4j_driver)
    _restore_foreign_patterns(w, foreign_snapshot)
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


# ---------------------------------------------------------------------------
# Wiring: seed_patterns.run() must gate prune on odoo_version_min_filter
# ---------------------------------------------------------------------------


def test_full_reseed_via_run_prunes_renamed_pattern(writer, neo4j_driver, tmp_path):
    """Wiring regression guard: ``seed_patterns.run()`` must compute
    ``prune=odoo_version_min_filter is None`` and actually forward it to
    ``write_pattern_examples`` - this exercises the real ``run()`` gating logic
    end to end (file -> load -> write), not just the Neo4jWriter primitive
    covered above, so a future refactor that drops or inverts the gate is
    caught here.
    """
    from src.indexer.seed_patterns import run as seed_run

    old_id = _MARKER_PREFIX + "wire-rename-old"
    new_id = _MARKER_PREFIX + "wire-rename-new"

    patterns_v1 = tmp_path / "patterns_v1.json"
    patterns_v1.write_text(json.dumps([_pattern_json(old_id)]))
    result_1 = seed_run(
        writer=writer, embedder=None, force=True,
        patterns_file=patterns_v1, odoo_version_min_filter=None,
    )
    assert result_1["skipped"] is False
    assert old_id in _marker_ids_present(neo4j_driver)

    patterns_v2 = tmp_path / "patterns_v2.json"
    patterns_v2.write_text(json.dumps([_pattern_json(new_id)]))
    result_2 = seed_run(
        writer=writer, embedder=None, force=True,
        patterns_file=patterns_v2, odoo_version_min_filter=None,
    )
    assert result_2["skipped"] is False

    present = _marker_ids_present(neo4j_driver)
    assert old_id not in present, (
        "run() with odoo_version_min_filter=None must prune the renamed-away "
        "id - the FULL-catalogue wiring (`prune=odoo_version_min_filter is "
        f"None`) regressed. Present: {sorted(present)}"
    )
    assert new_id in present


def test_partial_reseed_via_run_spares_other_versions(writer, neo4j_driver, tmp_path):
    """Wiring regression guard: ``seed_patterns.run()`` called with
    ``odoo_version_min_filter`` SET (the version-filtered CLI path, e.g.
    ``--version 15.0``) must compute ``prune=False`` - a catalogue file that
    also contains a DIFFERENT version's pattern must not have that other
    pattern pruned just because this run only loaded one version's subset.
    """
    from src.indexer.seed_patterns import run as seed_run

    v15_id = _MARKER_PREFIX + "wire-v15-only"
    v16_id = _MARKER_PREFIX + "wire-v16-only"

    full_file = tmp_path / "patterns_full.json"
    full_file.write_text(json.dumps([
        _pattern_json(v15_id, version="15.0"),
        _pattern_json(v16_id, version="16.0"),
    ]))
    seed_run(
        writer=writer, embedder=None, force=True,
        patterns_file=full_file, odoo_version_min_filter=None,
    )
    assert {v15_id, v16_id} <= _marker_ids_present(neo4j_driver)

    # Version-filtered reseed off the SAME file: _load_patterns keeps only the
    # 15.0 entry, so the wiring must pass prune=False here.
    seed_run(
        writer=writer, embedder=None, force=True,
        patterns_file=full_file, odoo_version_min_filter="15.0",
    )

    present = _marker_ids_present(neo4j_driver)
    assert v16_id in present, (
        "run() with odoo_version_min_filter='15.0' wrongly pruned the other "
        f"version's pattern {v16_id!r} - the version-filtered wiring regressed "
        f"to always-prune. Present: {sorted(present)}"
    )


def test_cli_wiring_full_prunes_and_version_filtered_spares(
    writer, neo4j_driver, tmp_path, monkeypatch,
):
    """Wiring regression guard for the ``seed-patterns`` CLI entry point:
    ``main()`` must compute ``prune=args.version is None`` and forward it
    through ``_write_neo4j``. Exercises BOTH states via the real argv parser,
    independent of the ``run()`` wiring tested above (a separate call site with
    its own duplicated gate).
    """
    from src.indexer.seed_patterns import main as seed_main

    monkeypatch.setenv("NEO4J_URI", os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"))
    monkeypatch.setenv("NEO4J_USER", os.getenv("NEO4J_TEST_USER", "neo4j"))
    monkeypatch.setenv("NEO4J_PASSWORD", os.getenv("NEO4J_TEST_PASSWORD", "password"))

    old_id = _MARKER_PREFIX + "cli-wire-old"
    new_id = _MARKER_PREFIX + "cli-wire-new"
    other_version_id = _MARKER_PREFIX + "cli-wire-other-version"

    full_file = tmp_path / "cli_full.json"
    full_file.write_text(json.dumps([
        _pattern_json(old_id), _pattern_json(other_version_id, version="16.0"),
    ]))
    rc = seed_main(["--patterns-file", str(full_file), "--no-embed", "--force"])
    assert rc == 0
    assert {old_id, other_version_id} <= _marker_ids_present(neo4j_driver)

    # Full-catalogue reseed (no --version): the renamed-away id must be pruned.
    renamed_file = tmp_path / "cli_renamed.json"
    renamed_file.write_text(json.dumps([
        _pattern_json(new_id), _pattern_json(other_version_id, version="16.0"),
    ]))
    rc = seed_main(["--patterns-file", str(renamed_file), "--no-embed", "--force"])
    assert rc == 0
    present = _marker_ids_present(neo4j_driver)
    assert old_id not in present, (
        "seed-patterns CLI without --version must prune - "
        "`prune=args.version is None` wiring regressed"
    )
    assert new_id in present
    assert other_version_id in present

    # Version-filtered reseed (--version 16.0), file carries ONLY that version:
    # must NOT prune new_id (TEST_VERSION) even though it's absent here.
    v16_only_file = tmp_path / "cli_v16_only.json"
    v16_only_file.write_text(json.dumps([
        _pattern_json(other_version_id, version="16.0"),
    ]))
    rc = seed_main([
        "--patterns-file", str(v16_only_file), "--no-embed", "--force",
        "--version", "16.0",
    ])
    assert rc == 0
    present = _marker_ids_present(neo4j_driver)
    assert new_id in present, (
        "seed-patterns CLI with --version must NOT prune other versions - "
        "`prune=args.version is None` wiring regressed to always-prune"
    )
    assert other_version_id in present
