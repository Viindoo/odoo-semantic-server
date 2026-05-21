# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_parser_python.py
import textwrap
from pathlib import Path

import pytest

from src.indexer.models import ModuleInfo
from src.indexer.parser_python import parse_file, parse_module


@pytest.fixture
def sale_module(tmp_path) -> ModuleInfo:
    return ModuleInfo(
        name="sale", odoo_version="17.0", repo="odoo_17.0",
        path=str(tmp_path), depends=["base"], version_raw="17.0.1.0.0",
    )


def write_py(directory: Path, filename: str, content: str) -> str:
    filepath = directory / filename
    filepath.write_text(textwrap.dedent(content))
    return str(filepath)


# --- parse_file tests ---

def test_parse_basic_model_name(tmp_path, sale_module):
    f = write_py(tmp_path, "sale_order.py", """
        from odoo import models, fields

        class SaleOrder(models.Model):
            _name = 'sale.order'
            _description = 'Sales Order'
    """)
    result = parse_file(f, sale_module)
    assert len(result) == 1
    assert result[0].name == "sale.order"


def test_parse_field_types(tmp_path, sale_module):
    f = write_py(tmp_path, "model.py", """
        from odoo import models, fields

        class MyModel(models.Model):
            _name = 'my.model'
            name = fields.Char(required=True)
            amount = fields.Float(compute='_compute_amount', store=True)
            partner_id = fields.Many2one('res.partner')
            line_ids = fields.One2many('my.line', 'order_id')
    """)
    result = parse_file(f, sale_module)
    assert len(result) == 1
    model = result[0]
    field_map = {fld.name: fld for fld in model.fields}

    assert field_map["name"].ttype == "char"
    assert field_map["name"].required is True
    assert field_map["amount"].compute == "_compute_amount"
    assert field_map["amount"].stored is True
    assert field_map["partner_id"].ttype == "many2one"
    assert field_map["line_ids"].ttype == "one2many"


def test_computed_field_default_not_stored(tmp_path, sale_module):
    f = write_py(tmp_path, "model.py", """
        from odoo import models, fields

        class M(models.Model):
            _name = 'm'
            computed = fields.Float(compute='_compute')
    """)
    result = parse_file(f, sale_module)
    field_map = {fld.name: fld for fld in result[0].fields}
    assert field_map["computed"].stored is False


def test_parse_single_inherit(tmp_path, sale_module):
    f = write_py(tmp_path, "extend.py", """
        from odoo import models, fields

        class SaleExtend(models.Model):
            _inherit = 'sale.order'
            x_custom = fields.Char()
    """)
    result = parse_file(f, sale_module)
    assert len(result) == 1
    model = result[0]
    assert model.name == "sale.order"
    assert "sale.order" in model.inherit


def test_parse_multi_inherit(tmp_path, sale_module):
    f = write_py(tmp_path, "mixin.py", """
        from odoo import models

        class SaleOrderMixin(models.Model):
            _name = 'sale.order'
            _inherit = ['sale.order', 'mail.thread', 'mail.activity.mixin']
    """)
    result = parse_file(f, sale_module)
    model = result[0]
    assert set(model.inherit) == {'sale.order', 'mail.thread', 'mail.activity.mixin'}


def test_parse_inherits_delegation(tmp_path, sale_module):
    f = write_py(tmp_path, "employee.py", """
        from odoo import models, fields

        class HrEmployee(models.Model):
            _name = 'hr.employee'
            _inherits = {'res.users': 'user_id'}
            user_id = fields.Many2one('res.users', required=True)
    """)
    result = parse_file(f, sale_module)
    model = result[0]
    assert model.inherits == {'res.users': 'user_id'}


def test_parse_method_with_super(tmp_path, sale_module):
    f = write_py(tmp_path, "override.py", """
        from odoo import models

        class SaleOrder(models.Model):
            _inherit = 'sale.order'

            def action_confirm(self):
                result = super().action_confirm()
                return result

            def _prepare_invoice(self):
                vals = {}
                return vals
    """)
    result = parse_file(f, sale_module)
    model = result[0]
    method_map = {m.name: m for m in model.methods}
    assert method_map["action_confirm"].has_super_call is True
    assert method_map["_prepare_invoice"].has_super_call is False


def test_parse_method_decorators(tmp_path, sale_module):
    f = write_py(tmp_path, "model.py", """
        from odoo import models, api

        class MyModel(models.Model):
            _name = 'my.model'

            @api.depends('partner_id')
            def _compute_name(self):
                pass

            @api.onchange('partner_id')
            def _onchange_partner(self):
                pass
    """)
    result = parse_file(f, sale_module)
    model = result[0]
    method_map = {m.name: m for m in model.methods}
    assert "api.depends" in method_map["_compute_name"].decorators
    assert "api.onchange" in method_map["_onchange_partner"].decorators


