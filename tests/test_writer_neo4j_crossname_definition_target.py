# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_writer_neo4j_crossname_definition_target.py
#
# Invariant: cross-name INHERITS (`_inherit = ['other.model']`) and DELEGATES_TO
# (`_inherits = {'other.model': 'field'}`) writers MUST target the single
# definition node of the parent/delegated model, NOT every same-name copy.
#
# Root cause (osm-warn-single.md sites #20, #21): under the C1 schema (ADR-0013)
# each module that defines or extends a model gets its OWN Model node with the
# same `.name`. The old cross-name writers matched the parent by `{name, version}`
# only, guarded merely by `WHERE NOT coalesce(parent.unresolved, false)`. In a
# same-name mesh of K copies of a commonly-extended model (account.move, res.partner),
# this matched ALL K copies, so:
#   1. The MERGE fanned out and created K spurious INHERITS/DELEGATES_TO edges
#      from the child to every copy (correctness bug — violates the K×D rule that
#      the self-extend path already enforces with `is_definition=true`).
#   2. `.single()` saw K > 1 rows and emitted
#      `UserWarning: Expected a result with a single record, but found multiple`.
#
# Fix (WI-A): add `AND coalesce(parent.is_definition, false) = true` (resp. for
# `d`) to the parent MATCH WHERE clause, mirroring the self-extend path.
#
# Fix (graph HIGH-1 review): the is_definition filter alone does NOT guarantee a
# single row. ADR-0048 explicitly accepts D>1 — a fork + its upstream can each
# declare `_name='x'` with an explicit name and no self-inherit, producing TWO
# is_definition=true nodes for the same (name, version). Calling `.single()` over
# that D=2 set re-emits the very "Expected a single record, found multiple"
# UserWarning this wave eliminates, and fans out K×D edges. So the writer now
# collapses to ONE deterministic target before MERGE
# (`ORDER BY coalesce(parent.field_count, 0) DESC, parent.module ASC LIMIT 1`),
# mirroring the REPORTS_ON / PATCHES fix in writer_neo4j_ui.py. Since field_count
# is not a stored Model property (always coalesces to 0), the effective tiebreak
# is `module ASC` → the alphabetically-first definition module wins.
#
# These tests build a real same-name mesh (definitions + extenders) and assert
# exactly ONE edge to the deterministically-chosen DEFINITION node, including the
# D=2 two-definition case that the is_definition filter alone does not cover.
#
# NOTE: neo4j-marked — runs in CI / against a throwaway container ONLY. Do NOT run
# locally with the default DSN (clean_neo4j drops the TEST_VERSION namespace).

import os
import warnings

import pytest

from src.indexer.models import FieldInfo, ModelInfo, ModuleInfo, ParseResult
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j


@pytest.fixture
def writer(clean_neo4j, neo4j_driver):
    """Neo4jWriter connected to the test Neo4j instance."""
    w = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    w.setup_indexes()
    yield w
    w.close()


def _result(
    module_name: str,
    model_name: str,
    *,
    inherit: list[str] | None = None,
    inherits: dict[str, str] | None = None,
    had_explicit_name: bool = False,
) -> ParseResult:
    """Build a minimal ParseResult.

    The writer sets ``is_definition = had_explicit_name AND name NOT IN inherit``.
    A *definition* node => ``had_explicit_name=True`` and the model name is NOT in
    its own ``inherit`` list. An *extender* same-name copy => ``inherit=[name]``
    (self-inherit) so ``is_definition`` stays False.
    """
    module = ModuleInfo(
        name=module_name, odoo_version=TEST_VERSION,
        repo=f"{module_name}_repo", path="/tmp",
        depends=[], version_raw="",
    )
    model = ModelInfo(
        name=model_name, module=module_name, odoo_version=TEST_VERSION,
        fields=[FieldInfo(name="id", ttype="integer")],
        methods=[],
        inherit=inherit or [],
        inherits=inherits or {},
        had_explicit_name=had_explicit_name,
    )
    return ParseResult(module=module, models=[model])


def _build_parent_mesh(writer, parent_name: str, k: int = 4) -> list[str]:
    """Index a same-name mesh of *k* copies of *parent_name*: one definition
    (is_definition=true) + (k-1) self-extenders (is_definition=false).

    Returns the list of extender module names (the non-definition copies) so the
    caller can assert no edge points at them.
    """
    # Copy 0 = canonical definition.
    writer.write_results([_result("base", parent_name, had_explicit_name=True)])
    extender_modules = []
    for i in range(1, k):
        mod = f"ext_{parent_name.replace('.', '_')}_{i}"
        extender_modules.append(mod)
        writer.write_results([_result(mod, parent_name, inherit=[parent_name])])
    return extender_modules


def _build_two_definition_mesh(
    writer, parent_name: str, mod_a: str, mod_b: str,
) -> None:
    """Index TWO is_definition=true copies of *parent_name* (the ADR-0048 D>1
    case: a fork + its upstream both declare `_name=parent_name` with an explicit
    name and no self-inherit). Both get is_definition=true, so the is_definition
    filter alone returns BOTH rows — exactly the multi-row case the deterministic
    LIMIT-1 collapse must handle.
    """
    writer.write_results([_result(mod_a, parent_name, had_explicit_name=True)])
    writer.write_results([_result(mod_b, parent_name, had_explicit_name=True)])


