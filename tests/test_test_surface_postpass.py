# SPDX-License-Identifier: AGPL-3.0-or-later
"""Test-surface post-pass rules (F11/L4, F46, F41, F2, F40): the INHERITS_TEST,
TestHelper projection and COVERS_* edges are exactly what the source derives,
after ONE index run, whatever the graph held before.

Rules (lane-tsurf contract section 3, FINDINGS F2/F11/F40/F41/F46):

* F11/L4 - a test base resolves to what the child imports: its own module (own
  file first), then an ``@framework`` class, then the nearest module of its
  manifest dependency closure. A same-named class of a module the child neither
  depends on nor imports from is never its base; a false edge a name-only graph
  holds is deleted on the first run; a base whose origin is unknown and that
  nothing resolves keeps its recorded edge but is never guessed into a new one;
  a base dropped from the declaration loses its edge.
* F46 - after ONE run every child of a helper reaches both the helper TestClass
  and its TestHelper projection; a second run changes nothing.
* F41 - ``is_helper`` is recomputed both ways; a projection whose class stopped
  being a helper is deleted with its edges.
* F2 - a projection's ``profile`` is exactly the union of the profiles of every
  repo copy of the helper, and follows the surviving owners after
  ``drop_module_owner``; a tenant of the second owner sees its own children.
* F40 - a COVERS_* edge the TestMethod's current refs no longer derive is
  deleted; ``via`` follows the method.

Every world is written through the real writers (``write_results`` for Module +
DEPENDS_ON, ``write_test_results``, ``write_results`` for Model/Field/Method) and
the post-pass is the production ``pipeline.reconcile_test_surface``. Expected
values come from the rules above and the real Odoo shapes cited per test, never
from what the implementation returns. All data at TEST_VERSION='99.0'.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from src.indexer.models import (
    FieldInfo,
    MethodInfo,
    ModelInfo,
    ModuleInfo,
    ModuleOwner,
    ParseResult,
    TestClassInfo,
    TestMethodInfo,
    TestParseResult,
)
from src.indexer.pipeline import reconcile_test_surface
from src.indexer.writer_neo4j import Neo4jWriter
from tests.conftest import TEST_VERSION

pytestmark = pytest.mark.neo4j

V = TEST_VERSION
CE = "tsurf_ce"          # profile of the repo shipping the module first
FORK = "tsurf_fork"      # profile of a second repo shipping the same module


@pytest.fixture
def writer(clean_neo4j):
    w = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    w.setup_indexes()
    yield w
    w.close()


# ---------------------------------------------------------------------------
# World builders (real writers)
# ---------------------------------------------------------------------------

def _cls(name: str, module: str, bases: list[str], *, file_path: str | None = None,
         tests: list[str] | None = None, repo: str = "odoo",
         refs: dict | None = None) -> TestClassInfo:
    """A TestClass with ``tests`` test_ methods (none -> it may become a helper).
    ``refs`` = TestMethod ref overrides for every method (model_refs, field_refs,
    method_refs, via). No import sources: origin unknown, as the era1 parser and
    graphs written before sources existed leave it."""
    fp = file_path or f"{module}/tests/test_{name.lower()}.py"
    methods = [
        TestMethodInfo(name=m, test_class=name, module=module, file_path=fp,
                       odoo_version=V, line=10 + i, asserts_count=1, **(refs or {}))
        for i, m in enumerate(tests or [])
    ]
    return TestClassInfo(
        name=name, module=module, file_path=fp, odoo_version=V,
        test_type="transaction", base_classes_ordered=bases,
        defines_no_test_methods=not methods, line=5, methods=methods,
    )


def _write(writer, module: str, classes: list[TestClassInfo], *, repo: str = "odoo",
           profiles: list[str] | None = None, depends: list[str] | None = None,
           models: list[ModelInfo] | None = None) -> None:
    """One module of one repo as an index run writes it: Module + DEPENDS_ON (+
    models), then its test classes."""
    profiles = profiles or [CE]
    mod = ModuleInfo(name=module, odoo_version=V, repo=repo, path=f"/{repo}/{module}",
                     depends=list(depends or []))
    writer.write_results([ParseResult(module=mod, models=models or [])], profiles=profiles)
    writer.write_test_results([TestParseResult(module=mod, test_classes=classes)],
                              profiles=profiles)


def _pass(writer) -> None:
    """The version-wide test-surface post-pass every index run executes."""
    reconcile_test_surface(writer, [V], framework_profiles=[CE])


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------

def _targets(driver, child: str, module: str) -> set[tuple]:
    """(label, module, name, file_path|None) of every INHERITS_TEST target of the
    child's TestClass node(s); file_path only for TestClass targets."""
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