def test_parse_skips_syntax_error_files(tmp_path, sale_module):
    bad = tmp_path / "bad.py"
    bad.write_text("def broken(: invalid syntax {{{")
    result = parse_file(str(bad), sale_module)
    assert result == []


def test_parse_skips_non_model_classes(tmp_path, sale_module):
    f = write_py(tmp_path, "utils.py", """
        class MyHelper:
            def do_something(self):
                pass
    """)
    result = parse_file(f, sale_module)
    assert result == []


# --- parse_module tests ---

def test_parse_module_scans_all_py_files(tmp_path, sale_module):
    sale_module_with_path = ModuleInfo(
        name="sale", odoo_version="17.0", repo="odoo_17.0",
        path=str(tmp_path), depends=[], version_raw="17.0.1.0.0",
    )
    write_py(tmp_path, "sale_order.py", """
        from odoo import models
        class SaleOrder(models.Model):
            _name = 'sale.order'
    """)
    write_py(tmp_path, "sale_line.py", """
        from odoo import models
        class SaleOrderLine(models.Model):
            _name = 'sale.order.line'
    """)
    result = parse_module(sale_module_with_path)
    model_names = {m.name for m in result.models}
    assert "sale.order" in model_names
    assert "sale.order.line" in model_names


def test_parse_module_skips_manifest(tmp_path):
    module = ModuleInfo(
        name="sale", odoo_version="17.0", repo="test",
        path=str(tmp_path), depends=[], version_raw="",
    )
    (tmp_path / "__manifest__.py").write_text("{'name': 'Sales'}")
    result = parse_module(module)
    assert result.models == []


# --- Era-aware parser tests (M4.5 WI1.2 — Odoo v8/v9 Python 2 / _columns) ---

@pytest.fixture
def v8_module(tmp_path) -> ModuleInfo:
    return ModuleInfo(
        name="account", odoo_version="8.0", repo="odoo_8.0",
        path=str(tmp_path), depends=["base"], version_raw="8.0.1.0",
    )


def test_parser_python_era1_columns_dict_detects_fields(tmp_path, v8_module):
    """_columns dict (v8/v9) → fields detected by name + type."""
    f = write_py(tmp_path, "model.py", """
        from openerp.osv import osv, fields

        class X(osv.osv):
            _name = 'x.model'
            _columns = {
                'amount': fields.float('Amount'),
                'name': fields.char('Name', size=64),
                'partner_id': fields.many2one('res.partner', 'Partner'),
            }
    """)
    result = parse_file(f, v8_module)
    assert len(result) == 1
    model = result[0]
    assert model.name == "x.model"
    field_map = {fld.name: fld for fld in model.fields}
    assert field_map["amount"].ttype == "float"
    assert field_map["name"].ttype == "char"
    assert field_map["partner_id"].ttype == "many2one"


def test_parser_python_era1_python2_print_statement_no_crash(tmp_path, v8_module):
    """Python 2 syntax (`print x`) outside class shouldn't crash — graceful fallback."""
    bad = tmp_path / "x.py"
    bad.write_text(
        "print 'hello'\n\n"
        "class X(osv.osv):\n"
        "    _name = 'x'\n"
        "    _columns = {'a': fields.char('A')}\n"
    )
    result = parse_file(str(bad), v8_module)
    # Should not raise. Best-effort: extract _name + a field.
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0].name == "x"
    assert any(fld.name == "a" for fld in result[0].fields)


def test_parser_python_era1_legacy_field_types_detected(tmp_path, v8_module):
    """fields.function, fields.related, fields.dummy → ttype detected (legacy types)."""
    f = write_py(tmp_path, "model.py", """
        from openerp.osv import osv, fields

        class X(osv.osv):
            _name = 'x'
            _columns = {
                'fn': fields.function(_compute_x, type='float'),
                'rel': fields.related('partner_id', 'name', type='char'),
                'dum': fields.dummy('Dummy'),
            }
    """)
    result = parse_file(f, v8_module)
    field_map = {fld.name: fld for fld in result[0].fields}
    assert field_map["fn"].ttype == "function"
    assert field_map["rel"].ttype == "related"
    assert field_map["dum"].ttype == "dummy"


def test_parser_python_era1_inherit_string(tmp_path, v8_module):
    """_inherit = 'res.partner' → model.inherit populated even without _name."""
    f = write_py(tmp_path, "ext.py", """
        from openerp.osv import osv, fields

        class X(osv.osv):
            _inherit = 'res.partner'
            _columns = {'x_extra': fields.char('Extra')}
    """)
    result = parse_file(f, v8_module)
    assert len(result) == 1
    assert "res.partner" in result[0].inherit
    # name = inherit[0] when _name missing (Odoo convention)
    assert result[0].name == "res.partner"


