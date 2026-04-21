"""Fixture: override partner_id on sale.order to add readonly=True only."""
from odoo import fields, models


class SaleOrderFieldOverrideNoCompute(models.Model):
    _inherit = 'sale.order'

    partner_id = fields.Many2one(readonly=True)