def _all_edges(driver) -> set[tuple]:
    """Every INHERITS_TEST edge of the version, both endpoints fully keyed."""
    with driver.session() as s:
        rows = s.run(
            """
            MATCH (c)-[:INHERITS_TEST]->(t)
            WHERE c.odoo_version = $v
            RETURN labels(c)[0] AS cl, c.module AS cm, c.name AS cn,
                   coalesce(c.file_path, '') AS cf, coalesce(c.repo, '') AS cr,
                   labels(t)[0] AS tl, t.module AS tm, t.name AS tn,
                   coalesce(t.file_path, '') AS tf, coalesce(t.repo, '') AS tr
            """,
            v=V,
        ).data()
    return {tuple(r.values()) for r in rows}


def _projection(driver, name: str, module: str) -> dict | None:
    with driver.session() as s:
        rec = s.run(
            "MATCH (h:TestHelper {name: $n, module: $m, odoo_version: $v}) "
            "RETURN properties(h) AS p",
            n=name, m=module, v=V,
        ).single()
    return dict(rec["p"]) if rec else None


def _is_helper(driver, name: str, module: str) -> list[bool]:
    with driver.session() as s:
        return [r["h"] for r in s.run(
            "MATCH (c:TestClass {name: $n, module: $m, odoo_version: $v}) "
            "RETURN coalesce(c.is_helper, false) AS h ORDER BY c.repo",
            n=name, m=module, v=V,
        )]


def _seed_name_only_edge(driver, child: str, child_module: str,
                         base: str, base_module: str) -> None:
    """The edge a name-only resolver (pre-F11 graph) wrote."""
    with driver.session() as s:
        n = s.run(
            """
            MATCH (c:TestClass {name: $c, module: $cm, odoo_version: $v})
            MATCH (b:TestClass {name: $b, module: $bm, odoo_version: $v})
            MERGE (c)-[:INHERITS_TEST]->(b)
            RETURN count(*) AS n
            """,
            c=child, cm=child_module, b=base, bm=base_module, v=V,
        ).single()["n"]
    assert n == 1, "seed: both endpoints must exist"


TC = ("TestHelper", "@framework", "TransactionCase", None)


def _tc(module: str, name: str, fp: str) -> tuple:
    return ("TestClass", module, name, fp)


def _th(module: str, name: str) -> tuple:
    return ("TestHelper", module, name, None)


# ---------------------------------------------------------------------------
# F11 / L4 - a base resolves through the child's module and dependency closure
# ---------------------------------------------------------------------------

def _seed_sale_stock_world(writer) -> None:
    """sale/tests/common.py ``TestSaleCommon(TransactionCase)`` (17.0 shape);
    sale_management depends on sale, sale_stock on sale_management, and
    sale_stock/tests/test_sale_stock.py ``TestSaleStock(TestSaleCommon)``.
    purchase - which sale_stock does not depend on - ships a same-named
    ``TestSaleCommon`` (the L4 shape: loyalty vs sale_coupon, two classes of one
    name in unrelated modules)."""
    _write(writer, "sale", [
        _cls("TestSaleCommon", "sale", ["TransactionCase"],
             file_path="sale/tests/common.py"),
    ], depends=["account"])
    _write(writer, "purchase", [
        _cls("TestSaleCommon", "purchase", ["TransactionCase"],
             file_path="purchase/tests/common.py"),
    ], depends=["account"])
    _write(writer, "sale_management", [], depends=["sale"])
    _write(writer, "sale_stock", [
        _cls("TestSaleStock", "sale_stock", ["TestSaleCommon"],
             file_path="sale_stock/tests/test_sale_stock.py",
             tests=["test_00_sale_stock_invoice"]),
    ], depends=["sale_management"])