# --- Era1 method extraction tests (PR#11 WI-F5) ------------------------------


def test_era1_extracts_method_names_from_class_block(tmp_path, v8_module):
    """Era1 fallback extracts def method_name(self, ...) from class body."""
    bad = tmp_path / "x.py"
    bad.write_text(
        "print 'hello'\n\n"
        "class X(osv.osv):\n"
        "    _name = 'x.model'\n"
        "    _columns = {}\n"
        "    def create(self, cr, uid, vals, context=None):\n"
        "        pass\n"
        "    def write(self, cr, uid, ids, vals, context=None):\n"
        "        pass\n"
    )
    result = parse_file(str(bad), v8_module)
    assert len(result) == 1
    method_names = [m.name for m in result[0].methods]
    assert "create" in method_names
    assert "write" in method_names


def test_era1_method_decorator_captured(tmp_path, v8_module):
    """Decorator before def in era1 class → decorators list populated."""
    bad = tmp_path / "dec.py"
    bad.write_text(
        "print 'hello'\n\n"
        "class X(osv.osv):\n"
        "    _name = 'x.dec'\n"
        "    _columns = {}\n"
        "    @api.multi\n"
        "    def baz(self, cr, uid, ids):\n"
        "        pass\n"
    )
    result = parse_file(str(bad), v8_module)
    assert len(result) == 1
    baz = next((m for m in result[0].methods if m.name == "baz"), None)
    assert baz is not None
    assert "api.multi" in baz.decorators


def test_era1_non_instance_method_not_extracted(tmp_path, v8_module):
    """def not receiving self (e.g. module-level def) → not included in methods."""
    bad = tmp_path / "noop.py"
    bad.write_text(
        "print 'hello'\n\n"
        "def helper(x):\n"
        "    pass\n\n"
        "class X(osv.osv):\n"
        "    _name = 'x.noop'\n"
        "    _columns = {}\n"
        "    def real_method(self, cr):\n"
        "        pass\n"
    )
    result = parse_file(str(bad), v8_module)
    assert len(result) == 1
    method_names = [m.name for m in result[0].methods]
    assert "helper" not in method_names
    assert "real_method" in method_names


def test_parser_python_v17_unaffected_by_era_dispatch(tmp_path, sale_module):
    """v17.0 module still uses AST parser — no regression."""
    f = write_py(tmp_path, "sale_order.py", """
        from odoo import models, fields

        class SaleOrder(models.Model):
            _name = 'sale.order'
            amount_total = fields.Monetary()
    """)
    result = parse_file(f, sale_module)
    assert len(result) == 1
    assert result[0].name == "sale.order"
    assert any(fld.name == "amount_total" for fld in result[0].fields)


# --- USES_CORE_SYMBOL detection (M4.5 WI6) ----------------------------------


def test_detect_self_name_get_call_in_method_body(tmp_path, sale_module):
    """`self.name_get()` in method body → core_symbol_refs contains 'name_get'."""
    f = write_py(tmp_path, "ext.py", """
        from odoo import models

        class SaleOrder(models.Model):
            _inherit = 'sale.order'

            def foo(self):
                return self.name_get()
    """)
    result = parse_file(f, sale_module)
    foo = next(m for m in result[0].methods if m.name == "foo")
    assert "name_get" in foo.core_symbol_refs


def test_detect_safe_eval_direct_call(tmp_path, sale_module):
    """Direct `safe_eval(expr)` call (after import) → core_symbol_refs contains 'safe_eval'."""
    f = write_py(tmp_path, "ext.py", """
        from odoo import models
        from odoo.tools import safe_eval

        class X(models.Model):
            _name = 'x'

            def foo(self):
                return safe_eval('1+1')
    """)
    result = parse_file(f, sale_module)
    foo = next(m for m in result[0].methods if m.name == "foo")
    assert "safe_eval" in foo.core_symbol_refs


def test_no_refs_for_non_deprecated_calls(tmp_path, sale_module):
    """Calls to non-deprecated APIs are NOT recorded (V0 scope is hot deprecated set)."""
    f = write_py(tmp_path, "ext.py", """
        from odoo import models

        class X(models.Model):
            _name = 'x'

            def foo(self):
                return self.search([])
    """)
    result = parse_file(f, sale_module)
    foo = next(m for m in result[0].methods if m.name == "foo")
    # `search` is not in V0 deprecated set → no ref
    assert foo.core_symbol_refs == []


def test_method_info_default_core_symbol_refs_is_empty():
    """MethodInfo.core_symbol_refs defaults to [] when no refs detected."""
    from src.indexer.models import MethodInfo
    m = MethodInfo(name="action_post")
    assert m.core_symbol_refs == []


# --- USES_CORE_SYMBOL V1 detection (M7 final-D) ------------------------------


