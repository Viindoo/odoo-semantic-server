"""Fixture: override _order on res.partner to sort by name desc."""
from odoo import models


class ResPartnerOrderOverride(models.Model):
    _inherit = 'res.partner'
    _order = 'name desc'
