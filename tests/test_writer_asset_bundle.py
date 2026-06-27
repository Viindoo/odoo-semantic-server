# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_writer_asset_bundle.py
"""WI-D — :AssetBundle graph writer (ADR-0052). neo4j-MARKED (CI-only).

Behaviour contract:
  (i)   write_asset_results creates :AssetBundle nodes (composite key
        name+odoo_version) with CONTRIBUTES_TO (Module -> AssetBundle, carrying
        entries) and INCLUDES_BUNDLE (AssetBundle -> AssetBundle) edges.
  (ii)  a legacy v15+ <template inherit_id="web.assets_backend"> extender resolves
        against the AssetBundle via EXTENDS_ASSET_BUNDLE — NO unresolved warning,
        NO __unresolved__ placeholder (the ~13 A2 warnings are removed by indexing
        the bundle, not by downgrading the log).

Do NOT run locally (dev box has live neo4j; clean_neo4j DROPs data) — CI only.
"""
import json
import os

import pytest

from src.indexer.models import (
    AssetBundleContribution,
    AssetParseResult,
    ModuleInfo,
    QWebInfo,
    ViewParseResult,
)
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j


@pytest.fixture
def writer(clean_neo4j, neo4j_driver):
    w = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    w.setup_indexes()
    yield w
    w.close()


def _mod(name: str) -> ModuleInfo:
    return ModuleInfo(
        name=name, odoo_version=TEST_VERSION, repo="test_repo",
        path="/tmp", depends=[], version_raw="",
    )


def _contrib(module: str, bundle: str, entries, includes=None) -> AssetBundleContribution:
    return AssetBundleContribution(
        module=module, odoo_version=TEST_VERSION, bundle_name=bundle,
        entries=entries, includes=includes or [],
    )


def _count(driver, cypher: str, **params) -> int:
    with driver.session() as s:
        row = s.run(cypher, v=TEST_VERSION, **params).single()
    return row["n"] if row else 0


def test_asset_bundle_node_and_contributes_to_created(writer, clean_neo4j):
    """AssetBundle node + CONTRIBUTES_TO edge with entries JSON are written."""
    driver = clean_neo4j
    res = AssetParseResult(module=_mod("web"), contributions=[
        _contrib("web", "web.assets_backend", ["web/static/src/app.js"]),
    ])
    writer.write_asset_results([res], profiles=["test_repo"])

    assert _count(
        driver,
        "MATCH (b:AssetBundle {name:'web.assets_backend', odoo_version:$v})"
        " RETURN count(b) AS n",
    ) == 1

    with driver.session() as s:
        row = s.run(
            "MATCH (m:Module {name:'web', odoo_version:$v})"
            "-[r:CONTRIBUTES_TO]->(b:AssetBundle {name:'web.assets_backend', odoo_version:$v})"
            " RETURN r.entries AS entries, b.is_private AS priv, b.module AS definer",
            v=TEST_VERSION,
        ).single()
    assert row is not None
    assert json.loads(row["entries"]) == ["web/static/src/app.js"]
    assert row["priv"] is False
    assert row["definer"] == "web"


def test_private_bundle_flagged(writer, clean_neo4j):
    driver = clean_neo4j
    res = AssetParseResult(module=_mod("web"), contributions=[
        _contrib("web", "web._assets_helpers", ["web/static/src/h.scss"]),
    ])
    writer.write_asset_results([res], profiles=["test_repo"])
    with driver.session() as s:
        row = s.run(
            "MATCH (b:AssetBundle {name:'web._assets_helpers', odoo_version:$v})"
            " RETURN b.is_private AS priv", v=TEST_VERSION,
        ).single()
    assert row["priv"] is True


def test_includes_bundle_edge_created(writer, clean_neo4j):
    """('include', 'web._assets_helpers') yields AssetBundle -> AssetBundle edge."""
    driver = clean_neo4j
    res = AssetParseResult(module=_mod("web"), contributions=[
        _contrib("web", "web.assets_common",
                 [["include", "web._assets_helpers"]],
                 includes=["web._assets_helpers"]),
    ])
    writer.write_asset_results([res], profiles=["test_repo"])
    assert _count(
        driver,
        "MATCH (:AssetBundle {name:'web.assets_common', odoo_version:$v})"
        "-[:INCLUDES_BUNDLE]->(:AssetBundle {name:'web._assets_helpers', odoo_version:$v})"
        " RETURN count(*) AS n",
    ) == 1


def test_legacy_extender_resolves_via_extends_asset_bundle(writer, clean_neo4j):
    """The headline fix: a v15+ legacy <template inherit_id='web.assets_backend'>
    extender (a QWebInfo with inherit_xmlid) resolves to the AssetBundle via
    EXTENDS_ASSET_BUNDLE — no unresolved placeholder, no warning."""
    driver = clean_neo4j

    # 1) write the AssetBundle base FIRST (pipeline ordering: assets before views).
    base = AssetParseResult(module=_mod("web"), contributions=[
        _contrib("web", "web.assets_backend", ["web/static/src/app.js"]),
    ])
    writer.write_asset_results([base], profiles=["test_repo"])

    # 2) write the legacy XML extender as a QWebTmpl with inherit_xmlid -> bundle.
    extender = ViewParseResult(module=_mod("crm"), qweb=[
        QWebInfo(
            xmlid="crm.assets_backend", module="crm",
            odoo_version=TEST_VERSION, inherit_xmlid="web.assets_backend",
        ),
    ])
    writer.write_view_results([extender], profiles=["test_repo"])

    # EXTENDS_ASSET_BUNDLE edge exists from the extender QWebTmpl to the bundle.
    assert _count(
        driver,
        "MATCH (t:QWebTmpl {xmlid:'crm.assets_backend', odoo_version:$v})"
        "-[:EXTENDS_ASSET_BUNDLE]->(b:AssetBundle {name:'web.assets_backend', odoo_version:$v})"
        " RETURN count(*) AS n",
    ) == 1

    # No __unresolved__ placeholder QWebTmpl was created for the bundle name.
    assert _count(
        driver,
        "MATCH (t:QWebTmpl {xmlid:'web.assets_backend', odoo_version:$v})"
        " RETURN count(t) AS n",
    ) == 0


def test_multiple_modules_contribute_definer_is_first(writer, clean_neo4j):
    """Two modules contributing the same bundle: both get CONTRIBUTES_TO; the
    first writer owns the `module` (definer) prop, later writers don't overwrite."""
    driver = clean_neo4j
    writer.write_asset_results([
        AssetParseResult(module=_mod("web"), contributions=[
            _contrib("web", "web.assets_backend", ["web/static/src/a.js"]),
        ]),
    ], profiles=["test_repo"])
    writer.write_asset_results([
        AssetParseResult(module=_mod("crm"), contributions=[
            _contrib("crm", "web.assets_backend", ["crm/static/src/b.js"]),
        ]),
    ], profiles=["test_repo"])

    assert _count(
        driver,
        "MATCH (:Module)-[:CONTRIBUTES_TO]->"
        "(b:AssetBundle {name:'web.assets_backend', odoo_version:$v})"
        " RETURN count(*) AS n",
    ) == 2
    with driver.session() as s:
        row = s.run(
            "MATCH (b:AssetBundle {name:'web.assets_backend', odoo_version:$v})"
            " RETURN b.module AS definer", v=TEST_VERSION,
        ).single()
    assert row["definer"] == "web"  # first writer owns the definer