def test_detect_fields_get_sig_change(tmp_path, sale_module):
    """`self.fields_get(['name'], attributes=['string'])` → core_symbol_refs has 'fields_get'."""
    f = write_py(tmp_path, "ext.py", """
        from odoo import models

        class X(models.Model):
            _inherit = 'sale.order'

            def foo(self):
                return self.fields_get(['name'], attributes=['string'])
    """)
    result = parse_file(f, sale_module)
    foo = next(m for m in result[0].methods if m.name == "foo")
    assert "fields_get" in foo.core_symbol_refs


def test_detect_float_compare_moved_module(tmp_path, sale_module):
    """`from odoo.tools.float_utils import float_compare; float_compare(...)` → ref captured."""
    f = write_py(tmp_path, "ext.py", """
        from odoo import models
        from odoo.tools.float_utils import float_compare

        class X(models.Model):
            _name = 'x'

            def foo(self):
                return float_compare(1.0, 2.0, precision_digits=2)
    """)
    result = parse_file(f, sale_module)
    foo = next(m for m in result[0].methods if m.name == "foo")
    assert "float_compare" in foo.core_symbol_refs


def test_detect_search_sig_change(tmp_path, sale_module):
    """`self._search(domain, access_rights_uid=uid)` → core_symbol_refs has '_search'."""
    f = write_py(tmp_path, "ext.py", """
        from odoo import models

        class X(models.Model):
            _inherit = 'sale.order'

            def bar(self):
                return self._search([('state', '=', 'draft')], access_rights_uid=1)
    """)
    result = parse_file(f, sale_module)
    bar = next(m for m in result[0].methods if m.name == "bar")
    assert "_search" in bar.core_symbol_refs


def test_v1_local_def_suppresses_emission(tmp_path, sale_module):
    """Top-level `def fields_get(...)` in same file must suppress USES_CORE_SYMBOL."""
    f = write_py(tmp_path, "ext.py", """
        from odoo import models

        def fields_get(*a, **kw):  # local helper, not Odoo ORM
            return {}

        class X(models.Model):
            _name = 'x'

            def foo(self):
                return fields_get()
    """)
    result = parse_file(f, sale_module)
    foo = next(m for m in result[0].methods if m.name == "foo")
    assert "fields_get" not in foo.core_symbol_refs, (
        "Local top-level def fields_get must suppress USES_CORE_SYMBOL emission"
    )


# --- Method convention classification (M4.6 WI2) ---------------------------


def test_classify_compute():
    from src.indexer.parser_python import _classify_method_convention
    assert _classify_method_convention("_compute_amount") == (
        "compute", "never", False,
    )


def test_classify_inverse():
    from src.indexer.parser_python import _classify_method_convention
    assert _classify_method_convention("_inverse_amount") == (
        "inverse", "never", False,
    )


def test_classify_search_method():
    from src.indexer.parser_python import _classify_method_convention
    assert _classify_method_convention("_search_partner_id") == (
        "search", "never", False,
    )


def test_classify_default():
    from src.indexer.parser_python import _classify_method_convention
    assert _classify_method_convention("_default_company_id") == (
        "default", "never", False,
    )
    assert _classify_method_convention("_get_default_user") == (
        "default", "never", False,
    )


def test_classify_action():
    from src.indexer.parser_python import _classify_method_convention
    assert _classify_method_convention("action_confirm") == (
        "action", "always", True,
    )


def test_classify_crud_create():
    from src.indexer.parser_python import _classify_method_convention
    assert _classify_method_convention("create") == ("crud", "always", True)


def test_classify_prepare():
    from src.indexer.parser_python import _classify_method_convention
    assert _classify_method_convention("_prepare_invoice_values") == (
        "prepare", "usually", False,
    )


def test_classify_public_no_underscore():
    from src.indexer.parser_python import _classify_method_convention
    assert _classify_method_convention("compute_total") == (
        "public", "usually", False,
    )


def test_method_info_default_convention_kind():
    """MethodInfo defaults: convention_kind='private', super_safety='usually'."""
    from src.indexer.models import MethodInfo
    m = MethodInfo(name="foo")
    assert m.convention_kind == "private"
    assert m.super_safety == "usually"
    assert m.return_required is False


def test_parser_populates_convention_for_action(tmp_path, sale_module):
    """Parsing `action_confirm` populates convention_kind='action' in MethodInfo."""
    f = write_py(tmp_path, "ext.py", """
        from odoo import models

        class SaleOrder(models.Model):
            _inherit = 'sale.order'

            def action_confirm(self):
                return super().action_confirm()
    """)
    result = parse_file(f, sale_module)
    mth = next(m for m in result[0].methods if m.name == "action_confirm")
    assert mth.convention_kind == "action"
    assert mth.super_safety == "always"
    assert mth.return_required is True


# --- Module edition detection (M4.6 WI1) -----------------------------------


