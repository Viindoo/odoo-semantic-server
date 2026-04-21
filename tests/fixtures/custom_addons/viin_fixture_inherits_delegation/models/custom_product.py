"""Fixture: _inherits delegation — y.custom delegates to product.template via tmpl_id."""
from odoo import fields, models


class CustomProduct(models.Model):
    _name = 'y.custom'
    _description = 'Custom Product (delegation)'
    _inherits = {'product.template': 'tmpl_id'}

    tmpl_id = fields.Many2one('product.template', required=True, ondelete='cascade')
    custom_code = fields.Char('Custom Code')