# ---------------------------------------------------------------------------
# Test 1: cross-name INHERITS targets the definition node only (1 edge, not K)
# ---------------------------------------------------------------------------

def test_crossname_inherits_targets_only_definition_node(writer, neo4j_driver):
    """A child model whose ``_inherit`` names a commonly-extended parent must get
    exactly ONE INHERITS edge — to the parent's definition node — even when K
    same-name copies of the parent exist.

    Red-before-green: without the ``is_definition=true`` guard the writer matched
    all K copies and created K edges (and `.single()` warned). With the guard,
    exactly one edge to the definition node.
    """
    extenders = _build_parent_mesh(writer, "account.move", k=5)

    # Child model in a DIFFERENT-named model declares _inherit on the parent.
    writer.write_results([
        _result("sale", "sale.order", inherit=["account.move"]),
    ])

    with neo4j_driver.session() as session:
        rows = session.run(
            """
            MATCH (child:Model {name: 'sale.order', module: 'sale',
                                odoo_version: $v})
                  -[:INHERITS]->
                  (parent:Model {name: 'account.move', odoo_version: $v})
            RETURN parent.module AS parent_mod,
                   coalesce(parent.is_definition, false) AS is_def
            """,
            v=TEST_VERSION,
        ).data()

    assert len(rows) == 1, (
        f"Expected exactly 1 INHERITS edge to the definition node, got "
        f"{len(rows)}: {rows}. Without the is_definition guard the writer fans "
        f"out to all {len(extenders) + 1} same-name copies of account.move."
    )
    assert rows[0]["is_def"] is True, (
        f"INHERITS edge must target the definition node, got module "
        f"{rows[0]['parent_mod']!r} with is_definition={rows[0]['is_def']}."
    )
    assert rows[0]["parent_mod"] == "base", (
        f"Edge should point at the 'base' definition copy, not an extender: "
        f"{rows[0]['parent_mod']!r}"
    )

    # No edge may point at any extender (non-definition) copy.
    with neo4j_driver.session() as session:
        bad = session.run(
            """
            MATCH (child:Model {name: 'sale.order', module: 'sale',
                                odoo_version: $v})
                  -[:INHERITS]->
                  (parent:Model {name: 'account.move', odoo_version: $v})
            WHERE NOT coalesce(parent.is_definition, false)
            RETURN count(parent) AS spurious
            """,
            v=TEST_VERSION,
        ).single()
    assert bad["spurious"] == 0, (
        f"Found {bad['spurious']} spurious INHERITS edge(s) to non-definition "
        "same-name copies of account.move."
    )


# ---------------------------------------------------------------------------
# Test 2: cross-name INHERITS write emits no `.single()` multi-row warning
# ---------------------------------------------------------------------------

def test_crossname_inherits_emits_no_single_multirow_warning(writer, neo4j_driver):
    """Writing the cross-name INHERITS against a K-copy mesh must NOT emit the
    neo4j ``Expected a result with a single record, but found multiple``
    UserWarning — the MATCH now returns 0-or-1 rows.
    """
    _build_parent_mesh(writer, "res.partner", k=4)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        writer.write_results([
            _result("crm", "crm.lead", inherit=["res.partner"]),
        ])

    multi_row = [
        w for w in caught
        if "single record" in str(w.message)
        or "found multiple" in str(w.message)
    ]
    assert multi_row == [], (
        "cross-name INHERITS writer emitted a .single() multi-row warning: "
        f"{[str(w.message) for w in multi_row]}"
    )


# ---------------------------------------------------------------------------
# Test 3: DELEGATES_TO targets the definition node only (1 edge, not K)
# ---------------------------------------------------------------------------

def test_delegates_to_targets_only_definition_node(writer, neo4j_driver):
    """A model with ``_inherits = {'res.partner': 'partner_id'}`` must get
    exactly ONE DELEGATES_TO edge — to the definition node of res.partner —
    even when K same-name copies of res.partner exist.
    """
    _build_parent_mesh(writer, "res.partner", k=5)

    writer.write_results([
        _result("base", "res.users", inherits={"res.partner": "partner_id"}),
    ])

    with neo4j_driver.session() as session:
        rows = session.run(
            """
            MATCH (m:Model {name: 'res.users', module: 'base',
                            odoo_version: $v})
                  -[:DELEGATES_TO]->
                  (d:Model {name: 'res.partner', odoo_version: $v})
            RETURN d.module AS d_mod,
                   coalesce(d.is_definition, false) AS is_def
            """,
            v=TEST_VERSION,
        ).data()

    assert len(rows) == 1, (
        f"Expected exactly 1 DELEGATES_TO edge to the definition node, got "
        f"{len(rows)}: {rows}. Without the is_definition guard the writer fans "
        "out to every same-name copy of res.partner."
    )
    assert rows[0]["is_def"] is True, (
        f"DELEGATES_TO edge must target the definition node, got module "
        f"{rows[0]['d_mod']!r} with is_definition={rows[0]['is_def']}."
    )
    assert rows[0]["d_mod"] == "base"


