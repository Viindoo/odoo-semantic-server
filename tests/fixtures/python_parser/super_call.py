"""Fixture: method that calls super() and method that does not."""
from odoo import models


class SaleOrderConfirm(models.Model):
    _inherit = 'sale.order'

    def action_confirm(self):
        return super().action_confirm()

    def action_cancel(self):
        return False
