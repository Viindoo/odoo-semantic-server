"""Fixture: child locally redefines a parent field — synthesis must be suppressed."""
from odoo import fields, models


class ParentModel(models.Model):
    _name = 'parent.model'

    shared_field = fields.Char('Shared')
    only_parent = fields.Integer('Only in Parent')


class ChildModel(models.Model):
    _name = 'child.model'
    _inherits = {'parent.model': 'parent_id'}

    parent_id = fields.Many2one('parent.model', required=True, ondelete='cascade')
    shared_field = fields.Char('Overridden locally')