def test_a_test_base_resolves_through_the_dependency_closure_never_to_an_unrelated_module(
    writer, clean_neo4j,
):
    """F11/L4 (FIX): TestSaleStock's base is sale's TestSaleCommon (two hops
    through sale_management), never purchase's same-named class; the false edge
    a name-only graph holds is deleted on the first run, and the unrelated class
    gains no subclass (so it is not promoted to a helper)."""
    _seed_sale_stock_world(writer)
    _seed_name_only_edge(clean_neo4j, "TestSaleStock", "sale_stock", "TestSaleCommon", "purchase")

    _pass(writer)

    assert _targets(clean_neo4j, "TestSaleStock", "sale_stock") == {
        _tc("sale", "TestSaleCommon", "sale/tests/common.py"),
        _th("sale", "TestSaleCommon"),
    }
    assert _is_helper(clean_neo4j, "TestSaleCommon", "purchase") == [False]
    assert _projection(clean_neo4j, "TestSaleCommon", "purchase") is None


def test_a_second_run_with_no_source_change_changes_no_test_edge(writer, clean_neo4j):
    """F11 + F46 (FIX): the derived edge set is a fixed point - a second run over
    an unchanged graph leaves every INHERITS_TEST edge exactly as it was."""
    _seed_sale_stock_world(writer)
    _seed_name_only_edge(clean_neo4j, "TestSaleStock", "sale_stock", "TestSaleCommon", "purchase")
    _pass(writer)
    first = _all_edges(clean_neo4j)

    _pass(writer)

    assert _all_edges(clean_neo4j) == first


def test_a_base_changed_in_the_source_moves_the_edge(writer, clean_neo4j):
    """F11/F40 (FIX): TestSaleStock re-declared on TransactionCase loses its
    edges to TestSaleCommon (both twin nodes) and gains the framework edge."""
    _seed_sale_stock_world(writer)
    _pass(writer)
    assert _th("sale", "TestSaleCommon") in _targets(clean_neo4j, "TestSaleStock", "sale_stock")

    _write(writer, "sale_stock", [
        _cls("TestSaleStock", "sale_stock", ["TransactionCase"],
             file_path="sale_stock/tests/test_sale_stock.py",
             tests=["test_00_sale_stock_invoice"]),
    ], depends=["sale_management"])
    _pass(writer)

    assert _targets(clean_neo4j, "TestSaleStock", "sale_stock") == {TC}


def test_a_base_in_the_childs_own_file_is_resolved(writer, clean_neo4j):
    """F11 follow-up (FIX): Odoo 17.0 sale/tests/common.py declares
    ``TestSaleCommonBase(TransactionCase)`` (line 50) and, in the SAME file,
    ``TestSaleCommon(AccountTestInvoicingCommon, TestSaleCommonBase)`` (line 237);
    account/tests/common.py ``AccountTestInvoicingCommon``. Both bases resolve
    - the one defined next to the child included."""
    _write(writer, "account", [
        _cls("AccountTestInvoicingCommon", "account", ["TransactionCase"],
             file_path="account/tests/common.py"),
    ])
    _write(writer, "sale", [
        _cls("TestSaleCommonBase", "sale", ["TransactionCase"],
             file_path="sale/tests/common.py"),
        _cls("TestSaleCommon", "sale", ["AccountTestInvoicingCommon", "TestSaleCommonBase"],
             file_path="sale/tests/common.py"),
        _cls("TestSaleRefund", "sale", ["TestSaleCommon"],
             file_path="sale/tests/test_sale_refund.py", tests=["test_refund_create"]),
    ], depends=["account"])

    _pass(writer)

    got = {t for t in _targets(clean_neo4j, "TestSaleCommon", "sale") if t[0] == "TestClass"}
    assert got == {
        _tc("account", "AccountTestInvoicingCommon", "account/tests/common.py"),
        _tc("sale", "TestSaleCommonBase", "sale/tests/common.py"),
    }


