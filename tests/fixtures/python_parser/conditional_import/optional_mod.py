"""Optional model — imported only when an optional dependency is available."""
from odoo import fields, models


class OptionalModel(models.Model):
    _name = 'optional.model'
    _inherit = 'mail.thread'

    value = fields.Float('Value', required=True)
