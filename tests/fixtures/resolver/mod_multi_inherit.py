"""Fixture: new model inheriting from both mail.thread and mail.activity.mixin."""
from odoo import fields, models


class MailableRecord(models.Model):
    _name = 'mailable.record'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char('Name', required=True)
    active = fields.Boolean('Active', default=True)

    def write(self, vals):
        return super().write(vals)
