"""Fixture: override amount_total on sale.order with a new compute method."""
from odoo import api, fields, models


class SaleOrderFieldOverrideCompute(models.Model):
    _inherit = 'sale.order'

    amount_total = fields.Monetary(
        compute='_viin_amount_all',
        store=True,
    )

    @api.depends('order_line.price_total', 'order_line.discount')
    def _viin_amount_all(self):
        for order in self:
            order.amount_total = sum(order.order_line.mapped('price_total'))
