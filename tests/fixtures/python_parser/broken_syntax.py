"""Fixture: intentionally malformed Python — parser must return empty + log warning."""
from odoo import models

class BrokenModel(models.Model
    _name = 'broken.model'
