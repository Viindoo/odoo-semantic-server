# SPDX-License-Identifier: AGPL-3.0-or-later
"""Country gate, scope and limits of the auto_install install set (#380). No database.

Rules protected (oracle: Odoo source, cited per test):

* v18+ ``button_install.must_install`` only installs a candidate that declares
  ``countries`` when a company is in one of them; a fresh database's company
  has no country, so such a candidate is never auto-installed and is listed
  apart (odoo_18 ``odoo/addons/base/models/ir_module.py:425-429``, odoo_19
  ``:419-423``); v17 has no such clause (odoo_17 ``:420-422``);
* nothing is installed THROUGH a module the caller cannot see:
  the dependencies of an unavailable module are never walked;
* a timeout in the informational auto-install part degrades to one line and
  keeps the rest of the answer (ADR-0023);
* each round walks its candidates' closures in one BFS (one query per hop);
* the fresh-database baseline is reused per (version, scope), never across
  scopes;
* a scope of several profiles carries a caveat and a profile_name hint;
* module_inspect's list is capped (ADR-0023 section 3).
"""
from __future__ import annotations

import pytest

from src.constants import LIST_PREVIEW_MAX_ITEMS
from src.mcp import describe
from src.mcp.install_set import resolve_install_set
from src.mcp.orm import OrmQueryTimeout


def _mod(depends=(), triggers=None, available=True, countries=()):
    return {"depends": list(depends), "triggers": triggers, "available": available,
            "countries": list(countries)}


class Graph:
    """Lookups over a dict; unlike the Cypher, ``depends_of`` returns the deps
    of an UNAVAILABLE module too, so install_set's own guard is what is tested."""

    def __init__(self, modules):
        self.modules = modules
        self.depends_calls: list[list[str]] = []

    def depends_of(self, names):
        self.depends_calls.append(list(names))
        return {n: (n in self.modules and self.modules[n]["available"],
                    list(self.modules[n]["depends"]) if n in self.modules else [])
                for n in names}

    def candidates(self, installed, excluded):
        return [
            (n, list(m["triggers"]), list(m["countries"]))
            for n, m in self.modules.items()
            if m["triggers"] is not None and n not in installed and n not in excluded
            and m["available"]
        ]


BASE = {"base": _mod(), "web": _mod(["base"], ["base"])}


# ---------------------------------------------------------------------------
# The v18+ countries gate
# ---------------------------------------------------------------------------


def _l10n_world():
    return Graph({
        **BASE,
        "account": _mod(["web"]),
        "l10n_be": _mod(["account"], ["account"], countries=["be"]),
        "account_extra": _mod(["account"], ["account"]),
    })


def test_v18_a_country_gated_candidate_is_not_auto_installed_on_a_fresh_database():
    g = _l10n_world()
    res = resolve_install_set(["account"], g.depends_of, g.candidates, country_gate=True)
    assert [e.name for e in res.auto] == ["account_extra"]
    assert "l10n_be" not in res.installed
    assert res.country_conditional == {"l10n_be": ("be",)}


def test_v17_has_no_country_gate():
    g = _l10n_world()
    res = resolve_install_set(["account"], g.depends_of, g.candidates, country_gate=False)
    assert [e.name for e in res.auto] == ["account_extra", "l10n_be"]
    assert res.country_conditional == {}


@pytest.mark.parametrize(("version", "gated"), [
    ("17.0", False), ("18.0", True), ("19.0", True), ("20.0", True),
])
def test_the_gate_starts_at_odoo_18(monkeypatch, version, gated):
    srv = FakeSrv(_l10n_world())
    monkeypatch.setattr(describe, "_srv", srv)
    describe.clear_install_baseline_cache()
    res = describe._install_set(None, ["account"], version, None, "lbl")
    assert ("l10n_be" in res.country_conditional) is gated
    assert ("l10n_be" in {e.name for e in res.auto}) is not gated


def test_the_country_conditional_line_is_rendered_apart():
    g = _l10n_world()
    lines = describe._auto_install_branch(
        resolve_install_set(["account"], g.depends_of, g.candidates, country_gate=True),
        "account",
    )
    assert lines[:2] == [
        "├─ Auto-installed with this module (1):",
        "│   └─ account_extra (triggered by: account)",
    ]
    assert ("├─ Country-conditional, auto-installed only when a company is in one"
            " of its countries (1): l10n_be (BE)") in lines


