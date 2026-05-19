"""Integration tests for src/mcp/resources_index.py (WI-F2, M11 Wave F).

Covers AC-F2-1 through AC-F2-5:
  AC-F2-1: list_resources_index() exists and returns a list of dicts.
  AC-F2-2: Returns ≤100 entries per version (LIMIT enforced).
  AC-F2-3: Ordered by dep_count DESC within each version, name ASC tiebreak.
  AC-F2-4: Each entry has uri, mimeType, description, name (all str).
  AC-F2-5: ≥4 tests — count/ordering/empty/URI format.

DB isolation:
  - Neo4j version "RI_99.0" — unique prefix avoids collision with any other
    test suite.  Wiped before/after every test via `wipe_ri_neo4j` fixture.

Marker: pytest.mark.neo4j  (requires running Neo4j via testcontainers or
        docker compose neo4j).
"""

from __future__ import annotations

import re

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RI_VERSION = "99.5"   # Dedicated test version — no collision with real data (conftest uses 99.0)
_RI_VERSION_2 = "98.5"  # Second version for multi-version tests

pytestmark = pytest.mark.neo4j


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_module(session, module_name: str, version: str, depends: list[str] | None = None) -> None:
    """Seed a bare Module node with optional DEPENDS_ON edges."""
    session.run(
        "MERGE (m:Module {name: $name, odoo_version: $v})",
        name=module_name, v=version,
    )
    for dep in (depends or []):
        session.run(
            """
            MATCH (src:Module {name: $src, odoo_version: $v})
            MERGE (dep:Module {name: $dep, odoo_version: $v})
            MERGE (src)-[:DEPENDS_ON]->(dep)
            """,
            src=module_name, dep=dep, v=version,
        )


def _seed_model(
    session,
    model_name: str,
    module_name: str,
    version: str,
    is_definition: bool = True,
) -> None:
    """Seed a Model node with DEFINED_IN edge to its Module."""
    session.run(
        """
        MERGE (mod:Module {name: $module, odoo_version: $v})
        MERGE (m:Model {name: $model, module: $module, odoo_version: $v})
        ON CREATE SET m.is_definition = $is_def
        ON MATCH  SET m.is_definition = $is_def
        MERGE (m)-[:DEFINED_IN]->(mod)
        """,
        model=model_name, module=module_name, v=version, is_def=is_definition,
    )


def _wipe_version(session, version: str) -> None:
    session.run("MATCH (n) WHERE n.odoo_version = $v DETACH DELETE n", v=version)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def wipe_ri_neo4j(neo4j_driver):
    """Wipe RI_99.0 and RI_98.0 test data before and after each test."""
    with neo4j_driver.session() as s:
        _wipe_version(s, _RI_VERSION)
        _wipe_version(s, _RI_VERSION_2)
    yield neo4j_driver
    with neo4j_driver.session() as s:
        _wipe_version(s, _RI_VERSION)
        _wipe_version(s, _RI_VERSION_2)


@pytest.fixture()
def ri_driver(wipe_ri_neo4j):
    """Return the neo4j_driver after cleaning RI test versions."""
    return wipe_ri_neo4j


@pytest.fixture()
def patched_get_driver(ri_driver):
    """Patch src.mcp.server._get_driver to return the test driver."""
    from unittest.mock import patch as _patch

    with _patch("src.mcp.server._get_driver", return_value=ri_driver):
        yield ri_driver


# ---------------------------------------------------------------------------
# Utility — import list_resources_index lazily so patching works
# ---------------------------------------------------------------------------


def _list_resources_index():
    from src.mcp.resources_index import list_resources_index
    return list_resources_index()


# ===========================================================================
# Test 1: Empty corpus → empty list
# ===========================================================================

def test_empty_corpus_returns_empty_list(patched_get_driver):
    """AC-F2-5: no seeded data → list_resources_index() returns []."""
    result = _list_resources_index()
    assert result == [], f"Expected empty list for empty corpus, got {result!r}"


# ===========================================================================
# Test 2: Fixture corpus — expected count
# ===========================================================================

def test_returns_expected_count_for_fixture_corpus(patched_get_driver, ri_driver):
    """AC-F2-1 + AC-F2-5: seeded corpus returns correct number of entries."""
    # Seed 3 models for one version and 2 for another.
    with ri_driver.session() as s:
        # Version RI_99.0 — 3 modules/models
        _seed_module(s, "sale", _RI_VERSION)
        _seed_module(s, "account", _RI_VERSION, depends=["sale"])
        _seed_module(s, "stock", _RI_VERSION, depends=["sale"])

        _seed_model(s, "sale.order", "sale", _RI_VERSION)
        _seed_model(s, "account.move", "account", _RI_VERSION)
        _seed_model(s, "stock.move", "stock", _RI_VERSION)

        # Version RI_98.0 — 2 modules/models
        _seed_module(s, "purchase", _RI_VERSION_2)
        _seed_module(s, "mrp", _RI_VERSION_2, depends=["purchase"])

        _seed_model(s, "purchase.order", "purchase", _RI_VERSION_2)
        _seed_model(s, "mrp.production", "mrp", _RI_VERSION_2)

    result = _list_resources_index()

    # 3 from RI_99.0 + 2 from RI_98.0 = 5 total
    assert len(result) == 5, (
        f"Expected 5 entries (3+2 across versions), got {len(result)}: {result}"
    )


# ===========================================================================
# Test 3: Ordering — dep_count DESC, name ASC tiebreak
# ===========================================================================

