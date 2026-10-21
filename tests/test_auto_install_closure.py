# SPDX-License-Identifier: AGPL-3.0-or-later
"""auto_install triggers and install-set closure (#380). No database.

Oracle for every expected value below: Odoo's own source, per major -
``odoo/modules/module.py`` (manifest normalisation of ``auto_install``),
``odoo/addons/base/models/ir_module.py`` (``button_install`` /
``must_install`` / ``_state_update``) and ``odoo/modules/db.py``
(``initialize``: what database creation installs). The rules are summarised
on ``src.indexer.registry.auto_install_triggers`` and in
``src.mcp.install_set``.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from src.indexer.registry import (
    _regex_extract_manifest,
    auto_install_triggers,
    build_registry,
)
from src.mcp.install_set import resolve_install_set
from tests.conftest import make_git_repo

# ---------------------------------------------------------------------------
# Trigger resolution, version by version
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["8.0", "10.0", "12.0"])
def test_pre_v13_a_trigger_list_is_only_a_flag_so_every_dependency_triggers(version):
    """v8-v12 store auto_install in a Boolean column: a non-empty list just means
    True, and the module waits for ALL of its depends (not the listed ones)."""
    manifest = {"depends": ["sale", "stock", "base"], "auto_install": ["sale"]}
    assert auto_install_triggers(manifest, manifest["depends"], version) == [
        "base", "sale", "stock",
    ]


@pytest.mark.parametrize("version", ["8.0", "12.0"])
def test_pre_v13_an_empty_list_is_false(version):
    manifest = {"depends": ["sale"], "auto_install": []}
    assert auto_install_triggers(manifest, manifest["depends"], version) is None


@pytest.mark.parametrize("version", ["13.0", "15.0", "17.0", "19.0"])
def test_v13_plus_a_trigger_list_is_the_trigger_set(version):
    """v13+: a list names the auto_install_required dependencies only."""
    manifest = {"depends": ["sale", "stock", "base"], "auto_install": ["stock", "sale"]}
    assert auto_install_triggers(manifest, manifest["depends"], version) == ["sale", "stock"]


@pytest.mark.parametrize("version", ["8.0", "14.0", "17.0"])
def test_true_means_every_dependency(version):
    manifest = {"depends": ["sale", "stock"], "auto_install": True}
    assert auto_install_triggers(manifest, manifest["depends"], version) == ["sale", "stock"]


@pytest.mark.parametrize("version", ["8.0", "17.0", "19.0"])
def test_false_or_absent_is_not_auto_installable(version):
    assert auto_install_triggers({"depends": ["sale"]}, ["sale"], version) is None
    assert auto_install_triggers(
        {"depends": ["sale"], "auto_install": False}, ["sale"], version,
    ) is None


def test_v13_plus_empty_list_is_the_always_install_form():
    """`auto_install: []` (v13+) is kept as [] - distinct from not auto-installable."""
    manifest = {"depends": ["web"], "auto_install": []}
    assert auto_install_triggers(manifest, ["web"], "17.0") == []


def test_v13_plus_a_trigger_that_is_not_a_dependency_makes_the_module_not_auto_installable(
    caplog,
):
    """Odoo asserts `auto_install` is a subset of `depends` and refuses the
    whole manifest (odoo_13 odoo/modules/module.py:374-379): no partial
    trigger set, the module is never auto-installed."""
    manifest = {"depends": ["sale"], "auto_install": ["sale", "crm"]}
    with caplog.at_level(logging.WARNING, logger="src.indexer.registry"):
        assert auto_install_triggers(manifest, ["sale"], "17.0", "mod_x") is None
    assert "crm" in caplog.text and "mod_x" in caplog.text


def test_v13_plus_a_string_is_not_a_trigger_list(caplog):
    with caplog.at_level(logging.WARNING, logger="src.indexer.registry"):
        assert auto_install_triggers(
            {"depends": ["sale"], "auto_install": "sale"}, ["sale"], "17.0",
        ) is None


def test_v8_to_v12_active_replaces_auto_install():
    """v8-v12 `if 'active' in info: info['auto_install'] = info['active']`."""
    assert auto_install_triggers(
        {"depends": ["sale"], "auto_install": True, "active": False}, ["sale"], "12.0",
    ) is None
    assert auto_install_triggers(
        {"depends": ["sale"], "active": True}, ["sale"], "8.0",
    ) == ["sale"]


@pytest.mark.parametrize("version", ["13.0", "14.0"])
def test_v13_v14_active_alone_is_not_auto_installable(version):
    """v13-v14 read `info.get('auto_install', info.get('active', False))`, but
    the defaults dict already holds `'auto_install': False` (odoo_13
    odoo/modules/module.py:336, odoo_14 :317), so `active` is never reached."""
    assert auto_install_triggers(
        {"depends": ["sale"], "active": True}, ["sale"], version,
    ) is None
    assert auto_install_triggers(
        {"depends": ["sale"], "auto_install": False, "active": True}, ["sale"], version,
    ) is None
    assert auto_install_triggers(
        {"depends": ["sale"], "auto_install": True, "active": False}, ["sale"], version,
    ) == ["sale"]


def test_v15_plus_ignores_active():
    assert auto_install_triggers(
        {"depends": ["sale"], "active": True}, ["sale"], "15.0",
    ) is None


def test_v19_an_empty_depends_becomes_base():
    """v19 load_manifest: a module (but base) with no depends depends on base."""
    assert auto_install_triggers(
        {"depends": [], "auto_install": True}, [], "19.0", "mod_y",
    ) == ["base"]
    assert auto_install_triggers(
        {"depends": [], "auto_install": True}, [], "18.0", "mod_y",
    ) == []
    assert auto_install_triggers(
        {"depends": [], "auto_install": True}, [], "19.0", "base",
    ) == []


def test_regex_fallback_keeps_the_auto_install_flag():
    """A legacy manifest that ast cannot parse still yields its auto_install flag."""
    src = (
        "# -*- coding: utf-8 -*-\nprint 'x'\n"
        "{'name': 'A', 'depends': ['sale'], 'auto_install': True}"
    )
    assert _regex_extract_manifest(src)["auto_install"] is True


def _write_manifest(module_dir: Path, **keys) -> None:
    module_dir.mkdir(parents=True, exist_ok=True)
    data = {"name": module_dir.name, "version": "17.0.1.0.0", "installable": True}
    data.update(keys)
    (module_dir / "__manifest__.py").write_text(repr(data) + "\n")


def test_registry_records_triggers_and_the_odoo_flag(tmp_path):
    """ModuleInfo carries the triggers; the flag follows Odoo (`is not False`)."""
    repo = make_git_repo(tmp_path / "r", "17.0")
    _write_manifest(repo / "bridge", depends=["sale", "stock"], auto_install=["stock"])
    _write_manifest(repo / "always", depends=["web"], auto_install=[])
    _write_manifest(repo / "plain", depends=["sale"])
    reg = build_registry([(str(repo), "17.0")])["17.0"]
    assert reg["bridge"].auto_install_triggers == ["stock"]
    assert reg["bridge"].auto_install is True
    assert reg["always"].auto_install_triggers == []
    assert reg["always"].auto_install is True
    assert reg["plain"].auto_install_triggers is None
    assert reg["plain"].auto_install is False


# ---------------------------------------------------------------------------
# Install-set fixpoint on synthetic graphs
# ---------------------------------------------------------------------------


class Graph:
    """A version's modules: depends, triggers (None = not auto-installable),
    availability (indexed and visible). Lookups record their calls."""

    def __init__(self, modules: dict[str, dict]):
        self.modules = modules
        self.candidate_calls = 0

    def depends_of(self, names):
        out = {}
        for n in names:
            m = self.modules.get(n)
            out[n] = (m is not None and m.get("available", True),
                      list(m["depends"]) if m else [])
        return out

    def candidates(self, installed, excluded):
        self.candidate_calls += 1
        rows = []
        for name, m in self.modules.items():
            trig = m.get("triggers")
            if trig is None or name in installed or name in excluded:
                continue
            if not m.get("available", True):
                continue
            # No trigger prefilter: the rule under test is resolve_install_set's.
            rows.append((name, list(trig)))
        return rows

    def resolve(self, *roots):
        return resolve_install_set(list(roots), self.depends_of, self.candidates)


def _mod(depends=(), triggers=None, available=True):
    return {"depends": list(depends), "triggers": triggers, "available": available}


def _auto_names(result):
    return [e.name for e in result.auto]


BASE = {"base": _mod(), "web": _mod(["base"], ["base"])}


def test_a_module_whose_triggers_are_all_installed_is_auto_installed():
    g = Graph({**BASE, "sale": _mod(["web"]), "stock": _mod(["web"]),
               "sale_stock": _mod(["sale", "stock"], ["sale", "stock"])})
    assert "sale_stock" not in _auto_names(g.resolve("sale"))
    res = g.resolve("sale", "stock")
    assert _auto_names(res) == ["sale_stock"]
    assert res.auto[0].triggers == ("sale", "stock")


def test_a_list_trigger_subset_pulls_the_module_and_its_other_depends():
    """v13+ list form: only `stock` is required; installing stock pulls the
    bridge AND its non-trigger dependency `sale` (Odoo _state_update)."""
    g = Graph({**BASE, "sale": _mod(["web"]), "stock": _mod(["web"]),
               "bridge": _mod(["sale", "stock"], ["stock"])})
    res = g.resolve("stock")
    assert [(e.name, e.required_by) for e in res.auto] == [
        ("bridge", None), ("sale", "bridge"),
    ]
    assert {"bridge", "sale"} <= res.installed


def test_an_unsatisfied_trigger_leaves_the_module_out():
    g = Graph({**BASE, "sale": _mod(["web"]), "crm": _mod(["web"]),
               "sale_crm": _mod(["sale", "crm"], ["sale", "crm"])})
    res = g.resolve("sale")
    assert res.auto == []
    assert "sale_crm" not in res.installed


def test_auto_install_chains_to_a_fixpoint():
    g = Graph({**BASE, "a": _mod(["web"]),
               "b": _mod(["a"], ["a"]), "c": _mod(["b"], ["b"]),
               "d": _mod(["c", "a"], ["c", "a"])})
    res = g.resolve("a")
    assert [(e.name, e.round) for e in res.auto] == [("b", 1), ("c", 2), ("d", 3)]


def test_a_dependency_cycle_terminates():
    g = Graph({**BASE, "x": _mod(["y", "web"]), "y": _mod(["x"]),
               "z": _mod(["x", "y"], ["x", "y"]), "w": _mod(["z"], ["z"])})
    res = g.resolve("x")
    assert _auto_names(res) == ["z", "w"]
    assert not res.truncated
    assert g.candidate_calls < 10


def test_modules_of_a_fresh_database_are_not_reported():
    """`web` (triggered by base) is installed at database creation, so it is in
    the baseline and never listed as pulled in by a later install."""
    g = Graph({**BASE, "sale": _mod(["web"])})
    res = g.resolve("sale")
    assert "web" in res.baseline
    assert res.auto == []


def test_a_trigger_newly_installed_by_the_request_pulls_its_module():
    """A trigger the request installs (web, not on a fresh database here) pulls
    its module; when that module is already installed at database creation the
    request adds nothing."""
    g = Graph({"base": _mod(), "web": _mod(["base"]),
               "web_extra": _mod(["web"], ["web"]),
               "sale": _mod(["web"])})
    # web is not auto-installed here, so web_extra is not in the baseline;
    # installing sale installs web (newly), which then triggers web_extra.
    res = g.resolve("sale")
    assert _auto_names(res) == ["web_extra"]
    g2 = Graph({**BASE, "web_extra": _mod(["web"], ["web"]), "sale": _mod(["web"])})
    # here web_extra is installed at creation already: nothing new for sale
    assert g2.resolve("sale").auto == []


def test_the_empty_trigger_form_is_installed_at_creation_only():
    g = Graph({**BASE, "always": _mod(["web"], []), "sale": _mod(["web"])})
    res = g.resolve("sale")
    assert "always" in res.baseline
    assert res.auto == []


def test_a_candidate_with_an_unavailable_dependency_is_not_installed():
    """Odoo refuses to install a module whose dependency it does not know;
    here `ext` is not indexed in the caller's scope."""
    g = Graph({**BASE, "sale": _mod(["web"]),
               "bridge": _mod(["sale", "ext"], ["sale"]),
               "ext": _mod([], available=False)})
    res = g.resolve("sale")
    assert res.auto == []
    assert res.unsatisfiable == {"bridge": "ext"}


