"""Fixture: _register = False — class opts out of registry."""
from odoo import fields, models


class AbstractQWebBase(models.AbstractModel):
    _name = 'ir.qweb.abstract'
    _register = False

    source = fields.Text('Source')
