"""Fixture: nested classes — inner classes must NOT be emitted as independent models."""
from odoo import fields, models


class SaleOrder(models.Model):
    _name = 'sale.order'

    state = fields.Selection([('draft', 'Draft'), ('done', 'Done')], default='draft')

    class _StateHelper:
        """Inner helper class — should never appear as an independent model row."""
        DRAFT = 'draft'
        DONE = 'done'

    def action_confirm(self):
        return True