def test_installing_base_reports_what_database_creation_installs():
    g = Graph({**BASE, "web_tour": _mod(["web"], ["web"]), "sale": _mod(["web"])})
    res = g.resolve("base")
    assert _auto_names(res) == ["web", "web_tour"]


# ---------------------------------------------------------------------------
# Rendering (ADR-0023 tree grammar)
# ---------------------------------------------------------------------------


def test_module_inspect_branch_labels_trigger_and_dependency_rows():
    from src.mcp.describe import _auto_install_branch

    g = Graph({**BASE, "sale": _mod(["web"]), "stock": _mod(["web"]),
               "bridge": _mod(["sale", "stock"], ["stock"])})
    lines = _auto_install_branch(g.resolve("stock"), "stock")
    assert lines == [
        "├─ Auto-installed with this module (2):",
        "│   ├─ bridge (triggered by: stock)",
        "│   └─ sale (dependency of bridge)",
    ]


def test_module_inspect_branch_for_a_fresh_database_module():
    from src.mcp.describe import _auto_install_branch

    lines = _auto_install_branch(Graph(dict(BASE)).resolve("web"), "web")
    assert lines == [
        "├─ Auto-installed with this module: none"
        " (it is already installed with every new database)",
    ]


# ---------------------------------------------------------------------------
# Backfill: manifest-only collection
# ---------------------------------------------------------------------------


def test_backfill_collects_triggers_by_the_indexer_version_rule(tmp_path):
    from ops.backfill_auto_install import collect_repo_triggers

    repo = make_git_repo(tmp_path / "r", "16.0")
    _write_manifest(repo / "bridge", depends=["sale", "stock"], auto_install=["stock"])
    _write_manifest(repo / "plain", depends=["sale"])
    _write_manifest(repo / "gone", depends=["sale"], auto_install=True, installable=False)
    rows = {r.name: r for r in collect_repo_triggers(str(repo), "17.0", "16.0")}
    # the standard branch wins over the profile version (ADR-0056 version rule)
    assert {r.odoo_version for r in rows.values()} == {"16.0"}
    assert rows["bridge"].triggers == ["stock"] and rows["bridge"].auto_install
    assert rows["plain"].triggers is None and not rows["plain"].auto_install
    assert "gone" not in rows
