# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_writer_neo4j_stub_profile.py
"""Tests for stub (placeholder) node profile ownership.

ADR-0034 single-owner provenance SUPERSEDES the earlier ADR-0016 D7 rule for
cross-reference placeholders. A placeholder created for an UNRESOLVED reference
(unresolved INHERITS / DELEGATES_TO / INHERITS_VIEW / EXTENDS_TMPL / PATCHES) is
a node the current run merely REFERENCES, not one it OWNS — so the writer must
NOT stamp the referencing run's profile onto it. Doing so unioned a foreign
tenant-private profile name onto a node owned elsewhere (and, for the View/QWeb
placeholders that converge on the real node's {xmlid, version} key, polluted the
real owner), which the ADR-0034 `all()` choke then mis-handled.

New contract (this file): a reference placeholder is created PROFILE-LESS. The
read-side `size(profile) > 0` F-6 guard then fail-CLOSES it to scoped tenants
(admin, `$own IS NULL`, still sees it) until the referent is indexed under its
OWN owning profile. The original 2026-05-17 concern these tests guarded —
placeholder nodes ACCUMULATING on every reindex — is still covered: the MERGE
keys are stable, so reindex does not grow the placeholder count.

All tests in this file require Neo4j (CLAUDE.md module-level marker convention).
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


def test_stub_node_is_profile_less_failclosed(writer, neo4j_driver):
    """INHERITS placeholder must be created PROFILE-LESS (ADR-0034), not stamped
    with the referencing module's profile.

    A referenced (unresolved) node is owned by whatever run eventually indexes it,
    NOT by the run that references it. Stamping the referrer's profile here unions
    a foreign tenant-private name onto a node owned elsewhere — the scope-choke
    pollution this fix removes. Profile-less ⇒ the read-side `size(profile)>0`
    guard fail-closes it to scoped tenants until the real model is indexed.
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
    assert not rec["profile"], (
        "placeholder.profile must be PROFILE-LESS (NULL/empty) — ADR-0034 supersedes "
        f"ADR-0016 D7: a referenced node is not owned by the referrer (got {rec['profile']!r})"
    )


def test_reindex_does_not_accumulate_stubs(writer, neo4j_driver):
    """Running the writer twice with the same input must not grow the placeholder
    count — the stable MERGE key makes reindex idempotent (the original
    anti-accumulation guarantee), and the placeholders remain profile-less.
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

    def _count_unresolved():
        with neo4j_driver.session() as session:
            return session.run(
                "MATCH (n {odoo_version: $v}) WHERE n.module = '__unresolved__'"
                " RETURN count(n) AS c",
                v=TEST_VERSION,
            ).single()["c"]

    # First write
    writer.write_results(parse_results, profiles=[referrer_profile])
    count_1 = _count_unresolved()

    # Second write — same data (ON MATCH path)
    writer.write_results(parse_results, profiles=[referrer_profile])
    count_2 = _count_unresolved()

    assert count_1 == 1, "first write must create exactly one __unresolved__ placeholder"
    assert count_2 == count_1, (
        "reindex must not accumulate placeholder nodes — the stable MERGE key makes "
        f"a second write idempotent (got {count_1} then {count_2})"
    )

    # And it is profile-less (fail-closed), per ADR-0034.
    with neo4j_driver.session() as session:
        prof = session.run(
            "MATCH (n:Model {name: 'res.partner.idem.stub', module: '__unresolved__',"
            " odoo_version: $v}) RETURN n.profile AS profile",
            v=TEST_VERSION,
        ).single()["profile"]
    assert not prof, f"placeholder must stay profile-less across reindex (got {prof!r})"


def test_stub_stays_profile_less_across_referencers(writer, neo4j_driver):
    """A placeholder MERGE'd by referencer A then referencer B must stay
    PROFILE-LESS — neither referencing run owns it, so neither stamps a profile.

    This is the security-critical case: under the old behaviour the stub would
    carry the union [A, B] (two different tenants' profiles), which the choke
    could then expose. Profile-less keeps it fail-closed for both until the real
    target is indexed under its own owner.
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
    assert not rec["profile"], (
        "shared placeholder must stay PROFILE-LESS — neither referencer A nor B owns it; "
        f"stamping either would risk a cross-tenant union leak (got {rec['profile']!r})"
    )


def test_owl_patches_placeholder_is_profile_less(writer, neo4j_driver):
    """OWLComp PATCHES placeholder must be created PROFILE-LESS (ADR-0034).

    The patched component is a node the JSPatch run REFERENCES, not owns — so the
    placeholder is created without a profile and fail-closes to scoped tenants
    until the real OWLComp is indexed under its own owner.
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
    assert not rec["profile"], (
        "OWLComp placeholder.profile must be PROFILE-LESS (NULL/empty) — ADR-0034 "
        f"supersedes ADR-0016 D7 (got {rec['profile']!r})"
    )
