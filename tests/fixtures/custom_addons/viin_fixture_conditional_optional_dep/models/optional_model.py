"""Fixture: optional model — imported inside try/except in __init__.py (spec 5c case 1)."""
from odoo import fields, models


class SaleOrderOptional(models.Model):
    """Conditionally loaded class; parser must flag conditional_import=True."""

    _inherit = 'sale.order'

    viin_optional_field = fields.Char('Optional Field')
