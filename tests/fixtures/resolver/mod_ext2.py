"""Fixture: second extension of sale.order — overrides amount_total again."""
from odoo import fields, models


class SaleOrderExt2(models.Model):
    _inherit = 'sale.order'

    amount_total = fields.Monetary(compute='_amount_with_margin', store=True)

    def action_confirm(self):
        return True