def test_ordering_dep_count_desc_then_name_asc(patched_get_driver, ri_driver):
    """AC-F2-3: entries ordered by dependency count descending, name ascending.

    Setup:
      - base module (0 dependents) defines base.model
      - popular module (2 dependents from module_a, module_b) defines popular.model
      - mid module (1 dependent from module_c) defines mid.model

    Expected order: popular.model (2), mid.model (1), base.model (0).
    Within same dep_count: name ASC alphabetical.
    """
    with ri_driver.session() as s:
        _seed_module(s, "base", _RI_VERSION)
        _seed_module(s, "popular", _RI_VERSION)
        _seed_module(s, "mid", _RI_VERSION)
        _seed_module(s, "module_a", _RI_VERSION, depends=["popular"])
        _seed_module(s, "module_b", _RI_VERSION, depends=["popular"])
        _seed_module(s, "module_c", _RI_VERSION, depends=["mid"])

        _seed_model(s, "base.model", "base", _RI_VERSION)
        _seed_model(s, "popular.model", "popular", _RI_VERSION)
        _seed_model(s, "mid.model", "mid", _RI_VERSION)

    result = _list_resources_index()

    # Filter to only RI_99.0 entries (in case other versions exist)
    ri_entries = [e for e in result if f"/{_RI_VERSION}/" in e["uri"]]

    assert len(ri_entries) == 3, f"Expected 3 RI_99.0 entries, got {ri_entries}"

    names = [e["name"] for e in ri_entries]
    assert names == ["popular.model", "mid.model", "base.model"], (
        f"Ordering wrong — expected popular→mid→base, got {names}"
    )


def test_name_asc_tiebreak_within_same_dep_count(patched_get_driver, ri_driver):
    """AC-F2-3: when dep_count is equal, model names are sorted ASC."""
    with ri_driver.session() as s:
        # two modules with equal (zero) dependents
        _seed_module(s, "zebra_mod", _RI_VERSION)
        _seed_module(s, "alpha_mod", _RI_VERSION)

        _seed_model(s, "zebra.model", "zebra_mod", _RI_VERSION)
        _seed_model(s, "alpha.model", "alpha_mod", _RI_VERSION)

    result = _list_resources_index()
    ri_entries = [e for e in result if f"/{_RI_VERSION}/" in e["uri"]]

    names = [e["name"] for e in ri_entries]
    assert names == sorted(names), (
        f"Expected alphabetical order for equal dep_count, got {names}"
    )
    assert "alpha.model" in names and "zebra.model" in names


# ===========================================================================
# Test 4: URI format validation
# ===========================================================================

_URI_PATTERN = re.compile(r"^odoo://[\d]+\.[\d]+/model/[a-z][a-z0-9_.]*$")


def test_uri_format_matches_scheme(patched_get_driver, ri_driver):
    """AC-F2-4 + AC-F2-5: every uri matches odoo://<version>/model/<name>."""
    with ri_driver.session() as s:
        _seed_module(s, "sale", _RI_VERSION)
        _seed_model(s, "sale.order", "sale", _RI_VERSION)

    result = _list_resources_index()
    assert result, "Expected at least one entry"

    ri_entries = [e for e in result if f"/{_RI_VERSION}/" in e["uri"]]
    assert ri_entries, "Expected at least one RI_99.0 entry"

    for entry in ri_entries:
        uri = entry["uri"]
        assert _URI_PATTERN.match(uri), (
            f"URI '{uri}' does not match pattern 'odoo://<version>/model/<name>'"
        )
        # Verify the version and model name are encoded correctly
        assert _RI_VERSION in uri, f"Version missing from URI: {uri}"
        assert "sale.order" in uri, f"Model name missing from URI: {uri}"


# ===========================================================================
# Test 5: Entry shape validation (AC-F2-4)
# ===========================================================================

def test_each_entry_has_required_fields(patched_get_driver, ri_driver):
    """AC-F2-4: each entry has uri, mimeType, name, description — all strings."""
    with ri_driver.session() as s:
        _seed_module(s, "product", _RI_VERSION)
        _seed_model(s, "product.template", "product", _RI_VERSION)

    result = _list_resources_index()
    assert result, "Expected at least one entry"

    for entry in result:
        assert isinstance(entry, dict), f"Entry is not a dict: {entry!r}"
        for key in ("uri", "mimeType", "name", "description"):
            assert key in entry, f"Missing key '{key}' in entry: {entry!r}"
            assert isinstance(entry[key], str), (
                f"Key '{key}' should be str, got {type(entry[key])!r}: {entry!r}"
            )
        assert entry["mimeType"] == "text/markdown", (
            f"Expected mimeType='text/markdown', got {entry['mimeType']!r}"
        )


# ===========================================================================
# Test 6: LIMIT 100 per version (AC-F2-2)
# ===========================================================================

def test_limit_100_per_version_enforced(patched_get_driver, ri_driver):
    """AC-F2-2: even with >100 models seeded, at most 100 returned per version."""
    with ri_driver.session() as s:
        # Seed one module + 110 distinct models
        _seed_module(s, "big_mod", _RI_VERSION)
        for i in range(110):
            _seed_model(s, f"big.model.{i:03d}", "big_mod", _RI_VERSION)

    result = _list_resources_index()
    ri_entries = [e for e in result if f"/{_RI_VERSION}/" in e["uri"]]

    assert len(ri_entries) <= 100, (
        f"Expected at most 100 entries for RI_99.0, got {len(ri_entries)}"
    )
    # Sanity: we seeded 110 but should get exactly 100
    assert len(ri_entries) == 100, (
        f"Expected exactly 100 entries (LIMIT 100), got {len(ri_entries)}"
    )
