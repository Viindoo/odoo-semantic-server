"""Fixture: _register = False — indexer must flag register_false_chain=True (spec 5c case 2)."""
from odoo import fields, models


class ViinAbstractBase(models.AbstractModel):
    _name = 'viin.abstract.base'
    _description = 'Abstract base — not registered'
    _register = False

    viin_marker = fields.Char('Marker')
