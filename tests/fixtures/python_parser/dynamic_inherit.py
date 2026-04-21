"""Fixture: non-literal _inherit — must flag dynamic_inherit=True."""
from odoo import models

MIXIN_MODEL = 'mail.thread'


class DynamicInheritModel(models.Model):
    _name = 'dynamic.model'
    _inherit = MIXIN_MODEL
