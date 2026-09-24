# SPDX-License-Identifier: AGPL-3.0-or-later
"""INHERITS_TEST on REAL Odoo 17.0 test sources, through the real index run
(F11/L4 + its follow-ups, F46).

The test files are copied verbatim from the Odoo 17.0 checkout
(``tests/_odoo_checkouts.checkout_root(17)``; the test skips without it) into a
temp git repo, each module with a manifest carrying its REAL ``depends`` list
read from the checkout, then ``index_profile`` (``_index_repo`` + the
version-wide post-passes) indexes it at TEST_VERSION. Expected edges are read
off the real ``import`` statements of those files (Python resolves a base by
import, not by manifest):

* ``l10n_dk_oioubl/tests/test_xml_oioubl_dk.py``
  ``from odoo.addons.l10n_account_edi_ubl_cii_tests.tests.common import TestUBLCommon``
  and ``from odoo.addons.account.tests.test_account_move_send import
  TestAccountMoveSendCommon`` -> ``class TestUBLDK(TestUBLCommon,
  TestAccountMoveSendCommon)``. l10n_dk_oioubl's manifest (account_edi_ubl_cii,
  l10n_dk) does NOT depend on l10n_account_edi_ubl_cii_tests: the helper is
  linked by its import. A same-named ``TestUBLCommon`` placed in
  account_edi_ubl_cii (a dependency; SYNTHETIC decoy, not in 17.0) must not win
  over the imported one.
* ``stock/tests/test_product.py`` imports ``TestStockCommon`` from
  ``stock.tests.common2`` while ``stock/tests/test_move_lines.py`` imports it
  from ``stock.tests.common`` - two same-named classes of ONE module; each child
  gets the one its file imports.
* ``sale/tests/common.py`` ``TestSaleCommon(AccountTestInvoicingCommon,
  TestSaleCommonBase)`` - the second base is defined in the same file.

Needs Neo4j + PostgreSQL with pgvector (the run embeds with a FakeEmbedder).
"""
from __future__ import annotations

import ast
import shutil
from pathlib import Path

import pytest

from src.db.migrate import _vector_extension_available, run_migrations
from tests._lifecycle_repo import GitRepo, V, register, run
from tests._odoo_checkouts import checkout_root

pytestmark = [pytest.mark.postgres, pytest.mark.neo4j]

# module -> real test files copied from the 17.0 checkout
_REAL_TEST_FILES: dict[str, list[str]] = {
    "account": ["tests/common.py", "tests/test_account_move_send.py"],
    "account_payment": [],
    "account_edi_ubl_cii": [],
    "l10n_account_edi_ubl_cii_tests": ["tests/common.py"],
    "l10n_dk": [],
    "l10n_dk_oioubl": ["tests/test_xml_oioubl_dk.py"],
    "product": ["tests/common.py"],
    "stock": ["tests/common.py", "tests/common2.py", "tests/test_product.py",
              "tests/test_move_lines.py"],
    "sale": ["tests/common.py", "tests/test_sale_refund.py"],
}

# SYNTHETIC decoy (not in 17.0): a same-named helper in a module l10n_dk_oioubl
# DOES depend on. Import evidence must beat dependency proximity.
_DECOY_UBL_COMMON = '''\
from odoo.addons.account.tests.common import AccountTestInvoicingCommon


class TestUBLCommon(AccountTestInvoicingCommon):
    """Decoy: same name as l10n_account_edi_ubl_cii_tests' helper."""
'''


@pytest.fixture
def pg(clean_pg, clean_neo4j):
    run_migrations(clean_pg)
    if not _vector_extension_available(clean_pg):
        pytest.skip("pgvector extension not installed - the index run cannot embed")
    return clean_pg


@pytest.fixture
def odoo17() -> Path:
    root = checkout_root(17)
    if root is None or not (root / "addons" / "l10n_dk_oioubl").is_dir():
        pytest.skip("Odoo 17.0 checkout not on disk (tests/_odoo_checkouts.checkout_root)")
    return root / "addons"


def _real_depends(addons: Path, module: str) -> list[str]:
    manifest = ast.literal_eval((addons / module / "__manifest__.py").read_text())
    return list(manifest.get("depends", []))


def _odoo17_repo(parent: Path, addons: Path) -> GitRepo:
    repo = GitRepo(parent, "odoo17_tests")
    for module, files in _REAL_TEST_FILES.items():
        mod = repo.path / module
        mod.mkdir()
        (mod / "__manifest__.py").write_text(repr({
            "name": module, "version": f"{V}.1.0", "installable": True,
            "license": "LGPL-3", "depends": _real_depends(addons, module),
        }) + "\n")
        (mod / "__init__.py").write_text("")
        if files:
            (mod / "tests").mkdir()
            (mod / "tests" / "__init__.py").write_text("".join(
                f"from . import {Path(f).stem}\n" for f in files))
        for f in files:
            shutil.copy(addons / module / f, mod / f)
    decoy = repo.path / "account_edi_ubl_cii" / "tests"
    decoy.mkdir()
    (decoy / "__init__.py").write_text("from . import common\n")
    (decoy / "common.py").write_text(_DECOY_UBL_COMMON)
    repo.commit("Odoo 17.0 test sources (verbatim) + manifests with real depends")
    return repo


