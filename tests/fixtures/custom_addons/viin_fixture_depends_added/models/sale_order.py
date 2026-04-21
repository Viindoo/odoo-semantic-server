"""Fixture: extend amount_total compute with additional @api.depends trigger."""
from odoo import api, fields, models


class SaleOrderDependsAdded(models.Model):
    _inherit = 'sale.order'

    amount_total = fields.Monetary(
        compute='_amount_all',
        store=True,
    )

    @api.depends('order_line.price_total', 'viin_discount_extra')
    def _amount_all(self):
        return super()._amount_all()

    viin_discount_extra = fields.Float('Extra Discount %', default=0.0)
