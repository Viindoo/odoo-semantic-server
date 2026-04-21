"""Fixture: base model always present regardless of optional dep."""
from odoo import fields, models


class SaleOrderConditional(models.Model):
    _inherit = 'sale.order'

    viin_base_field = fields.Char('Base Field')