# ---------------------------------------------------------------------------
# Never walk the dependencies of an invisible module
# ---------------------------------------------------------------------------


def test_nothing_is_installed_through_a_module_outside_the_scope():
    """`acme` (visible) depends on `foo` (another tenant's, unavailable), which
    depends on `hidden_trigger`. `bridge` (visible) triggers on
    `hidden_trigger`: reachable only through `foo`, so it must not appear, and
    neither `foo` nor its deps are installed."""
    g = Graph({
        **BASE,
        "acme": _mod(["web", "foo"]),
        "foo": _mod(["hidden_trigger"], available=False),
        "hidden_trigger": _mod(["web"]),
        "bridge": _mod(["hidden_trigger"], ["hidden_trigger"]),
    })
    res = resolve_install_set(["acme"], g.depends_of, g.candidates)
    assert res.auto == []
    assert "foo" not in res.installed
    assert "hidden_trigger" not in res.installed
    assert not any("hidden_trigger" in call for call in g.depends_calls)


def test_a_candidate_needing_an_invisible_module_names_only_its_own_declared_dependency():
    g = Graph({
        **BASE,
        "sale": _mod(["web"]),
        "bridge": _mod(["sale", "foo"], ["sale"]),
        "foo": _mod(["secret_dep"], available=False),
        "secret_dep": _mod([]),
    })
    res = resolve_install_set(["sale"], g.depends_of, g.candidates)
    assert res.auto == []
    assert res.unsatisfiable == {"bridge": "foo"}
    assert "secret_dep" not in res.installed


# ---------------------------------------------------------------------------
# Timeouts degrade, one BFS per round, cached baseline
# ---------------------------------------------------------------------------


class FakeSrv:
    """The hub surface ``describe._install_set`` reads, over a Graph."""

    NEO4J_QUERY_TIMEOUT_SECONDS = 30

    def __init__(self, graph, scope=None, roots=("account",), fail_after=None):
        self.graph = graph
        self.scope = scope or {"own": ["p"], "shared": []}
        self.roots = list(roots)
        self.fail_after = fail_after
        self.queries: list[str] = []
        self.metrics: list[str] = []

    def _scope(self, profile_name=None, **_k):
        return dict(self.scope)

    def _scope_pred(self, alias):
        return "true"

    def _resolve_profile(self, _p):
        return None

    def _nonorm_timeout(self, label):
        return OrmQueryTimeout(f"timed out: {label}")

    def _metric_nonorm_query_timeout(self, tool):
        self.metrics.append(tool)

    def _data_bounded(self, _session, text, _label, **p):
        kind = ("depends" if "UNWIND $names" in text
                else "roots" if "DEFINED_IN" in text else "candidates")
        self.queries.append(kind)
        if self.fail_after is not None and len(self.queries) > self.fail_after:
            raise OrmQueryTimeout("Query timed out after 30s")
        if kind == "roots":
            return [{"name": r} for r in self.roots]
        if kind == "depends":
            info = self.graph.depends_of(p["names"])
            return [{"name": n, "available": a, "deps": d if a else []}
                    for n, (a, d) in info.items()]
        installed = set(p["installed"])
        return [
            {"name": n, "triggers": t, "countries": c}
            for n, t, c in self.graph.candidates(installed, set(p["excluded"]))
            if set(t) <= installed
        ]


def test_impact_line_degrades_on_timeout_instead_of_failing_the_report(monkeypatch):
    srv = FakeSrv(_l10n_world(), fail_after=2)
    monkeypatch.setattr(describe, "_srv", srv)
    describe.clear_install_baseline_cache()
    line = describe._impact_auto_install_line(None, "account.move", "17.0", None, "lbl")
    assert line == ("├─ Auto-installed with the defining module: not computed"
                    " (the query timed out; retry later, or narrow with profile_name=)")
    assert srv.metrics == ["impact_analysis"]


