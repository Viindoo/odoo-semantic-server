# tests/test_writer_neo4j_stub_profile.py
"""Tests for stub node profile ownership (ADR-0016 D7).

Verifies that cross-module reference placeholder nodes created by writer_neo4j
inherit the profile array of the REFERENCING module, not NULL.

Regression for: 5,988 NULL-profile stub nodes accumulating in production
on every reindex (found 2026-05-17).

All tests in this file require Neo4j (CLAUDE.md module-level marker convention).
The pure-unit test for the v9 OWL era guard lives in test_parser_js.py.
"""
import os

import pytest

from src.indexer.models import (
    JSGraphResult,
    JSPatchInfo,
    ModelInfo,
    ModuleInfo,
    ParseResult,
)
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


def test_stub_node_inherits_referrer_profile(writer, neo4j_driver):
    """INHERITS placeholder must carry the referencing module's profile array.

    Regression for: writer created __unresolved__ Model nodes with NULL profile,
    making them invisible to profile-scoped MCP queries.
    """
    referrer_profile = "test_profile_stub"

    ext_module = ModuleInfo(
        name="viin_stub_test", odoo_version=TEST_VERSION,
        repo="viin_repo", path="/tmp", depends=[], version_raw="",
    )
    ext_model = ModelInfo(
        name="custom.stubmodel", module="viin_stub_test", odoo_version=TEST_VERSION,
        inherit=["mail.thread.stub"],  # intentionally NOT seeded → triggers placeholder
    )
    writer.write_results(
        [ParseResult(module=ext_module, models=[ext_model])],
        profiles=[referrer_profile],
    )

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (placeholder:Model {name: 'mail.thread.stub',
                                      module: '__unresolved__', odoo_version: $v})
            RETURN placeholder.profile AS profile,
                   placeholder.unresolved AS unresolved
        """, v=TEST_VERSION).single()

    assert rec is not None, "__unresolved__ placeholder must exist after unresolved INHERITS"
    assert rec["unresolved"] is True
    assert rec["profile"] is not None, (
        "placeholder.profile must NOT be NULL — ADR-0016 D7: stub inherits referrer profile"
    )
    assert referrer_profile in rec["profile"], (
        f"referrer profile '{referrer_profile}' must appear in placeholder.profile"
    )


def test_reindex_idempotent_no_new_stubs(writer, neo4j_driver):
    """Running the writer twice with same input must not grow the NULL-profile stub count.

    Verifies that ON CREATE SET is idempotent — a second write to an existing
    __unresolved__ node (ON MATCH path) does not clear profile back to NULL.
    """
    referrer_profile = "test_profile_idem"

    ext_module = ModuleInfo(
        name="viin_idem_test", odoo_version=TEST_VERSION,
        repo="viin_repo", path="/tmp", depends=[], version_raw="",
    )
    ext_model = ModelInfo(
        name="custom.idemmodel", module="viin_idem_test", odoo_version=TEST_VERSION,
        inherit=["res.partner.idem.stub"],  # intentionally NOT seeded
    )

    parse_results = [ParseResult(module=ext_module, models=[ext_model])]

    # First write
    writer.write_results(parse_results, profiles=[referrer_profile])

    # Count NULL-profile nodes after first write
    with neo4j_driver.session() as session:
        count_1 = session.run(
            "MATCH (n {odoo_version: $v}) WHERE n.module = '__unresolved__'"
            " AND n.profile IS NULL RETURN count(n) AS c",
            v=TEST_VERSION,
        ).single()["c"]

    # Second write — same data
    writer.write_results(parse_results, profiles=[referrer_profile])

    # Count NULL-profile nodes after second write — must not grow
    with neo4j_driver.session() as session:
        count_2 = session.run(
            "MATCH (n {odoo_version: $v}) WHERE n.module = '__unresolved__'"
            " AND n.profile IS NULL RETURN count(n) AS c",
            v=TEST_VERSION,
        ).single()["c"]

    assert count_1 == 0, (
        "After first write, no __unresolved__ nodes should have NULL profile"
    )
    assert count_2 == 0, (
        "After second write (idempotent), still no __unresolved__ nodes should have NULL profile"
    )


def test_stub_profile_unions_on_second_referencer(writer, neo4j_driver):
    """Stub MERGE'd by referencer A then referencer B must hold BOTH profiles.

    Regression for clobber bug: ON MATCH SET <node>.profile = $profiles would
    REPLACE A's profile when B references the same `__unresolved__` MERGE key,
    making the stub invisible to A's scoped queries. Mirror the real-node
    union semantics from commit 4ff56a8.
    """
    profile_a = "test_profile_union_a"
    profile_b = "test_profile_union_b"

    mod_a = ModuleInfo(
        name="viin_union_a", odoo_version=TEST_VERSION,
        repo="viin_repo", path="/tmp/a", depends=[], version_raw="",
    )
    model_a = ModelInfo(
        name="custom.unionmodel.a", module="viin_union_a", odoo_version=TEST_VERSION,
        inherit=["mail.thread.union.shared"],  # shared unresolved target
    )

    mod_b = ModuleInfo(
        name="viin_union_b", odoo_version=TEST_VERSION,
        repo="viin_repo", path="/tmp/b", depends=[], version_raw="",
    )
    model_b = ModelInfo(
        name="custom.unionmodel.b", module="viin_union_b", odoo_version=TEST_VERSION,
        inherit=["mail.thread.union.shared"],  # same target as A
    )

    # Profile A indexes first
    writer.write_results(
        [ParseResult(module=mod_a, models=[model_a])],
        profiles=[profile_a],
    )

    # Profile B indexes second — hits the same `__unresolved__` MERGE key
    writer.write_results(
        [ParseResult(module=mod_b, models=[model_b])],
        profiles=[profile_b],
    )

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (placeholder:Model {name: 'mail.thread.union.shared',
                                      module: '__unresolved__', odoo_version: $v})
            RETURN placeholder.profile AS profile
        """, v=TEST_VERSION).single()

    assert rec is not None, "Shared __unresolved__ placeholder must exist"
    assert profile_a in rec["profile"], (
        f"After B writes, A's profile '{profile_a}' must still be in stub.profile "
        f"(got {rec['profile']}) — ON MATCH SET must UNION not REPLACE"
    )
    assert profile_b in rec["profile"], (
        f"After B writes, B's profile '{profile_b}' must also be in stub.profile "
        f"(got {rec['profile']})"
    )