def test_detect_edition_viindoo_prefix_viin():
    from src.indexer.parser_python import _detect_module_edition
    assert _detect_module_edition({}, "viin_helpdesk", "/any/path") == "viindoo"


def test_detect_edition_viindoo_prefix_to():
    from src.indexer.parser_python import _detect_module_edition
    assert _detect_module_edition({}, "to_quality", "/any/path") == "viindoo"


def test_detect_edition_generic_path_no_prefix_returns_custom():
    """A module with no viin_/to_ prefix in any generic repo path falls through to custom."""
    from src.indexer.parser_python import _detect_module_edition
    # Generic module name + generic repo path: should NOT be detected as viindoo
    # (path-based detection was removed — only name-prefix convention is used)
    result = _detect_module_edition({}, "anymod", "/home/x/some_addons17/anymod")
    assert result in ("custom", "community", "oca"), (
        f"Expected non-viindoo result for generic module, got {result!r}"
    )


def test_detect_edition_oeel1_returns_enterprise():
    from src.indexer.parser_python import _detect_module_edition
    # OEEL-1 → enterprise, regardless of path (path-independent)
    assert _detect_module_edition({"license": "OEEL-1"}, "knowledge", "/any/path") == "enterprise"
    assert (
        _detect_module_edition(
            {"license": "OEEL-1"}, "knowledge",
            "/home/x/proprietary/knowledge",
        ) == "enterprise"
    )


def test_detect_edition_viindoo_prefix_wins_over_oeel1():
    from src.indexer.parser_python import _detect_module_edition

    """Rule order: Viindoo > Enterprise > OCA > CE path > custom.

    Viindoo prefix wins, even if license claims OEEL-1. This guards against
    Viindoo internal addons that may use any license string but are authored
    by Viindoo (path/name prefix indicates authorship).
    """
    # viin_ prefix should return 'viindoo', NOT 'enterprise' despite OEEL-1 license
    assert (
        _detect_module_edition(
            {"license": "OEEL-1"}, "viin_test_module", "/any/path"
        ) == "viindoo"
    ), "viin_ prefix must win over OEEL-1 license"

    # Same for to_ prefix
    assert (
        _detect_module_edition(
            {"license": "OEEL-1"}, "to_crm", "/any/path"
        ) == "viindoo"
    ), "to_ prefix must win over OEEL-1 license"


def test_detect_edition_oca():
    from src.indexer.parser_python import _detect_module_edition
    assert _detect_module_edition({"license": "OCA-AGPL-3"}, "x", "/path") == "oca"


def test_detect_edition_community():
    from src.indexer.parser_python import _detect_module_edition
    assert _detect_module_edition(
        {"license": "LGPL-3"}, "sale",
        "/home/x/odoo17/odoo/addons/sale",
    ) == "community"


def test_detect_edition_fallback_custom():
    from src.indexer.parser_python import _detect_module_edition
    assert _detect_module_edition({}, "x", "/path") == "custom"


def test_detect_viindoo_equivalent_known():
    from src.indexer.parser_python import _detect_viindoo_equivalent
    assert _detect_viindoo_equivalent("helpdesk") == "viin_helpdesk"
    assert _detect_viindoo_equivalent("documents") == "viin_document"


def test_detect_viindoo_equivalent_unknown_returns_none():
    from src.indexer.parser_python import _detect_viindoo_equivalent
    assert _detect_viindoo_equivalent("nonexistent_xyz") is None


def test_module_info_has_edition_default():
    """ModuleInfo defaults: edition='community', viindoo_equivalent_qname=None."""
    m = ModuleInfo(
        name="x", odoo_version="17.0", repo="r", path="/x",
        depends=[], version_raw="",
    )
    assert m.edition == "community"
    assert m.viindoo_equivalent_qname is None


# --- _extract_columns_block tokenizer-aware tests (PR#11 WI-F4) ---------------


def test_extract_columns_handles_unbalanced_open_brace_in_string():
    """Unbalanced '{' inside a string value must NOT stop extraction early."""
    from src.indexer.parser_python import _extract_columns_block

    body = "_columns = {'help': 'Use {only open', 'name': 'char'}"
    result = _extract_columns_block(body)
    # Must return the full inner block — not '' from premature termination
    assert "'name': 'char'" in result, (
        f"Expected full block but got: {result!r}"
    )


def test_extract_columns_handles_unbalanced_close_brace_in_string():
    """Unbalanced '}' inside a string value must NOT cause early return."""
    from src.indexer.parser_python import _extract_columns_block

    body = "_columns = {'help': 'closed} only', 'name': 'char'}"
    result = _extract_columns_block(body)
    # Must not return early at '}' inside the string
    assert "'name': 'char'" in result, (
        f"Expected full block but got: {result!r}"
    )