# ---------------------------------------------------------------------------
# Test 4: DELEGATES_TO write emits no `.single()` multi-row warning
# ---------------------------------------------------------------------------

def test_delegates_to_emits_no_single_multirow_warning(writer, neo4j_driver):
    """Writing DELEGATES_TO against a K-copy mesh must NOT emit the neo4j
    multi-row UserWarning."""
    _build_parent_mesh(writer, "res.partner", k=4)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        writer.write_results([
            _result("hr", "hr.employee",
                    inherits={"res.partner": "partner_id"}),
        ])

    multi_row = [
        w for w in caught
        if "single record" in str(w.message)
        or "found multiple" in str(w.message)
    ]
    assert multi_row == [], (
        "DELEGATES_TO writer emitted a .single() multi-row warning: "
        f"{[str(w.message) for w in multi_row]}"
    )


# ---------------------------------------------------------------------------
# Test 5 (graph HIGH-1): D=2 — TWO is_definition=true copies. The is_definition
# filter alone returns both rows; the deterministic LIMIT-1 collapse must pick
# exactly ONE (alphabetically-first module) and emit no multi-row warning.
# ---------------------------------------------------------------------------

def test_crossname_inherits_d2_collapses_to_one_definition_no_warning(
    writer, neo4j_driver,
):
    """ADR-0048 D>1: when a fork + upstream both declare `_name='account.move'`
    with explicit names (both is_definition=true), the cross-name INHERITS writer
    must (a) emit exactly ONE edge to the deterministically-chosen definition
    (alphabetically-first module: 'aaa_fork' < 'zzz_upstream'), and (b) emit NO
    `.single()` multi-row UserWarning.

    Red-before-green: with the is_definition filter but WITHOUT the LIMIT-1
    collapse, the MATCH returns 2 rows → 2 edges + the multi-row warning.
    """
    _build_two_definition_mesh(
        writer, "account.move", mod_a="aaa_fork", mod_b="zzz_upstream",
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        writer.write_results([
            _result("sale", "sale.order", inherit=["account.move"]),
        ])

    with neo4j_driver.session() as session:
        rows = session.run(
            """
            MATCH (child:Model {name: 'sale.order', module: 'sale',
                                odoo_version: $v})
                  -[:INHERITS]->
                  (parent:Model {name: 'account.move', odoo_version: $v})
            RETURN parent.module AS parent_mod,
                   coalesce(parent.is_definition, false) AS is_def
            """,
            v=TEST_VERSION,
        ).data()

    assert len(rows) == 1, (
        f"Expected exactly 1 INHERITS edge even with D=2 definitions, got "
        f"{len(rows)}: {rows}. The is_definition filter alone returns both "
        "definition rows; the LIMIT-1 collapse must pick exactly one."
    )
    assert rows[0]["is_def"] is True
    assert rows[0]["parent_mod"] == "aaa_fork", (
        "D=2 collapse must pick the deterministic winner (field_count DESC then "
        f"module ASC → 'aaa_fork'), got {rows[0]['parent_mod']!r}."
    )

    multi_row = [
        w for w in caught
        if "single record" in str(w.message)
        or "found multiple" in str(w.message)
    ]
    assert multi_row == [], (
        "cross-name INHERITS writer emitted a .single() multi-row warning on the "
        f"D=2 definition case: {[str(w.message) for w in multi_row]}"
    )


def test_delegates_to_d2_collapses_to_one_definition_no_warning(
    writer, neo4j_driver,
):
    """ADR-0048 D>1 for DELEGATES_TO: two is_definition=true copies of
    res.partner → exactly ONE delegate edge to the alphabetically-first module,
    no multi-row warning."""
    _build_two_definition_mesh(
        writer, "res.partner", mod_a="aaa_fork", mod_b="zzz_upstream",
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        writer.write_results([
            _result("base", "res.users",
                    inherits={"res.partner": "partner_id"}),
        ])

    with neo4j_driver.session() as session:
        rows = session.run(
            """
            MATCH (m:Model {name: 'res.users', module: 'base',
                            odoo_version: $v})
                  -[:DELEGATES_TO]->
                  (d:Model {name: 'res.partner', odoo_version: $v})
            RETURN d.module AS d_mod,
                   coalesce(d.is_definition, false) AS is_def
            """,
            v=TEST_VERSION,
        ).data()

    assert len(rows) == 1, (
        f"Expected exactly 1 DELEGATES_TO edge even with D=2 definitions, got "
        f"{len(rows)}: {rows}."
    )
    assert rows[0]["is_def"] is True
    assert rows[0]["d_mod"] == "aaa_fork"

    multi_row = [
        w for w in caught
        if "single record" in str(w.message)
        or "found multiple" in str(w.message)
    ]
    assert multi_row == [], (
        "DELEGATES_TO writer emitted a .single() multi-row warning on the D=2 "
        f"definition case: {[str(w.message) for w in multi_row]}"
    )
