"""Fixture: _inherit as a list (multi-inherit mixin pattern)."""
from odoo import fields, models


class MailableProduct(models.Model):
    _name = 'product.template'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    message_follower_count = fields.Integer(compute='_compute_follower_count', store=True)
