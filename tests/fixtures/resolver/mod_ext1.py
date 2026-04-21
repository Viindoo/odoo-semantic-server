"""Fixture: first extension of sale.order — overrides amount_total."""
from odoo import fields, models


class SaleOrderExt1(models.Model):
    _inherit = 'sale.order'

    amount_total = fields.Monetary(compute='_amount_with_tax', store=True)

    def action_confirm(self):
        super().action_confirm()
        return True
