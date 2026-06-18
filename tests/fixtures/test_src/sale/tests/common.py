"""Fixture: same-name class in a different file (CRITICAL-1 test)."""
from odoo.tests.common import TransactionCase


# This class has the SAME NAME as TestSaleCommon in test_sale_order.py
# but is in a different file. CRITICAL-1: they must be two distinct nodes.
class TestSaleCommon(TransactionCase):
    """The OTHER TestSaleCommon - same module, different file."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.product = cls.env['product.product'].create({'name': 'Test Product'})
