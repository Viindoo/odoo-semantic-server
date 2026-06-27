# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_module_identity_neo4j.py
"""Integration (Neo4j) tests for the module identity card + edition reclassify +
profile coverage (issue #121 P1/P2/P5 + must-fix H1/M1/M4).

All tests use TEST_VERSION (99.0) + the conftest ``clean_neo4j`` fixture, which
wipes that version before AND after each test, so every test is isolated.

Covered behaviours:
  * writer roundtrip of shortdesc/author + coalesce-ON-MATCH preservation (P2)
  * check_module_exists identity block (render when populated, hide when NULL),
    incl. the direct factual fix (the "VNIs" display name is surfaced)
  * describe_module Display-name header + Author in the Manifest sub-tree
  * H1: the reclassified viindoo edition outranks 'custom' in the ORM same-name
    field tiebreak (the blast-radius the review flagged)
  * coverage superset-diff (in_profile vs indexed_elsewhere) + caveat (M1)
  * coverage tenant-leak guard (ADR-0034) + name-required guard (M4)
"""
import os

import pytest

from src.indexer.models import ModuleInfo, ParseResult
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j


def _writer() -> Neo4jWriter:
    return Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )


def _import_server():
    """Re-import src.mcp.server bound to the test Neo4j (lazy connect on call)."""
    os.environ["NEO4J_URI"] = os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687")
    os.environ["NEO4J_USER"] = os.getenv("NEO4J_TEST_USER", "neo4j")
    os.environ["NEO4J_PASSWORD"] = os.getenv("NEO4J_TEST_PASSWORD", "password")
    import sys
    sys.modules.pop("src.mcp.server", None)
    import src.mcp.server as srv  # noqa: PLC0415
    return srv


# --- P2: writer roundtrip + coalesce ----------------------------------------


def test_writer_roundtrip_shortdesc_author(clean_neo4j):
    """A Module written with shortdesc+author reads back with both values."""
    writer = _writer()
    writer.setup_indexes()
    mod = ModuleInfo(
        name="id_round_mod", odoo_version=TEST_VERSION, repo="r",
        path="/tmp/id_round_mod", depends=[],
        shortdesc="Round Display Name", author="Round Author, Inc",
        summary="Round summary",
    )
    writer.write_results([ParseResult(module=mod)], profiles=["default"])
    writer.close()

    with clean_neo4j.session() as session:
        rec = session.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) "
            "RETURN m.shortdesc AS s, m.author AS a, m.summary AS sm",
            n="id_round_mod", v=TEST_VERSION,
        ).single()
    assert rec["s"] == "Round Display Name"
    assert rec["a"] == "Round Author, Inc"
    assert rec["sm"] == "Round summary"


def test_writer_coalesce_on_match_preserves_identity(clean_neo4j):
    """A re-index that omits author/shortdesc (None) must NOT erase prior values
    (coalesce ON MATCH - same safety pattern as summary/repo_url)."""
    writer = _writer()
    writer.setup_indexes()
    first = ModuleInfo(
        name="id_coalesce_mod", odoo_version=TEST_VERSION, repo="r",
        path="/tmp/id_coalesce_mod", depends=[],
        shortdesc="Original Name", author="Original Author",
    )
    writer.write_results([ParseResult(module=first)], profiles=["default"])
    # Second pass: a manifest that does not declare name/author -> None.
    second = ModuleInfo(
        name="id_coalesce_mod", odoo_version=TEST_VERSION, repo="r",
        path="/tmp/id_coalesce_mod", depends=[],
        shortdesc=None, author=None,
    )
    writer.write_results([ParseResult(module=second)], profiles=["default"])
    writer.close()

    with clean_neo4j.session() as session:
        rec = session.run(
            "MATCH (m:Module {name: $n, odoo_version: $v}) "
            "RETURN m.shortdesc AS s, m.author AS a",
            n="id_coalesce_mod", v=TEST_VERSION,
        ).single()
    assert rec["s"] == "Original Name", "shortdesc must survive a None re-write"
    assert rec["a"] == "Original Author", "author must survive a None re-write"


# --- P2: check_module_exists identity block ---------------------------------


def _seed_module(driver, *, name, edition="community", license_val=None,
                 shortdesc=None, summary=None, author=None, repo="test_repo",
                 category=None, profile=("default",)):
    with driver.session() as session:
        session.run(
            "MERGE (m:Module {name: $n, odoo_version: $v}) "
            "SET m.edition = $edition, m.license = $license, m.repo = $repo, "
            "    m.shortdesc = $shortdesc, m.summary = $summary, m.author = $author, "
            "    m.category = $category, m.profile = $profile",
            n=name, v=TEST_VERSION, edition=edition, license=license_val,
            repo=repo, shortdesc=shortdesc, summary=summary, author=author,
            category=category, profile=list(profile),
        )


