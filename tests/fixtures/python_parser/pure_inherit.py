"""Fixture: pure _inherit extension — no _name, inherits from sale.order."""
from odoo import fields, models


class SaleOrderExtension(models.Model):
    _inherit = 'sale.order'

    custom_note = fields.Char('Custom Note', required=True)

    def action_custom(self):
        return super().action_custom()
