# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_parser_inherit_constants.py
"""Model identity declared through a tuple or a constant (no DB, seconds).

Business rule: a class's ``_name`` / ``_inherit`` is what Odoo registers, however
the addon spells it. The parser used to accept only string literals and a
``list``, so:

* ``_inherit = 'abstract.approval.request.line',`` (a 1-tuple,
  to_hr_employee_advance 17.0) produced NO parent - the INHERITS edge was
  missing;
* ``_name = INTROSPECT_SERVICE`` (viin_ai 17.0, constant defined in the same
  file and imported by siblings) dropped the whole model and its methods;
* ``_name = 'ir.module.module'; _inherit = _name`` (Odoo ``website``) lost the
  self-extension and marked the extender as the model's definition.

Each case below is a small addon written to ``tmp_path`` and parsed with the
real ``parse_module`` / ``parse_file``.
"""
from __future__ import annotations

import logging
import textwrap
from pathlib import Path

from src.indexer.models import ModuleInfo
from src.indexer.parser_python import parse_file, parse_module
from src.indexer.parser_python_era1 import _parse_era1_text

VERSION = "17.0"


def _addon(root: Path, name: str, files: dict[str, str]) -> ModuleInfo:
    module_dir = root / name
    module_dir.mkdir(parents=True, exist_ok=True)
    (module_dir / "__manifest__.py").write_text(
        f"{{'name': {name!r}, 'version': '17.0.1.0.0', 'depends': ['base']}}\n"
    )
    for rel, body in files.items():
        path = module_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip())
    return ModuleInfo(
        name=name, odoo_version=VERSION, repo="repo", path=str(module_dir),
        depends=["base"], version_raw="17.0.1.0.0",
    )


def _models(info: ModuleInfo) -> dict[str, list]:
    by_name: dict[str, list] = {}
    for model in parse_module(info).models:
        by_name.setdefault(model.name, []).append(model)
    return by_name


# --- tuple _inherit -----------------------------------------------------------

def test_one_element_tuple_inherit_names_its_parent(tmp_path):
    info = _addon(tmp_path, "to_hr_employee_advance", {
        "models/employee_advance_line.py": """
            from odoo import models

            class EmployeeAdvanceLine(models.Model):
                _name = 'employee.advance.line'
                _inherit = 'abstract.approval.request.line',
        """,
    })
    [model] = _models(info)["employee.advance.line"]
    assert model.inherit == ["abstract.approval.request.line"]
    assert model.had_explicit_name is True


def test_parenthesised_tuple_inherit_keeps_every_parent_in_order(tmp_path):
    info = _addon(tmp_path, "m", {
        "models/x.py": """
            from odoo import models

            class X(models.Model):
                _name = 'x.model'
                _inherit = ('mail.thread', 'mail.activity.mixin')
        """,
    })
    [model] = _models(info)["x.model"]
    assert model.inherit == ["mail.thread", "mail.activity.mixin"]


def test_tuple_inherit_without_name_takes_its_first_parent_as_name(tmp_path):
    info = _addon(tmp_path, "m", {
        "models/x.py": """
            from odoo import models

            class X(models.Model):
                _inherit = 'res.partner',
        """,
    })
    [model] = _models(info)["res.partner"]
    assert model.inherit == ["res.partner"]
    assert model.had_explicit_name is False


# --- constants ----------------------------------------------------------------

def test_name_bound_to_a_constant_of_the_same_file_keeps_the_model(tmp_path):
    info = _addon(tmp_path, "viin_ai", {
        "models/ui_introspect.py": """
            from odoo import models

            INTROSPECT_SERVICE = 'viin.ai.ui.introspect'

            class UiIntrospect(models.AbstractModel):
                _name = INTROSPECT_SERVICE
                _description = 'UI introspection'

                def _available_buttons(self):
                    return []

                def _resolve_capabilities(self):
                    return {}
        """,
    })
    [model] = _models(info)["viin.ai.ui.introspect"]
    assert model.had_explicit_name is True
    assert model.is_abstract is True
    assert {m.name for m in model.methods} == {"_available_buttons", "_resolve_capabilities"}


def test_constant_imported_from_a_sibling_file_resolves(tmp_path):
    info = _addon(tmp_path, "viin_ai", {
        "models/__init__.py": "from . import ui_introspect, ui_suggest\n",
        "models/ui_introspect.py": """
            INTROSPECT_SERVICE = 'viin.ai.ui.introspect'
        """,
        "models/ui_suggest.py": """
            from odoo import models
            from .ui_introspect import INTROSPECT_SERVICE

            class UiSuggest(models.AbstractModel):
                _inherit = INTROSPECT_SERVICE

                def _suggest(self):
                    return []
        """,
    })
    [model] = _models(info)["viin.ai.ui.introspect"]
    assert model.inherit == ["viin.ai.ui.introspect"]
    assert [m.name for m in model.methods] == ["_suggest"]


def test_constant_imported_from_the_parent_package_resolves_with_alias(tmp_path):
    info = _addon(tmp_path, "m", {
        "const.py": """
            MIXIN = 'm.request.log.mixin'
            PARENTS = ('mail.thread', MIXIN)
        """,
        "models/provider.py": """
            from odoo import models
            from ..const import MIXIN as LOG_MIXIN, PARENTS

            class Provider(models.Model):
                _name = 'm.provider'
                _inherit = [LOG_MIXIN, 'mail.activity.mixin']

            class Other(models.Model):
                _name = 'm.other'
                _inherit = PARENTS
        """,
    })
    models = _models(info)
    assert models["m.provider"][0].inherit == ["m.request.log.mixin", "mail.activity.mixin"]
    assert models["m.other"][0].inherit == ["mail.thread", "m.request.log.mixin"]


