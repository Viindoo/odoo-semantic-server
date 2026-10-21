# SPDX-License-Identifier: AGPL-3.0-or-later
"""The install set over REAL Odoo CE manifests. No database.

Oracle: Odoo's own ``button_install`` per major. v18+ ``must_install`` keeps a
candidate that declares ``countries`` out unless a company is in one of them
(odoo_18 ``odoo/addons/base/models/ir_module.py:425-429``, odoo_19
``:419-423``); a fresh database's company has no country. v17 has no such
clause (``:420-422``; no v17 CE module combines auto_install with countries,
so the synthetic tests cover the v17 side). Before the fix, installing
``account`` on 18.0 reported 173 auto-installed modules; now 21, with 112
country-conditional localizations listed apart (19.0: 23 + 117).

Checkouts are discovered by ``tests._odoo_checkouts.checkout_root`` (skip per
version when absent).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.indexer.registry import auto_install_triggers
from src.mcp.install_set import COUNTRY_GATE_MIN_MAJOR, resolve_install_set
from tests._odoo_checkouts import checkout_root


def _manifests(root: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for addons in (root / "odoo" / "addons", root / "addons"):
        if not addons.is_dir():
            continue
        for mf in sorted(addons.glob("*/__manifest__.py")):
            try:
                data = ast.literal_eval(mf.read_text(encoding="utf-8"))
            except (ValueError, SyntaxError):
                continue
            if data.get("installable", True):
                out.setdefault(mf.parent.name, data)
    return out


def _resolve(major: int, root_module: str):
    root = checkout_root(major)
    if root is None:
        pytest.skip(f"no Odoo {major} checkout (tests._odoo_checkouts)")
    mods = _manifests(root)
    if root_module not in mods:
        pytest.skip(f"{root_module} not in the Odoo {major} checkout")
    version = f"{major}.0"
    info = {}
    for name, m in mods.items():
        deps = list(m.get("depends") or [])
        if not deps and name != "base" and major >= 19:
            deps = ["base"]
        info[name] = (deps, auto_install_triggers(m, deps, version, name),
                      list(m.get("countries") or []))

    def depends_of(names):
        return {n: (n in info, list(info[n][0]) if n in info else []) for n in names}

    def candidates(installed, excluded):
        return [(n, t, c) for n, (_d, t, c) in info.items()
                if t is not None and n not in installed and n not in excluded]

    return info, resolve_install_set([root_module], depends_of, candidates,
                                     country_gate=major >= COUNTRY_GATE_MIN_MAJOR)


@pytest.mark.parametrize("major", [18, 19])
def test_installing_account_on_v18_plus_auto_installs_no_country_gated_module(major):
    info, res = _resolve(major, "account")
    auto = {e.name for e in res.auto}
    gated_auto = sorted(n for n in auto if info[n][2])
    assert gated_auto == [], gated_auto
    # the localizations Odoo installs only for a company in their country
    assert len(res.country_conditional) >= 50, len(res.country_conditional)
    assert all(info[n][2] for n in res.country_conditional)

