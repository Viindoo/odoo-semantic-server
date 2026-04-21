"""Fixture: override action_confirm — does NOT call super, breaks chain."""
from odoo import models


class SaleOrderMethodBreakSuper(models.Model):
    _inherit = 'sale.order'

    def action_confirm(self):
        # Intentionally skips super() — chain_is_broken = True.
        self.write({'state': 'sale'})
        return True