def test_constant_re_exported_by_the_package_init_resolves(tmp_path):
    info = _addon(tmp_path, "m", {
        "models/__init__.py": "from .const import SERVICE\nfrom . import service\n",
        "models/const.py": "SERVICE = 'm.service'\n",
        "models/service.py": """
            from odoo import models
            from . import SERVICE

            class Service(models.AbstractModel):
                _name = SERVICE
        """,
    })
    assert "m.service" in _models(info)


def test_absolute_import_of_the_own_addon_resolves(tmp_path):
    info = _addon(tmp_path, "m", {
        "models/const.py": "SERVICE = 'm.service'\n",
        "models/service.py": """
            from odoo import models
            from odoo.addons.m.models.const import SERVICE

            class Service(models.AbstractModel):
                _name = SERVICE
        """,
    })
    assert "m.service" in _models(info)


def test_inherit_reusing_the_class_name_attribute_is_a_self_extension(tmp_path):
    """Odoo website: `_name = 'ir.module.module'` then `_inherit = _name`."""
    info = _addon(tmp_path, "website", {
        "models/ir_module_module.py": """
            from odoo import models

            class IrModuleModule(models.Model):
                _name = "ir.module.module"
                _description = 'Module'
                _inherit = _name
        """,
    })
    [model] = _models(info)["ir.module.module"]
    assert model.inherit == ["ir.module.module"]
    # had_explicit_name AND name in inherit -> the writer marks it an extender,
    # not the definition (is_definition = had_explicit_name AND NOT name IN inherit).
    assert model.had_explicit_name is True
    assert model.name in model.inherit


def test_module_constant_value_is_the_one_bound_before_the_class(tmp_path):
    info = _addon(tmp_path, "m", {
        "models/x.py": """
            from odoo import models

            NAME = 'm.first'

            class X(models.Model):
                _name = NAME

            NAME = 'm.second'
        """,
    })
    assert set(_models(info)) == {"m.first"}


def test_class_attribute_shadows_the_module_constant(tmp_path):
    info = _addon(tmp_path, "m", {
        "models/x.py": """
            from odoo import models

            NAME = 'm.module_level'

            class X(models.Model):
                NAME = 'm.class_level'
                _name = NAME
        """,
    })
    assert set(_models(info)) == {"m.class_level"}


# --- unresolvable: current behaviour, no crash ---------------------------------

def test_unresolvable_constants_are_skipped_and_logged_without_crashing(tmp_path, caplog):
    info = _addon(tmp_path, "m", {
        "models/__init__.py": "",
        "models/a.py": """
            from odoo import models
            from .b import B_NAME
            from .missing import GHOST
            from odoo.addons.other_addon.const import FOREIGN

            def make():
                return 'm.computed'

            COMPUTED = make()
            if True:
                BRANCHED = 'm.branched'

            class Computed(models.Model):
                _name = COMPUTED

            class Branched(models.Model):
                _name = BRANCHED

            class Cycle(models.Model):
                _name = B_NAME

            class Ghost(models.Model):
                _name = GHOST

            class Foreign(models.Model):
                _name = FOREIGN

            class Partial(models.Model):
                _name = 'm.partial'
                _inherit = ['mail.thread', FOREIGN]
        """,
        # b.py imports back from a.py: an import cycle, never a value.
        "models/b.py": "from .a import B_NAME\n",
    })
    with caplog.at_level(logging.DEBUG, logger="src.indexer.parser_python_consts"):
        models = _models(info)
    # Unresolvable `_name` -> the class is not a model (pre-existing behaviour).
    assert set(models) == {"m.partial"}
    # A list keeps its literal elements when one element does not resolve.
    assert models["m.partial"][0].inherit == ["mail.thread"]
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "a.py" in logged and "Computed" in logged and "Foreign" in logged


def test_import_outside_the_addon_directory_is_not_followed(tmp_path):
    (tmp_path / "shared.py").write_text("NAME = 'outside.model'\n")
    info = _addon(tmp_path, "m", {
        "x.py": """
            from odoo import models
            from ..shared import NAME

            class X(models.Model):
                _name = NAME
        """,
    })
    assert _models(info) == {}


def test_parse_file_without_a_resolver_still_resolves_same_file_constants(tmp_path):
    info = _addon(tmp_path, "m", {
        "models/x.py": """
            from odoo import models

            SERVICE = 'm.service'

            class X(models.Model):
                _name = SERVICE
        """,
    })
    models = parse_file(str(Path(info.path) / "models/x.py"), info)
    assert [m.name for m in models] == ["m.service"]


# --- era1 text-regex fallback (files Python 3 cannot parse) -------------------

def _era1(source: str) -> list[tuple[str, list[str]]]:
    info = ModuleInfo(
        name="m", odoo_version="8.0", repo="repo", path="/nonexistent",
        depends=[], version_raw="",
    )
    return [(m.name, m.inherit) for m in _parse_era1_text(textwrap.dedent(source), info)]


def test_era1_reads_tuple_inherit_forms():
    assert _era1("""
        class A(osv.osv):
            _inherit = 'x.one',
    """) == [("x.one", ["x.one"])]
    assert _era1("""
        class A(osv.osv):
            _inherit = 'x.one', 'x.two'
    """) == [("x.one", ["x.one", "x.two"])]
    assert _era1("""
        class A(osv.osv):
            _name = 'a.model'
            _inherit = ('x.one',
                        'x.two')
    """) == [("a.model", ["x.one", "x.two"])]


def test_era1_plain_string_inherit_is_not_extended_by_a_later_line():
    assert _era1("""
        class A(osv.osv):
            _inherit = 'x.one'
            _description = 'not a parent',
    """) == [("x.one", ["x.one"])]