def test_check_module_exists_identity_block_when_populated(clean_neo4j):
    srv = _import_server()
    _seed_module(
        clean_neo4j, name="id_card_full", edition="viindoo", license_val="OPL-1",
        shortdesc="E-Invoice - Misa meInvoice Integrator",
        summary="Integrate with Misa meInvoice service to issue legal e-Invoice",
        author="T.V.T Marine Automation (aka TVTMA),Viindoo",
    )
    out = srv._check_module_exists("id_card_full", TEST_VERSION)
    assert "Identity (from indexed manifest):" in out
    assert "Display name: E-Invoice - Misa meInvoice Integrator" in out
    assert "Summary:" in out
    assert "Author: T.V.T Marine Automation (aka TVTMA),Viindoo" in out


def test_check_module_exists_no_identity_block_when_null(clean_neo4j):
    """A module with no shortdesc/summary/author renders NO identity block
    (graceful degrade before the --full backfill)."""
    srv = _import_server()
    _seed_module(clean_neo4j, name="id_card_empty", edition="community")
    out = srv._check_module_exists("id_card_empty", TEST_VERSION)
    assert "Identity (from indexed manifest)" not in out
    assert "Display name" not in out
    # Sanity: it is still reported as indexed (block omission is graceful).
    assert "Indexed:         Yes" in out


def test_check_module_exists_einvoice_vnis_surfaced(clean_neo4j):
    """Direct fix for the issue #121 factual error: the human display name
    'VNIs VN-Invoice Integrator' must surface so the agent does not have to guess
    the provider from the slug (which wrongly produced 'VNPT')."""
    srv = _import_server()
    _seed_module(
        clean_neo4j, name="l10n_vn_viin_accounting_vninvoice",
        edition="viindoo", license_val="OPL-1", repo="tvtmaaddons17",
        shortdesc="E-Invoice - VNIs VN-Invoice Integrator",
        summary="Integrates with VN-Invoice service to issue legal e-Invoice",
        author="T.V.T Marine Automation (aka TVTMA),Viindoo",
    )
    out = srv._check_module_exists("l10n_vn_viin_accounting_vninvoice", TEST_VERSION)
    assert "VNIs" in out, f"Display name 'VNIs' must surface, got:\n{out}"
    assert "Display name: E-Invoice - VNIs VN-Invoice Integrator" in out
    # The edition label is Viindoo-branded, never an Odoo Enterprise one (#263).
    assert "Viindoo" in out
    assert "Odoo Enterprise" not in out


# --- P2: describe_module Display name header + Author -----------------------


def test_describe_module_display_name_header_and_author(clean_neo4j):
    srv = _import_server()
    _seed_module(
        clean_neo4j, name="id_describe_mod", edition="viindoo", license_val="OPL-1",
        shortdesc="E-Invoice - Misa meInvoice Integrator",
        summary="Integrate with Misa meInvoice service to issue legal e-Invoice",
        author="T.V.T Marine Automation (aka TVTMA),Viindoo",
        category="Accounting/Localizations/EDI",
    )
    out = srv._describe_module("id_describe_mod", TEST_VERSION)
    lines = out.splitlines()
    # Display name appears at the very top (second line, under the name header).
    assert lines[0].startswith("id_describe_mod")
    assert any(
        ln.startswith("├─ Display name: E-Invoice - Misa meInvoice Integrator")
        for ln in lines
    ), f"Display name header missing, got:\n{out}"
    # Author appears inside the Manifest sub-tree (indented under '│').
    assert any(
        "Author: T.V.T Marine Automation (aka TVTMA),Viindoo" in ln
        for ln in lines
    ), f"Author manifest row missing, got:\n{out}"


# --- H1: reclassified viindoo edition outranks 'custom' in the ORM tiebreak --


def test_resolve_field_reclassified_viindoo_outranks_custom(clean_neo4j):
    """H1 must-fix: with all higher ranking tiers equal, a module whose edition is
    'viindoo' (the post-reclassify state of an OPL-1 Viindoo addon) must win the
    edition tiebreak over a 'custom' module - even though 'custom' is
    alphabetically first.

    is_def_rank, field_count, dependents are made equal across both modules, so
    the edition tier (viindoo=2 < custom=4) is decisive. Module names are chosen
    so the alphabetical FINAL tiebreak would order them the OTHER way
    (aaa_custom < zzz_viin). This test LOCKS the rank-ordering consequence H1
    flagged: it is red if EDITION_PRIORITY changes so 'viindoo' no longer
    outranks 'custom' in the same-name tiebreak. The reclassify mapping itself
    (OPL-1 + Viindoo author -> 'viindoo') is covered separately by
    test_build_registry_parses_shortdesc_and_author.
    """
    with clean_neo4j.session() as session:
        session.run(
            """
            MERGE (c:Module {name: 'aaa_custom_mod', odoo_version: $v})
            SET c.edition = 'custom', c.profile = ['default'], c.repo = 'r_custom'
            MERGE (w:Module {name: 'zzz_viin_mod', odoo_version: $v})
            SET w.edition = 'viindoo', w.profile = ['default'], w.repo = 'r_viin'
            MERGE (f1:Field {name: 'x_dup', model: 'id.model',
                             module: 'aaa_custom_mod', odoo_version: $v})
            SET f1.ttype = 'char', f1.profile = ['default']
            MERGE (f2:Field {name: 'x_dup', model: 'id.model',
                             module: 'zzz_viin_mod', odoo_version: $v})
            SET f2.ttype = 'char', f2.profile = ['default']
            """,
            v=TEST_VERSION,
        )
    srv = _import_server()
    out = srv._resolve_field("id.model", "x_dup", TEST_VERSION)
    pos_viin = out.find("zzz_viin_mod")
    pos_custom = out.find("aaa_custom_mod")
    assert pos_viin != -1 and pos_custom != -1, f"both modules must list, got:\n{out}"
    assert pos_viin < pos_custom, (
        "viindoo (rank 2) must outrank custom (rank 4) in the edition tiebreak, "
        f"appearing before it in 'Declared in'. Got:\n{out}"
    )