def test_an_unresolvable_base_of_unknown_origin_keeps_its_edge_but_is_never_guessed(
    writer, clean_neo4j,
):
    """F11 keep rule (FIX): with no import source and no candidate in the own
    module, the framework or the dependency closure, an edge already recorded is
    kept (not guessed away) and no new edge is guessed across modules; once the
    base is dropped from the declaration its edge goes."""
    _write(writer, "mail_plugin", [
        _cls("MailPluginCommon", "mail_plugin", ["TransactionCase"],
             file_path="mail_plugin/tests/common.py"),
    ])
    _write(writer, "crm_mail_plugin", [
        _cls("TestCrmMailPlugin", "crm_mail_plugin", ["MailPluginCommon"],
             tests=["test_crm_lead_create"]),
        _cls("TestCrmMailPluginLead", "crm_mail_plugin", ["MailPluginCommon"],
             tests=["test_lead_enrich"]),
    ])  # no DEPENDS_ON recorded (e.g. a module node written before its manifest)
    _seed_name_only_edge(clean_neo4j, "TestCrmMailPlugin", "crm_mail_plugin",
                         "MailPluginCommon", "mail_plugin")

    _pass(writer)

    kept = {t for t in _targets(clean_neo4j, "TestCrmMailPlugin", "crm_mail_plugin")
            if t[0] == "TestClass"}
    assert kept == {_tc("mail_plugin", "MailPluginCommon", "mail_plugin/tests/common.py")}
    assert _targets(clean_neo4j, "TestCrmMailPluginLead", "crm_mail_plugin") == set()

    _write(writer, "crm_mail_plugin", [
        _cls("TestCrmMailPlugin", "crm_mail_plugin", [], tests=["test_crm_lead_create"]),
    ])
    _pass(writer)

    assert _targets(clean_neo4j, "TestCrmMailPlugin", "crm_mail_plugin") == set()


# ---------------------------------------------------------------------------
# F46 - one run leaves the helper edges complete
# ---------------------------------------------------------------------------

def test_one_run_links_every_child_to_both_the_helper_and_its_projection(writer, clean_neo4j):
    """F46 (FIX): after the FIRST run each child of the promoted TestSaleCommon
    has its edge to the TestClass AND to the TestHelper projection (before, the
    projection edge appeared only on the second run); a child moved off the
    helper loses both."""
    _write(writer, "sale", [
        _cls("TestSaleCommon", "sale", ["TransactionCase"], file_path="sale/tests/common.py"),
        _cls("TestSaleOrder", "sale", ["TestSaleCommon"], tests=["test_sale_order"]),
    ])
    _write(writer, "sale_stock", [
        _cls("TestSaleStock", "sale_stock", ["TestSaleCommon"], tests=["test_sale_stock"]),
    ], depends=["sale"])

    _pass(writer)

    both = {_tc("sale", "TestSaleCommon", "sale/tests/common.py"), _th("sale", "TestSaleCommon")}
    assert _targets(clean_neo4j, "TestSaleOrder", "sale") == both
    assert _targets(clean_neo4j, "TestSaleStock", "sale_stock") == both

    _write(writer, "sale_stock", [
        _cls("TestSaleStock", "sale_stock", ["TransactionCase"], tests=["test_sale_stock"]),
    ], depends=["sale"])
    _pass(writer)

    assert _targets(clean_neo4j, "TestSaleStock", "sale_stock") == {TC}
    assert _targets(clean_neo4j, "TestSaleOrder", "sale") == both


# ---------------------------------------------------------------------------
# F41 - a class that stops being a helper loses flag and projection
# ---------------------------------------------------------------------------

def test_helper_flag_and_projection_follow_the_class_both_ways(writer, clean_neo4j):
    """F41 (FIX): account/tests/common.py AccountTestInvoicingCommon is a helper
    while it is subclassed and defines no test. Gaining a test method demotes it
    (flag false, projection and its edges gone); losing it again restores the
    projection; losing its last subclass demotes it again."""
    def world(*, helper_tests: list[str], with_child: bool) -> None:
        _write(writer, "account", [
            _cls("AccountTestInvoicingCommon", "account", ["TransactionCase"],
                 file_path="account/tests/common.py", tests=helper_tests),
        ] + ([_cls("TestAccountMove", "account", ["AccountTestInvoicingCommon"],
                   tests=["test_out_invoice_line_onchange"])] if with_child else []))

    world(helper_tests=[], with_child=True)
    _pass(writer)
    assert _is_helper(clean_neo4j, "AccountTestInvoicingCommon", "account") == [True]
    assert _projection(clean_neo4j, "AccountTestInvoicingCommon", "account") is not None

    world(helper_tests=["test_common_sanity"], with_child=True)
    _pass(writer)
    assert _is_helper(clean_neo4j, "AccountTestInvoicingCommon", "account") == [False]
    assert _projection(clean_neo4j, "AccountTestInvoicingCommon", "account") is None
    assert _targets(clean_neo4j, "TestAccountMove", "account") == {
        _tc("account", "AccountTestInvoicingCommon", "account/tests/common.py"),
    }

    world(helper_tests=[], with_child=True)
    _pass(writer)
    assert _is_helper(clean_neo4j, "AccountTestInvoicingCommon", "account") == [True]
    assert _projection(clean_neo4j, "AccountTestInvoicingCommon", "account") is not None
    assert _th("account", "AccountTestInvoicingCommon") in _targets(
        clean_neo4j, "TestAccountMove", "account")

    # The only subclass is re-declared on TransactionCase: no subclass left.
    _write(writer, "account", [
        _cls("TestAccountMove", "account", ["TransactionCase"],
             tests=["test_out_invoice_line_onchange"]),
    ])
    _pass(writer)
    assert _is_helper(clean_neo4j, "AccountTestInvoicingCommon", "account") == [False]
    assert _projection(clean_neo4j, "AccountTestInvoicingCommon", "account") is None


