"""Fixture: multi-inherit on sale.order — adds mail.thread + mail.activity.mixin."""
from odoo import fields, models


class SaleOrderMultiInherit(models.Model):
    _inherit = ['sale.order', 'mail.thread', 'mail.activity.mixin']

    viin_note = fields.Char('Viindoo Note')
