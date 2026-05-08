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