# ---------------------------------------------------------------------------
# F2 - a helper shipped by two repos is visible to both owners
# ---------------------------------------------------------------------------

def _seed_two_repo_sale(writer) -> None:
    """``sale`` shipped by two repos (the CE checkout and a fork of it, each in
    its own profile), indexed one after the other as two runs; the fork also
    ships acme_sale (depends sale) whose test subclasses TestSaleCommon."""
    for repo, profile in (("odoo", CE), ("odoo_fork", FORK)):
        _write(writer, "sale", [
            _cls("TestSaleCommon", "sale", ["TransactionCase"],
                 file_path="sale/tests/common.py"),
            _cls("TestSaleOrder", "sale", ["TestSaleCommon"], tests=["test_sale_order"]),
        ], repo=repo, profiles=[profile])
        if repo == "odoo_fork":
            _write(writer, "acme_sale", [
                _cls("TestAcmeDiscount", "acme_sale", ["TestSaleCommon"],
                     file_path="acme_sale/tests/test_discount.py",
                     tests=["test_discount_capped"]),
            ], repo=repo, profiles=[FORK], depends=["sale"])
        _pass(writer)


def _as_tenant(own: list[str], shared: list[str], fn, *args, **kwargs):
    with patch("src.mcp.server._get_tenant_id", return_value=378), \
         patch("src.mcp.session.resolve_tenant_scope", return_value=(own, shared)):
        return fn(*args, **kwargs)


def _hierarchy_children(driver, name: str) -> list[str]:
    from src.mcp.tools.test_tools import _test_class_inspect
    out = _test_class_inspect(name=name, odoo_version=V, method="hierarchy", _driver=driver)
    lines = out.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("├─ Subclassed by:"))
    rows = []
    for ln in lines[start + 1:]:
        if not ln.startswith("│"):
            break
        rows.append(ln.split("─ ", 1)[1])
    return rows


def test_a_helper_shipped_by_two_repos_is_owned_by_both_and_follows_the_survivor(
    writer, clean_neo4j,
):
    """F2 (FIX): both repo copies of TestSaleCommon are helpers, the projection's
    profile is exactly both owners' profiles, the fork's tenant sees its own
    subclasses of the helper, and after the CE repo stops shipping ``sale``
    (drop_module_owner -> fork only) the next run leaves the projection with
    exactly the fork's profile."""
    _seed_two_repo_sale(writer)

    assert _is_helper(clean_neo4j, "TestSaleCommon", "sale") == [True, True]
    both_owners = _projection(clean_neo4j, "TestSaleCommon", "sale")["profile"]
    assert set(both_owners) == {CE, FORK}
    children = _as_tenant([FORK], [], _hierarchy_children, clean_neo4j, "TestSaleCommon")
    assert children == ["[acme_sale] TestAcmeDiscount", "[sale] TestSaleOrder"], children

    writer.drop_module_owner(V, "sale", [ModuleOwner(profile_name=FORK,
                                                     repo_basename="odoo_fork")])
    _pass(writer)

    assert _projection(clean_neo4j, "TestSaleCommon", "sale")["profile"] == [FORK]
    assert _is_helper(clean_neo4j, "TestSaleCommon", "sale") == [True]
    children = _as_tenant([FORK], [], _hierarchy_children, clean_neo4j, "TestSaleCommon")
    assert children == ["[acme_sale] TestAcmeDiscount", "[sale] TestSaleOrder"], children
    # Exactly the sorted union (deterministic, like every other profile array).
    assert both_owners == sorted([CE, FORK])


