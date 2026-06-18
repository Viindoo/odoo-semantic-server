"""Handcrafted fixture: era2 test source for parser_test unit tests.

Contains:
- TestSaleCommon: a helper base (no test_ methods, subclassed by others)
- TestSaleOrder: a regular test class, subclasses TestSaleCommon
- MailCase: a non-Case mixin (no framework base in TEST_BASE_CLASSES)
- TestStandalone: uses @standalone -> commit_allowed=True
"""
from odoo.tests.common import TransactionCase, standalone, tagged


class TestSaleCommon(TransactionCase):
    """A shared base class for sale tests."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.order = cls.env['sale.order'].create({
            'partner_id': cls.env['res.partner'].create({'name': 'TestPartner'}).id,
        })
        cls.partner = cls.env['res.partner'].browse(cls.order.partner_id.id)


class MailCase:
    """A non-Case mixin class (no framework base - HIGH-1: must still emit a node)."""

    def assert_mail_sent(self, partner):
        self.assertTrue(True)


@tagged('post_install', '-at_install')
class TestSaleOrder(TestSaleCommon, MailCase):
    """Tests for sale.order model."""

    def test_amount_total_computed(self):
        """Business rule: amount_total = sum of order lines."""
        self.order.write({'order_line': []})
        self.assertEqual(self.order.amount_total, 0.0)

    def test_partner_id_required(self):
        """Business rule: partner_id is required on sale order."""
        self.assertTrue(self.order.partner_id)
        # Also access a def-use attr
        total = self.order.amount_total
        self.assertGreaterEqual(total, 0)

    @tagged('-standard')
    def test_with_negative_tag(self):
        """Has a negative tag."""
        self.assertEqual(1, 1)


@standalone
class TestModuleLifecycle(TransactionCase):
    """Module lifecycle test - allowed to commit (PP3)."""

    def test_module_install(self):
        self.env['ir.module.module'].search([('name', '=', 'sale')])
        self.assertTrue(True)


class TestSaleOrderExtra(TransactionCase):
    """Extra tests in the same file - same name as TestSaleCommon does NOT collide."""

    def test_simple(self):
        self.assertTrue(True)