def test_module_inspect_branch_degrades_to_one_line():
    assert describe._auto_install_branch(None, "sale") == [
        "├─ Auto-installed with this module: not computed (the query timed out;"
        " retry later, or narrow with profile_name=)",
    ]


def test_one_depends_query_per_hop_for_all_candidates_of_a_round():
    """Five candidates triggered in round 1, each with its own two-hop chain:
    the closures are walked together (2 hops = 2 lookups), not 5 x 2."""
    mods = {**BASE, "sale": _mod(["web"])}
    for i in range(5):
        mods[f"c{i}"] = _mod(["sale", f"d{i}"], ["sale"])
        mods[f"d{i}"] = _mod([f"e{i}"])
        mods[f"e{i}"] = _mod(["web"])
    g = Graph(mods)
    res = resolve_install_set(["sale"], g.depends_of, g.candidates)
    assert {e.name for e in res.auto} >= {f"c{i}" for i in range(5)}
    round_calls = [c for c in g.depends_calls if any(n.startswith("c") for n in c)]
    assert len(round_calls) == 1
    # baseline (base) + roots (sale) + round 1 (c*, then d*, then e*): each name once
    looked_up = [n for call in g.depends_calls for n in call]
    assert len(looked_up) == len(set(looked_up))


def test_the_baseline_is_cached_per_version_and_scope(monkeypatch):
    srv = FakeSrv(_l10n_world())
    monkeypatch.setattr(describe, "_srv", srv)
    describe.clear_install_baseline_cache()
    describe._install_set(None, ["account"], "17.0", None, "lbl")
    first = len(srv.queries)
    describe._install_set(None, ["account"], "17.0", None, "lbl")
    second = len(srv.queries) - first
    assert second < first  # the baseline fixpoint was not re-run
    srv.scope = {"own": ["other"], "shared": []}
    before = len(srv.queries)
    describe._install_set(None, ["account"], "17.0", None, "lbl")
    assert len(srv.queries) - before == first  # another scope: computed afresh


# ---------------------------------------------------------------------------
# Multi-profile caveat; capped list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("scope", "expected"), [
    ({"own": None, "shared": []}, "every indexed profile"),
    ({"own": ["p_ce", "p_ee"], "shared": ["p_std"]}, "3 profiles"),
    ({"own": ["p_ce"], "shared": []}, None),
])
def test_a_scope_of_several_profiles_carries_a_caveat(monkeypatch, scope, expected):
    srv = FakeSrv(_l10n_world(), scope=scope)
    monkeypatch.setattr(describe, "_srv", srv)
    describe.clear_install_baseline_cache()
    res = describe._install_set(None, ["account"], "17.0", None, "lbl")
    lines = describe._auto_install_branch(res, "account")
    notes = [ln for ln in lines if ln.startswith("├─ Note:")]
    if expected is None:
        assert notes == []
    else:
        assert notes == [
            f"├─ Note: the install set assumes every module of this scope ({expected})"
            " is on one addons path; pass profile_name= to resolve against one"
            " profile and its parents",
        ]
    line = describe._impact_auto_install_line(None, "account.move", "17.0", None, "lbl")
    assert ("profile_name=" in line) is (expected is not None)


def test_an_explicit_profile_has_no_caveat(monkeypatch):
    srv = FakeSrv(_l10n_world(), scope={"own": None, "shared": []})
    monkeypatch.setattr(describe, "_srv", srv)
    describe.clear_install_baseline_cache()
    res = describe._install_set(None, ["account"], "17.0", "p_ce", "lbl")
    assert res.caveat is None


def test_module_inspect_auto_install_list_is_capped():
    mods = {**BASE, "sale": _mod(["web"])}
    n = LIST_PREVIEW_MAX_ITEMS + 7
    for i in range(n):
        mods[f"m{i:02d}"] = _mod(["sale"], ["sale"])
    g = Graph(mods)
    lines = describe._auto_install_branch(
        resolve_install_set(["sale"], g.depends_of, g.candidates), "sale")
    assert lines[0] == f"├─ Auto-installed with this module ({n}):"
    body = [ln for ln in lines[1:] if ln.startswith("│   ")]
    assert len(body) == LIST_PREVIEW_MAX_ITEMS + 1
    assert body[-1] == "│   └─ ... and 7 more"
