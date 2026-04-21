"""Non-conditional base model."""
from odoo import fields, models


class BaseModel(models.Model):
    _name = 'base.model'

    name = fields.Char('Name', required=True)
