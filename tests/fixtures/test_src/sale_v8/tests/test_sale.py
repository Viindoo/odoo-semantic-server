"""Handcrafted era1 (v8/v9) fixture for parser_test degraded-path tests.

Simulates v8 Python 2 style test file. Parser must not crash and must emit
TestClassInfo nodes with test_type='unknown'.
"""
from openerp.tests.common import TransactionCase


class TestSaleV8(TransactionCase):
    """An era1 test class."""

    def setUp(self):
        super().setUp()
        self.sale_order = self.env['sale.order'].browse(1)

    def test_something(self):
        self.assertTrue(True)
