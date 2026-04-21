"""Fixture: product.product with _inherits delegation to product.template."""
from odoo import fields, models


class ProductTemplate(models.Model):
    _name = 'product.template'

    list_price = fields.Float('Sales Price', default=1.0)
    name = fields.Char('Product Name', required=True)
    description = fields.Text('Description')


class ProductProduct(models.Model):
    _name = 'product.product'
    _inherits = {'product.template': 'product_tmpl_id'}

    product_tmpl_id = fields.Many2one('product.template', required=True, ondelete='cascade')
    default_code = fields.Char('Internal Reference')
