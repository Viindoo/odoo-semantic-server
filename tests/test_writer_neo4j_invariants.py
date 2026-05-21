# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_writer_neo4j_invariants.py
"""Invariant tests for Neo4jWriter — property guarantees that must hold for every
node the writer creates, regardless of whether the node is a "real" definition or
an __unresolved__ placeholder.

Regression for: writer never setting is_definition on __unresolved__ placeholder
Model nodes (found 2026-05-16, 235 NULL rows in production).
"""
import os

import pytest

from src.indexer.models import ModelInfo, ModuleInfo, ParseResult
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j


@pytest.fixture
def writer(clean_neo4j, neo4j_driver):
    """Neo4jWriter connected to isolated test DB via TEST_VERSION."""
    w = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    w.setup_indexes()
    yield w
    w.close()


def test_placeholder_inherits_is_definition_not_null(writer, neo4j_driver):
    """INHERITS placeholder must have is_definition=false (not NULL).

    Regression: older writer left is_definition NULL on placeholder Model nodes
    created for unresolved INHERITS targets. Ranking Cypher coalesced NULL→false
    at query time but the on-disk NULL was misleading for audit queries.
    """
    ext_module = ModuleInfo(
        name="viin_test_inv", odoo_version=TEST_VERSION,
        repo="viin_repo", path="/tmp", depends=[], version_raw="",
    )
    ext_model = ModelInfo(
        name="custom.model", module="viin_test_inv", odoo_version=TEST_VERSION,
        inherit=["nonexistent.parent"],  # intentionally NOT seeded → triggers placeholder
    )
    writer.write_results([ParseResult(module=ext_module, models=[ext_model])])

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (placeholder:Model {name: 'nonexistent.parent',
                                      module: '__unresolved__', odoo_version: $v})
            RETURN placeholder.is_definition AS is_def,
                   placeholder.unresolved   AS unresolved
        """, v=TEST_VERSION).single()

    assert rec is not None, "placeholder node must exist after unresolved INHERITS"
    assert rec["unresolved"] is True
    assert rec["is_def"] is not None, (
        "is_definition must NOT be NULL on placeholder — was left NULL by older writer"
    )
    assert rec["is_def"] is False, (
        "is_definition must be false (not true) on __unresolved__ placeholder"
    )


def test_placeholder_delegates_to_is_definition_not_null(writer, neo4j_driver):
    """DELEGATES_TO placeholder must have is_definition=false (not NULL).

    Same regression as INHERITS placeholder — both code paths must set
    is_definition on ON CREATE.
    """
    hr_module = ModuleInfo(
        name="hr_test_inv", odoo_version=TEST_VERSION,
        repo="hr_repo", path="/tmp", depends=[], version_raw="",
    )
    hr_model = ModelInfo(
        name="hr.employee", module="hr_test_inv", odoo_version=TEST_VERSION,
        inherits={"res.partner.unresolved": "partner_id"},  # target NOT seeded
    )
    writer.write_results([ParseResult(module=hr_module, models=[hr_model])])

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (placeholder:Model {name: 'res.partner.unresolved',
                                      module: '__unresolved__', odoo_version: $v})
            RETURN placeholder.is_definition AS is_def,
                   placeholder.unresolved   AS unresolved
        """, v=TEST_VERSION).single()

    assert rec is not None, "placeholder node must exist after unresolved DELEGATES_TO"
    assert rec["unresolved"] is True
    assert rec["is_def"] is not None, (
        "is_definition must NOT be NULL on DELEGATES_TO placeholder"
    )
    assert rec["is_def"] is False, (
        "is_definition must be false on __unresolved__ placeholder"
    )
