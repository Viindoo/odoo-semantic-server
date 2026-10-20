# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_parser_identity_real_checkouts.py
"""Real-checkout sample for the constant / record-xmlid parser fixes (dev box).

Two single files per major, read from a real Odoo checkout
(``tests/_odoo_checkouts.checkout_root``; skips per major when absent), each
checked against the source text itself rather than a curated expectation:

* ``website/models/ir_module_module.py``: where the source spells
  ``_inherit = _name`` the parsed model must self-extend ``ir.module.module``
  (it used to parse with no ``_inherit`` at all, making the extender a
  definition);
* the website_forum FAQ record (``<record id="default_faq"
  model="ir.ui.view">`` keyed ``website_forum.faq_accordion``): the parsed
  template must carry the record xmlid ``inherit_id ref=`` resolves against.

Bounded: two files per major, well under a second in total.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.indexer.models import ModuleInfo
from src.indexer.parser_python import parse_file as parse_python_file
from src.indexer.parser_qweb import parse_file as parse_qweb_file
from tests._odoo_checkouts import checkout_root

pytestmark = pytest.mark.odoo_source

MAJORS = list(range(14, 20))


def _addon_dir(major: int, name: str) -> Path:
    root = checkout_root(major)
    if root is None:
        pytest.skip(f"no Odoo {major} checkout (OSM_ODOO_CHECKOUTS / ODOO{major}_SRC)")
    for candidate in (root / "addons" / name, root / "odoo" / "addons" / name):
        if candidate.is_dir():
            return candidate
    pytest.skip(f"Odoo {major} checkout has no {name} addon")


def _info(addon: Path, major: int) -> ModuleInfo:
    return ModuleInfo(
        name=addon.name, odoo_version=f"{major}.0", repo=f"odoo_{major}.0",
        path=str(addon), depends=[], version_raw="",
    )


@pytest.mark.parametrize("major", MAJORS)
def test_website_ir_module_module_inherit_name_is_a_self_extension(major):
    addon = _addon_dir(major, "website")
    path = addon / "models" / "ir_module_module.py"
    if not path.is_file():
        pytest.skip(f"Odoo {major}: {path.name} absent")
    if not re.search(r"^\s+_inherit\s*=\s*_name\s*$", path.read_text(), re.MULTILINE):
        pytest.skip(f"Odoo {major}: website does not spell `_inherit = _name`")
    models = [m for m in parse_python_file(str(path), _info(addon, major))
              if m.name == "ir.module.module"]
    assert models, "website ir.module.module not parsed"
    assert all(m.inherit[:1] == ["ir.module.module"] for m in models), [
        m.inherit for m in models
    ]


@pytest.mark.parametrize("major", MAJORS)
def test_website_forum_faq_record_keeps_its_record_xmlid(major):
    addon = _addon_dir(major, "website_forum")
    files = [p for p in sorted((addon / "data").glob("*.xml"))
             if 'id="default_faq"' in p.read_text(errors="ignore")]
    if not files:
        pytest.skip(f"Odoo {major}: no default_faq record in website_forum/data")
    info = _info(addon, major)
    templates = [q for f in files for q in parse_qweb_file(str(f), info)
                 if q.record_xmlid == "website_forum.default_faq"]
    assert len(templates) == 1, [q.xmlid for f in files for q in parse_qweb_file(str(f), info)]
    assert templates[0].xmlid == templates[0].key or templates[0].key is None