def test_extract_columns_handles_nested_dict_correctly():
    """Nested dict in _columns → brace counter tracks depth, returns full block."""
    from src.indexer.parser_python import _extract_columns_block

    body = "_columns = {'meta': {'a': 1}, 'name': 'char'}"
    result = _extract_columns_block(body)
    assert "'meta': {'a': 1}" in result
    assert "'name': 'char'" in result


def test_extract_columns_balanced_brace_in_string_works():
    """Balanced '{...}' inside a string — already worked, must still work."""
    from src.indexer.parser_python import _extract_columns_block

    body = "_columns = {'help': 'Use {curly} braces', 'name': 'char'}"
    result = _extract_columns_block(body)
    assert "'name': 'char'" in result


# --- had_explicit_name tracking (WI-3) ----------------------------------------


def test_had_explicit_name_true_when_name_declared(tmp_path, sale_module):
    """_name = 'foo' in class body → had_explicit_name == True."""
    f = write_py(tmp_path, "model.py", """
        from odoo import models

        class Foo(models.Model):
            _name = 'foo'
    """)
    result = parse_file(f, sale_module)
    assert len(result) == 1
    assert result[0].had_explicit_name is True


def test_had_explicit_name_false_when_only_inherit(tmp_path, sale_module):
    """_inherit = 'foo' without _name → had_explicit_name == False (name auto-derived)."""
    f = write_py(tmp_path, "ext.py", """
        from odoo import models

        class FooExt(models.Model):
            _inherit = 'foo'
    """)
    result = parse_file(f, sale_module)
    assert len(result) == 1
    assert result[0].had_explicit_name is False
    assert result[0].name == "foo"  # auto-derived from inherit[0]


def test_had_explicit_name_true_when_redeclare(tmp_path, sale_module):
    """Both _name = 'foo' and _inherit = 'foo' → had_explicit_name == True."""
    f = write_py(tmp_path, "redeclare.py", """
        from odoo import models

        class FooRedeclare(models.Model):
            _name = 'foo'
            _inherit = 'foo'
    """)
    result = parse_file(f, sale_module)
    assert len(result) == 1
    assert result[0].had_explicit_name is True
    assert result[0].name == "foo"


def test_had_explicit_name_era1_text_path(tmp_path):
    """Era1 text-regex path (_name = 'foo' in v8 class body) → had_explicit_name == True."""
    v8_module = ModuleInfo(
        name="account", odoo_version="8.0", repo="odoo_8.0",
        path=str(tmp_path), depends=["base"], version_raw="8.0.1.0",
    )
    # Python 2 print statement forces era1 fallback
    bad = tmp_path / "era1_model.py"
    bad.write_text(
        "print 'hello'\n\n"
        "class X(osv.osv):\n"
        "    _name = 'foo'\n"
        "    _columns = {'x': fields.char('X')}\n"
    )
    result = parse_file(str(bad), v8_module)
    assert len(result) == 1
    assert result[0].had_explicit_name is True
    assert result[0].name == "foo"


def test_extract_columns_falls_back_on_python2_syntax():
    """Python 2 mid-file syntax (`print x`) makes Python 3 tokenize raise
    TokenError / IndentationError / SyntaxError. Parser MUST fall through to
    the naive char-scan fallback, not bubble up an exception.

    Regression: nightly v8 smoke run 25546090542 caught
    `AttributeError: tokenize.TokenizeError` — wrong exception name + missing
    IndentationError handling. Era1 v8 indexing aborted, 0 nodes written.
    """
    from src.indexer.parser_python import _extract_columns_block

    # Era1-shaped source that looks like a method defined RIGHT AFTER _columns
    # with Python 2 print statement — would force the tokenizer to choke.
    body = (
        "_columns = {'name': 'char'}\n"
        "    def _legacy_method(self, cr, uid):\n"
        "        print 'python 2 syntax here'\n"
        "        return\n"
    )
    # Must NOT raise — tokenize errors should fall through to char-scan fallback.
    result = _extract_columns_block(body)
    assert "'name': 'char'" in result, (
        f"Expected fallback to extract _columns block, got: {result!r}"
    )


# --- Era1 _columns.update({...}) tests (WI-4) ---------------------------------


def test_era1_columns_update_extracts_fields(tmp_path):
    """_columns = {...} AND _columns.update({...}) → all fields merged (WI-4)."""
    v8_mod = ModuleInfo(
        name="account", odoo_version="8.0", repo="odoo_8.0",
        path=str(tmp_path), depends=["base"], version_raw="8.0.1.0",
    )
    src = tmp_path / "model.py"
    src.write_text(
        "print 'hello'\n\n"
        "class CashBoxIn(osv.osv):\n"
        "    _name = 'cash.box.in'\n"
        "    _columns = {\n"
        "        'a': fields.char('Alpha'),\n"
        "    }\n"
        "    _columns.update({\n"
        "        'b': fields.text('Beta'),\n"
        "        'c': fields.integer('Gamma'),\n"
        "    })\n"
    )
    result = parse_file(str(src), v8_mod)
    assert len(result) == 1
    field_names = {fld.name for fld in result[0].fields}
    assert "a" in field_names, "Field 'a' from initial _columns must be present"
    assert "b" in field_names, "Field 'b' from _columns.update must be present"
    assert "c" in field_names, "Field 'c' from _columns.update must be present"