# --- P1/M1: coverage superset-diff + caveat ---------------------------------


def _seed_coverage_module(driver, *, name, category, profile):
    with driver.session() as session:
        session.run(
            "MERGE (m:Module {name: $n, odoo_version: $v}) "
            "SET m.edition = 'viindoo', m.repo = 'r', m.category = $category, "
            "    m.profile = $profile",
            n=name, v=TEST_VERSION, category=category, profile=list(profile),
        )


def test_coverage_superset_diff_and_caveat(clean_neo4j):
    """method='coverage' shows in_profile vs indexed_elsewhere per category and the
    'absence != absence' caveat. Admin caller (own=None) so the whole version is
    in-scope: a category present elsewhere but absent here surfaces with
    in_profile=0, and one under-represented here flags [may be incomplete]."""
    P = "id_cov_p"
    OTHER = "id_cov_other"
    _seed_coverage_module(clean_neo4j, name="id_acc_a", category="Accounting", profile=[P])
    _seed_coverage_module(clean_neo4j, name="id_acc_b", category="Accounting", profile=[P])
    _seed_coverage_module(clean_neo4j, name="id_sale_c", category="Sales", profile=[P])
    # Visible to the admin caller but NOT in profile P:
    _seed_coverage_module(clean_neo4j, name="id_acc_d", category="Accounting", profile=[OTHER])
    _seed_coverage_module(clean_neo4j, name="id_inv_e", category="Inventory", profile=[OTHER])

    _import_server()  # ensure server re-imports against the test Neo4j env
    from src.mcp.inspect import _profile_inspect
    out = _profile_inspect(name=P, method="coverage", odoo_version=TEST_VERSION)

    # Accounting: 2 here, 1 elsewhere -> incomplete signal.
    assert "Accounting: in_profile=2, indexed_elsewhere=1  [may be incomplete]" in out, out
    # Sales: fully here.
    assert "Sales: in_profile=1, indexed_elsewhere=0" in out, out
    # Inventory: absent here but visible elsewhere -> in_profile=0 surfaced.
    assert "Inventory: in_profile=0, indexed_elsewhere=1  [may be incomplete]" in out, out
    # Caveat present, ASCII '!=' (M2), never the Unicode not-equal U+2260.
    assert "Absence from this list != absence from the product" in out
    assert "≠" not in out  # the banned Unicode not-equal sign must not ship
    assert "cross-check live ir.module.module" in out


def test_coverage_requires_name(clean_neo4j):
    from src.mcp.inspect import _profile_inspect
    _import_server()
    out = _profile_inspect(name=None, method="coverage", odoo_version=TEST_VERSION)
    assert out.startswith("Error:")
    assert "requires name" in out


def test_coverage_tenant_leak_guard(clean_neo4j):
    """ADR-0034: a scoped tenant must NOT see another profile's modules in the
    coverage counts. With own narrowed to ['id_cov_owned'], the foreign module's
    category must never appear (the choke is the same _scope_pred as summary)."""
    _seed_coverage_module(
        clean_neo4j, name="id_owned_mod", category="OwnedDomain", profile=["id_cov_owned"],
    )
    _seed_coverage_module(
        clean_neo4j, name="id_foreign_mod", category="SecretForeignDomain",
        profile=["id_cov_foreign"],
    )
    srv = _import_server()
    from unittest.mock import patch

    from src.mcp.inspect import _profile_inspect

    def _allow_owned(profile_name):
        allowed = ["id_cov_owned"]
        if profile_name is None:
            return allowed
        return [profile_name] if profile_name in allowed else []

    def _scope_owned(profile_name=None):
        return {"own": ["id_cov_owned"], "shared": []}

    with patch.object(srv, "_effective_allowed", side_effect=_allow_owned), \
         patch.object(srv, "_scope", side_effect=_scope_owned):
        out = _profile_inspect(name="id_cov_owned", method="coverage",
                               odoo_version=TEST_VERSION)

    assert "OwnedDomain" in out, f"own category must show, got:\n{out}"
    assert "SecretForeignDomain" not in out, (
        f"ADR-0034 leak: foreign profile category must NOT appear, got:\n{out}"
    )
