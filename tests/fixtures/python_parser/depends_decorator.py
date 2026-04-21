"""Fixture: @api.depends decorator on compute method, linked to a field."""
from odoo import api, fields, models


class SaleOrderLine(models.Model):
    _name = 'sale.order.line'

    price_unit = fields.Float('Unit Price')
    qty = fields.Float('Quantity')
    price_subtotal = fields.Float('Subtotal', compute='_compute_price_subtotal', store=True)

    @api.depends('price_unit', 'qty')
    def _compute_price_subtotal(self):
        for rec in self:
            rec.price_subtotal = rec.price_unit * rec.qty
