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