def test_owl_patches_placeholder_inherits_profile(writer, neo4j_driver):
    """OWLComp PATCHES placeholder must carry the referencing JSPatch's profile array.

    Verifies site 6 fix: unresolved PATCHES edge creates an OWLComp placeholder
    with profile set (not NULL).
    """
    referrer_profile = "test_profile_patches"

    js_module = ModuleInfo(
        name="viin_js_stub", odoo_version=TEST_VERSION,
        repo="viin_repo", path="/tmp", depends=[], version_raw="",
    )
    patch_info = JSPatchInfo(
        target="NonExistentComp",  # intentionally NOT seeded → triggers placeholder
        patch_name="my_patch",
        module="viin_js_stub",
        odoo_version=TEST_VERSION,
        era="patch",
        file_path="/tmp/test.js",
    )
    js_result = JSGraphResult(module=js_module, patches=[patch_info])

    writer.write_js_graph_results([js_result], profiles=[referrer_profile])

    with neo4j_driver.session() as session:
        rec = session.run("""
            MATCH (placeholder:OWLComp {name: 'NonExistentComp',
                                        module: '__unresolved__', odoo_version: $v})
            RETURN placeholder.profile AS profile,
                   placeholder.unresolved AS unresolved
        """, v=TEST_VERSION).single()

    assert rec is not None, "OWLComp placeholder must exist after unresolved PATCHES edge"
    assert rec["unresolved"] is True
    assert rec["profile"] is not None, (
        "OWLComp placeholder.profile must NOT be NULL — ADR-0016 D7"
    )
    assert referrer_profile in rec["profile"], (
        f"referrer profile '{referrer_profile}' must appear in OWLComp placeholder.profile"
    )