# ---------------------------------------------------------------------------
# F40 - coverage a test no longer has leaves the graph
# ---------------------------------------------------------------------------

def _models(module: str, *, defines: bool) -> list[ModelInfo]:
    """sale.order (amount_total, note, action_confirm) and res.partner (name)."""
    head = {"had_explicit_name": True} if defines else {"inherit": ["sale.order"]}
    return [
        ModelInfo(name="sale.order", module=module, odoo_version=V, **head,
                  fields=[FieldInfo(name="amount_total", ttype="monetary"),
                          FieldInfo(name="note", ttype="html")],
                  methods=[MethodInfo(name="action_confirm")]),
    ] + ([ModelInfo(name="res.partner", module=module, odoo_version=V,
                    had_explicit_name=True,
                    fields=[FieldInfo(name="name", ttype="char")])] if defines else [])


def _coverage(driver) -> set[tuple]:
    with driver.session() as s:
        rows = s.run(
            """
            MATCH (tm:TestMethod {name: 'test_sale_order_confirm', odoo_version: $v})
                  -[r:COVERS_MODEL|COVERS_FIELD|COVERS_METHOD]->(x)
            RETURN type(r) AS t, x.module AS m, x.name AS n, r.via AS via
            """,
            v=V,
        ).data()
    return {(r["t"], r["m"], r["n"], r["via"]) for r in rows}


def _coverage_world(writer, refs: dict) -> None:
    _write(writer, "sale", [
        _cls("TestSaleOrder", "sale", ["TransactionCase"],
             tests=["test_sale_order_confirm"], refs=refs),
    ], models=_models("sale", defines=True))


def test_coverage_follows_the_test_methods_current_references(writer, clean_neo4j):
    """F40 (FIX): sale/tests/test_sale_order.py-style method first covers
    sale.order + res.partner / amount_total + note / action_confirm (asserted);
    re-parsed with fewer refs (set up only) it keeps exactly the remaining edges,
    now via 'setup'; with sale.order dropped from its models the field edge goes
    too; and when the definition of sale.order moves to another Model node the
    COVERS_MODEL edge moves with it."""
    _coverage_world(writer, {"model_refs": ["sale.order", "res.partner"],
                             "field_refs": ["amount_total", "note"],
                             "method_refs": ["action_confirm"], "via": "assert"})
    _pass(writer)
    assert _coverage(clean_neo4j) == {
        ("COVERS_MODEL", "sale", "sale.order", "assert"),
        ("COVERS_MODEL", "sale", "res.partner", "assert"),
        ("COVERS_FIELD", "sale", "amount_total", "assert"),
        ("COVERS_FIELD", "sale", "note", "assert"),
        ("COVERS_METHOD", "sale", "action_confirm", "assert"),
    }

    _coverage_world(writer, {"model_refs": ["sale.order"], "field_refs": ["amount_total"],
                             "method_refs": [], "via": "setup"})
    _pass(writer)
    assert _coverage(clean_neo4j) == {
        ("COVERS_MODEL", "sale", "sale.order", "setup"),
        ("COVERS_FIELD", "sale", "amount_total", "setup"),
    }

    _coverage_world(writer, {"model_refs": ["res.partner"], "field_refs": ["amount_total"],
                             "method_refs": [], "via": "setup"})
    _pass(writer)
    assert _coverage(clean_neo4j) == {("COVERS_MODEL", "sale", "res.partner", "setup")}

    # sale.order's definition moves to another Model node (sale_core now holds
    # the _name, sale only extends it).
    _coverage_world(writer, {"model_refs": ["sale.order"], "field_refs": [],
                             "method_refs": [], "via": "setup"})
    _write(writer, "sale_core", [], models=_models("sale_core", defines=True))
    with clean_neo4j.session() as s:
        s.run("MATCH (m:Model {name: 'sale.order', module: 'sale', odoo_version: $v}) "
              "SET m.is_definition = false", v=V).consume()
    _pass(writer)
    assert _coverage(clean_neo4j) == {("COVERS_MODEL", "sale_core", "sale.order", "setup")}