def test_era1_columns_update_only_no_initial_dict(tmp_path):
    """_columns.update({...}) with NO prior _columns = {...} → fields still extracted."""
    v8_mod = ModuleInfo(
        name="account", odoo_version="8.0", repo="odoo_8.0",
        path=str(tmp_path), depends=["base"], version_raw="8.0.1.0",
    )
    src = tmp_path / "model2.py"
    src.write_text(
        "print 'hello'\n\n"
        "class MyModel(osv.osv):\n"
        "    _name = 'my.model'\n"
        "    _columns.update({\n"
        "        'x': fields.boolean('Flag'),\n"
        "    })\n"
    )
    result = parse_file(str(src), v8_mod)
    assert len(result) == 1
    field_names = {fld.name for fld in result[0].fields}
    assert "x" in field_names, "Field 'x' from _columns.update must be extracted"


def test_era1_columns_update_multiline_dict(tmp_path):
    """_columns.update with dict spread across many lines → all fields extracted."""
    v8_mod = ModuleInfo(
        name="account", odoo_version="8.0", repo="odoo_8.0",
        path=str(tmp_path), depends=["base"], version_raw="8.0.1.0",
    )
    src = tmp_path / "model3.py"
    src.write_text(
        "print 'hello'\n\n"
        "class BigModel(osv.osv):\n"
        "    _name = 'big.model'\n"
        "    _columns.update({\n"
        "        # first field\n"
        "        'ref':\n"
        "            fields.char(\n"
        "                'Reference',\n"
        "                size=64,\n"
        "            ),\n"
        "        # second field\n"
        "        'note':\n"
        "            fields.text(\n"
        "                'Note',\n"
        "            ),\n"
        "        'amount':\n"
        "            fields.float(\n"
        "                'Amount',\n"
        "            ),\n"
        "    })\n"
    )
    result = parse_file(str(src), v8_mod)
    assert len(result) == 1
    field_names = {fld.name for fld in result[0].fields}
    assert "ref" in field_names, "Field 'ref' from multiline update dict must be extracted"
    assert "note" in field_names, "Field 'note' from multiline update dict must be extracted"
    assert "amount" in field_names, "Field 'amount' from multiline update dict must be extracted"


def test_era1_columns_copy_detected_no_field_nodes(tmp_path):
    """_columns = ParentCls._columns.copy() line detected but NOT extracted.

    Parent fields come via INHERITS; copy() is Python-level convenience.
    Followed by _columns.update({...}) to add child-specific fields (like WI-4).
    Assert: only 'foo' field present (from update), NOT any from copy().
    """
    v8_mod = ModuleInfo(
        name="account", odoo_version="8.0", repo="odoo_8.0",
        path=str(tmp_path), depends=["base"], version_raw="8.0.1.0",
    )
    src = tmp_path / "copy_model.py"
    src.write_text(
        "print 'hello'\n\n"
        "class CashBox(osv.osv):\n"
        "    _name = 'cash.box'\n"
        "    _columns = {\n"
        "        'parent_field': fields.char('Parent Field'),\n"
        "    }\n\n"
        "class CashBoxIn(CashBox):\n"
        "    _name = 'cash.box.in'\n"
        "    _columns = CashBox._columns.copy()\n"
        "    _columns.update({\n"
        "        'foo': fields.char('Foo'),\n"
        "    })\n"
    )
    result = parse_file(str(src), v8_mod)
    assert len(result) == 2, "Should extract both CashBox and CashBoxIn"

    # CashBoxIn should have ONLY 'foo' from update, NOT 'parent_field' from copy()
    cash_box_in = next((m for m in result if m.name == "cash.box.in"), None)
    assert cash_box_in is not None, "CashBoxIn model must be present"
    field_names = {fld.name for fld in cash_box_in.fields}
    assert "foo" in field_names, "Field 'foo' from _columns.update must be present"
    assert "parent_field" not in field_names, (
        "Field 'parent_field' should NOT be extracted from copy() — "
        "parent fields come via INHERITS path"
    )


# ---------------------------------------------------------------------------
# M10.5 P1 — comodel_name extraction tests
# ---------------------------------------------------------------------------