def _targets(driver, child: str, module: str) -> set[tuple]:
    with driver.session() as s:
        rows = s.run(
            """
            MATCH (c:TestClass {name: $c, module: $m, odoo_version: $v})
                  -[:INHERITS_TEST]->(t)
            RETURN DISTINCT labels(t)[0] AS label, t.module AS module, t.name AS name,
                   CASE WHEN t:TestClass THEN t.file_path END AS fp
            """,
            c=child, m=module, v=V,
        ).data()
    return {(r["label"], r["module"], r["name"], r["fp"]) for r in rows}


def _snapshot(driver) -> tuple[set, set, set]:
    """Every INHERITS_TEST edge, TestHelper node and helper flag of the version."""
    with driver.session() as s:
        edges = {tuple(r.values()) for r in s.run(
            """
            MATCH (c)-[:INHERITS_TEST]->(t) WHERE c.odoo_version = $v
            RETURN labels(c)[0], c.module, c.name, coalesce(c.file_path, ''),
                   labels(t)[0], t.module, t.name, coalesce(t.file_path, '')
            """, v=V)}
        helpers = {(r["m"], r["n"], tuple(r["p"] or [])) for r in s.run(
            "MATCH (h:TestHelper {odoo_version: $v}) "
            "RETURN h.module AS m, h.name AS n, h.profile AS p", v=V)}
        flags = {(r["m"], r["n"], r["f"], r["h"]) for r in s.run(
            "MATCH (c:TestClass {odoo_version: $v}) "
            "RETURN c.module AS m, c.name AS n, c.file_path AS f, "
            "coalesce(c.is_helper, false) AS h", v=V)}
    return edges, helpers, flags


def _tc(module: str, name: str, fp: str) -> tuple:
    return ("TestClass", module, name, fp)


def _th(module: str, name: str) -> tuple:
    return ("TestHelper", module, name, None)


def test_real_17_test_bases_resolve_to_what_each_file_imports_after_one_run(
    pg, neo4j_driver, tmp_path, odoo17,
):
    """F11/L4 + follow-ups (FIX): after ONE index run every child in the real
    17.0 files is linked to exactly the base its import names - across modules
    outside the manifest closure (l10n_dk_oioubl -> l10n_account_edi_ubl_cii_tests),
    between two same-named classes of one module (stock common vs common2), to a
    base defined in the child's own file (sale TestSaleCommonBase) - and, per
    F46, to the TestHelper projection of each of those helpers as well."""
    repo = _odoo17_repo(tmp_path, odoo17)
    register("tsurf_odoo17", repo)
    run(pg, "tsurf_odoo17")

    assert _targets(neo4j_driver, "TestUBLDK", "l10n_dk_oioubl") == {
        _tc("l10n_account_edi_ubl_cii_tests", "TestUBLCommon",
            "l10n_account_edi_ubl_cii_tests/tests/common.py"),
        _th("l10n_account_edi_ubl_cii_tests", "TestUBLCommon"),
        _tc("account", "TestAccountMoveSendCommon", "account/tests/test_account_move_send.py"),
        _th("account", "TestAccountMoveSendCommon"),
    }
    assert _targets(neo4j_driver, "TestVirtualAvailable", "stock") == {
        _tc("stock", "TestStockCommon", "stock/tests/common2.py"),
        _th("stock", "TestStockCommon"),
    }
    assert _targets(neo4j_driver, "StockMoveLine", "stock") == {
        _tc("stock", "TestStockCommon", "stock/tests/common.py"),
        _th("stock", "TestStockCommon"),
    }
    assert _targets(neo4j_driver, "TestSaleCommon", "sale") == {
        _tc("account", "AccountTestInvoicingCommon", "account/tests/common.py"),
        _th("account", "AccountTestInvoicingCommon"),
        _tc("sale", "TestSaleCommonBase", "sale/tests/common.py"),
        _th("sale", "TestSaleCommonBase"),
    }
    assert _targets(neo4j_driver, "TestSaleRefund", "sale") == {
        _tc("sale", "TestSaleCommon", "sale/tests/common.py"),
        _th("sale", "TestSaleCommon"),
    }
    # The decoy has no subclass: nothing resolved to it.
    with neo4j_driver.session() as s:
        decoy_children = s.run(
            "MATCH (c)-[:INHERITS_TEST]->(:TestClass {name: 'TestUBLCommon', "
            "module: 'account_edi_ubl_cii', odoo_version: $v}) RETURN count(c) AS n", v=V,
        ).single()["n"]
    assert decoy_children == 0


def test_a_second_run_over_unchanged_real_sources_changes_nothing(
    pg, neo4j_driver, tmp_path, odoo17,
):
    """F46 (FIX): a nightly run with nothing new leaves every INHERITS_TEST
    edge, every TestHelper projection (with its profile) and every helper flag
    exactly as the first run left them."""
    repo = _odoo17_repo(tmp_path, odoo17)
    register("tsurf_odoo17", repo)
    run(pg, "tsurf_odoo17")
    first = _snapshot(neo4j_driver)
    assert first[0] and first[1], "positive control: the first run built edges and helpers"

    run(pg, "tsurf_odoo17")

    assert _snapshot(neo4j_driver) == first
