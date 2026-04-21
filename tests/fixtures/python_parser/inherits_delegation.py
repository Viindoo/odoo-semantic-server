"""Fixture: _inherits delegation — product.product delegates to product.template."""
from odoo import fields, models


class ProductProduct(models.Model):
    _name = 'product.product'
    _inherits = {'product.template': 'product_tmpl_id'}
    _inherit = ['mail.thread', 'mail.activity.mixin']

    product_tmpl_id = fields.Many2one('product.template', required=True, ondelete='cascade')
    default_code = fields.Char('Internal Reference')
