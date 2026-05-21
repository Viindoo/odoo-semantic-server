# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_diff_engine_lifecycle.py
"""Tests for lifecycle diff engine extension (PR#11 fix WI-F2).

Per ADR-0002 §2 (revised): lifecycle expressed as properties (added_in,
removed_in, deprecated_in) on CoreSymbol nodes, NOT as separate edges.
REPLACED_BY remains the only true edge between two different symbols.
"""
import os

import pytest

from src.indexer.diff_engine import DiffResult, compute_diff
from src.indexer.models import CoreSymbolInfo
from src.indexer.writer_neo4j import Neo4jWriter

pytestmark = pytest.mark.neo4j

_VERSION_OLD = "92.0"
_VERSION_NEW = "91.0"


def _sym(qname, version, kind="function", status="stable", **kwargs):
    return CoreSymbolInfo(
        qualified_name=qname, kind=kind, odoo_version=version,
        status=status, **kwargs,
    )


@pytest.fixture(scope="module")
def lifecycle_writer(neo4j_driver):
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()
    with neo4j_driver.session() as session:
        for v in (_VERSION_OLD, _VERSION_NEW):
            session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=v)
    yield writer
    with neo4j_driver.session() as session:
        for v in (_VERSION_OLD, _VERSION_NEW):
            session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=v)
    writer.close()


# ---------------------------------------------------------------------------
# Test 1: added_in property set on newly-added symbol
# ---------------------------------------------------------------------------

class TestDiffAddedEmitsAddedInProperty:
    def test_added_symbol_gets_added_in_property(
        self, lifecycle_writer, neo4j_driver,
    ):
        """Symbol only in new version → added_in = new_version on the new node."""
        old = []
        new = [_sym("odoo.tools.zip_dir", _VERSION_NEW)]
        diff = compute_diff(old, new)

        # Seed the NEW node in DB
        lifecycle_writer.write_core_symbols(new)
        # Apply lifecycle properties
        lifecycle_writer.write_lifecycle_properties(
            diff, from_version=_VERSION_OLD, to_version=_VERSION_NEW,
        )

        with neo4j_driver.session() as session:
            rec = session.run("""
                MATCH (cs:CoreSymbol {qualified_name: $qn, odoo_version: $v})
                RETURN cs.added_in AS added_in
            """, qn="odoo.tools.zip_dir", v=_VERSION_NEW).single()

        assert rec is not None
        assert rec["added_in"] == _VERSION_NEW


# ---------------------------------------------------------------------------
# Test 2: removed_in property set on removed symbol
# ---------------------------------------------------------------------------

class TestDiffRemovedEmitsRemovedInProperty:
    def test_removed_symbol_gets_removed_in_property(
        self, lifecycle_writer, neo4j_driver,
    ):
        """Symbol only in old version → removed_in = new_version on the OLD node."""
        old_sym = _sym("odoo.models.BaseModel.name_get", _VERSION_OLD)
        old = [old_sym]
        new = []
        diff = compute_diff(old, new)

        # Seed OLD node
        lifecycle_writer.write_core_symbols([old_sym])
        lifecycle_writer.write_lifecycle_properties(
            diff, from_version=_VERSION_OLD, to_version=_VERSION_NEW,
        )

        with neo4j_driver.session() as session:
            rec = session.run("""
                MATCH (cs:CoreSymbol {qualified_name: $qn, odoo_version: $v})
                RETURN cs.removed_in AS removed_in
            """, qn="odoo.models.BaseModel.name_get", v=_VERSION_OLD).single()

        assert rec is not None
        assert rec["removed_in"] == _VERSION_NEW


# ---------------------------------------------------------------------------
# Test 3: deprecated_in property set on symbol changing status to deprecated
# ---------------------------------------------------------------------------

