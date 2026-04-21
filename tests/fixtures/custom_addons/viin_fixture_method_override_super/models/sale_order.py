"""Fixture: override action_confirm on sale.order, calls super."""
from odoo import models


class SaleOrderMethodSuper(models.Model):
    _inherit = 'sale.order'

    def action_confirm(self):
        self._viin_pre_confirm_hook()
        return super().action_confirm()

    def _viin_pre_confirm_hook(self):
        pass
