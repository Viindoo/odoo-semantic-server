"""Fixture: base module declaring sale.order with amount_total field."""
from odoo import fields, models


class SaleOrder(models.Model):
    _name = 'sale.order'

    amount_total = fields.Monetary('Total', compute='_amount_all', store=True)
    partner_id = fields.Many2one('res.partner', required=True)

    def action_confirm(self):
        return True