class TestDiffDeprecatedEmitsDeprecatedInProperty:
    def test_deprecated_symbol_gets_deprecated_in_property(
        self, lifecycle_writer, neo4j_driver,
    ):
        """Symbol stable@old, deprecated@new → deprecated_in = new_version on new node."""
        old_sym = _sym("odoo.tools.safe_eval.safe_eval", _VERSION_OLD, status="stable")
        new_sym = _sym("odoo.tools.safe_eval.safe_eval", _VERSION_NEW, status="deprecated")
        diff = compute_diff([old_sym], [new_sym])

        # Seed both versions
        lifecycle_writer.write_core_symbols([old_sym, new_sym])
        lifecycle_writer.write_lifecycle_properties(
            diff, from_version=_VERSION_OLD, to_version=_VERSION_NEW,
        )

        with neo4j_driver.session() as session:
            rec = session.run("""
                MATCH (cs:CoreSymbol {qualified_name: $qn, odoo_version: $v})
                RETURN cs.deprecated_in AS deprecated_in
            """, qn="odoo.tools.safe_eval.safe_eval", v=_VERSION_NEW).single()

        assert rec is not None
        assert rec["deprecated_in"] == _VERSION_NEW


# ---------------------------------------------------------------------------
# Test 4: REPLACED_BY edge regression — must still be emitted
# ---------------------------------------------------------------------------

class TestDiffReplacedByRegressionUnchanged:
    def test_replaced_by_edge_still_emitted(
        self, lifecycle_writer, neo4j_driver,
    ):
        """Regression: REPLACED_BY edge must still be created alongside properties."""
        old_sym = _sym(
            "odoo.fields.Field.group_operator", _VERSION_OLD,
            replacement_qname="odoo.fields.Field.aggregator",
        )
        new_sym = _sym("odoo.fields.Field.aggregator", _VERSION_NEW)
        diff = compute_diff([old_sym], [new_sym])

        lifecycle_writer.write_core_symbols([old_sym, new_sym])
        lifecycle_writer.write_diff_edges(
            diff, from_version=_VERSION_OLD, to_version=_VERSION_NEW,
        )

        with neo4j_driver.session() as session:
            edge = session.run("""
                MATCH (a:CoreSymbol {qualified_name: $old_qn, odoo_version: $vfrom})
                      -[:REPLACED_BY]->
                      (b:CoreSymbol {qualified_name: $new_qn, odoo_version: $vto})
                RETURN 1 AS found
            """, old_qn="odoo.fields.Field.group_operator", vfrom=_VERSION_OLD,
                 new_qn="odoo.fields.Field.aggregator", vto=_VERSION_NEW).single()

        assert edge is not None, "REPLACED_BY edge was not created"


# ---------------------------------------------------------------------------
# Pure unit tests for compute_diff — deprecated bucket
# ---------------------------------------------------------------------------

class TestComputeDiffDeprecatedBucket:
    def test_diff_deprecated_detected_when_status_changes(self):
        """compute_diff produces deprecated bucket when status stable → deprecated."""
        old = [_sym("odoo.models.BaseModel.read_group", "17.0", status="stable")]
        new = [_sym("odoo.models.BaseModel.read_group", "18.0", status="deprecated")]
        diff = compute_diff(old, new)
        assert isinstance(diff, DiffResult)
        assert any(
            s.qualified_name == "odoo.models.BaseModel.read_group"
            for s in diff.deprecated
        ), f"Expected deprecated entry, got: {diff.deprecated}"

    def test_diff_no_deprecated_when_both_stable(self):
        """No deprecated entry when both old and new are stable."""
        old = [_sym("odoo.tools.safe_eval.safe_eval", "17.0", status="stable")]
        new = [_sym("odoo.tools.safe_eval.safe_eval", "18.0", status="stable")]
        diff = compute_diff(old, new)
        assert diff.deprecated == []

    def test_diff_deprecated_not_double_counted_in_removed(self):
        """Symbol that went stable→deprecated is in deprecated, NOT in removed."""
        old = [_sym("odoo.fields.Field.group_operator", "17.0", status="stable")]
        new = [_sym("odoo.fields.Field.group_operator", "18.0", status="deprecated")]
        diff = compute_diff(old, new)
        # present in both versions → not removed
        qnames_removed = [s.qualified_name for s in diff.removed]
        assert "odoo.fields.Field.group_operator" not in qnames_removed