def test_era2_comodel_many2one_positional(tmp_path, sale_module):
    """era2: Many2one with positional string arg → comodel_name extracted."""
    f = write_py(tmp_path, "model.py", """
        from odoo import models, fields

        class SaleOrder(models.Model):
            _name = 'sale.order'
            partner_id = fields.Many2one('res.partner')
    """)
    result = parse_file(f, sale_module)
    field_map = {fld.name: fld for fld in result[0].fields}
    assert field_map["partner_id"].comodel_name == "res.partner"


def test_era2_comodel_many2one_kwarg(tmp_path, sale_module):
    """era2: Many2one with comodel_name kwarg → comodel_name extracted."""
    f = write_py(tmp_path, "model.py", """
        from odoo import models, fields

        class SaleOrder(models.Model):
            _name = 'sale.order'
            partner_id = fields.Many2one(comodel_name='res.partner', required=True)
    """)
    result = parse_file(f, sale_module)
    field_map = {fld.name: fld for fld in result[0].fields}
    assert field_map["partner_id"].comodel_name == "res.partner"


def test_era2_comodel_one2many(tmp_path, sale_module):
    """era2: One2many positional string arg → comodel_name extracted."""
    f = write_py(tmp_path, "model.py", """
        from odoo import models, fields

        class SaleOrder(models.Model):
            _name = 'sale.order'
            line_ids = fields.One2many('sale.order.line', 'order_id')
    """)
    result = parse_file(f, sale_module)
    field_map = {fld.name: fld for fld in result[0].fields}
    assert field_map["line_ids"].comodel_name == "sale.order.line"


def test_era2_comodel_many2many(tmp_path, sale_module):
    """era2: Many2many positional string arg → comodel_name extracted."""
    f = write_py(tmp_path, "model.py", """
        from odoo import models, fields

        class SaleOrder(models.Model):
            _name = 'sale.order'
            tag_ids = fields.Many2many('crm.tag')
    """)
    result = parse_file(f, sale_module)
    field_map = {fld.name: fld for fld in result[0].fields}
    assert field_map["tag_ids"].comodel_name == "crm.tag"


def test_era2_comodel_variable_arg_returns_none(tmp_path, sale_module):
    """era2: Many2one with variable (not string literal) → comodel_name is None."""
    f = write_py(tmp_path, "model.py", """
        from odoo import models, fields

        TARGET_MODEL = 'res.partner'

        class SaleOrder(models.Model):
            _name = 'sale.order'
            partner_id = fields.Many2one(TARGET_MODEL)
    """)
    result = parse_file(f, sale_module)
    field_map = {fld.name: fld for fld in result[0].fields}
    assert field_map["partner_id"].comodel_name is None


def test_era2_non_relational_field_comodel_none(tmp_path, sale_module):
    """era2: Non-relational fields (Char, Float) have comodel_name = None."""
    f = write_py(tmp_path, "model.py", """
        from odoo import models, fields

        class SaleOrder(models.Model):
            _name = 'sale.order'
            name = fields.Char()
            amount = fields.Float()
    """)
    result = parse_file(f, sale_module)
    field_map = {fld.name: fld for fld in result[0].fields}
    assert field_map["name"].comodel_name is None
    assert field_map["amount"].comodel_name is None


def test_era1_comodel_many2one_columns(tmp_path):
    """era1: _columns many2one with literal arg → comodel_name extracted."""
    v8_mod = ModuleInfo(
        name="account", odoo_version="8.0", repo="odoo_8.0",
        path=str(tmp_path), depends=["base"], version_raw="8.0.1.0",
    )
    src = tmp_path / "model.py"
    src.write_text(
        "print 'hello'\n\n"
        "class AccountMove(osv.osv):\n"
        "    _name = 'account.move'\n"
        "    _columns = {\n"
        "        'partner_id': fields.many2one('res.partner', 'Partner'),\n"
        "    }\n"
    )
    result = parse_file(str(src), v8_mod)
    assert len(result) == 1
    field_map = {fld.name: fld for fld in result[0].fields}
    assert field_map["partner_id"].comodel_name == "res.partner"


def test_era1_non_relational_field_comodel_none(tmp_path):
    """era1: Non-relational fields have comodel_name = None."""
    v8_mod = ModuleInfo(
        name="account", odoo_version="8.0", repo="odoo_8.0",
        path=str(tmp_path), depends=["base"], version_raw="8.0.1.0",
    )
    src = tmp_path / "model.py"
    src.write_text(
        "print 'hello'\n\n"
        "class AccountMove(osv.osv):\n"
        "    _name = 'account.move'\n"
        "    _columns = {\n"
        "        'name': fields.char('Name'),\n"
        "        'amount': fields.float('Amount'),\n"
        "    }\n"
    )
    result = parse_file(str(src), v8_mod)
    assert len(result) == 1
    field_map = {fld.name: fld for fld in result[0].fields}
    assert field_map["name"].comodel_name is None
    assert field_map["amount"].comodel_name is None
