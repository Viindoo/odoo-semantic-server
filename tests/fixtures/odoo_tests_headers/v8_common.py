# AST-faithful excerpt of openerp/tests/common.py @ e5de6ca8013ab7e2d64937d14c4a54cceeabca5d
# See tests/fixtures/odoo_tests_headers/README.md - not a full copy.


class BaseCase(unittest2.TestCase):
    """Subclass of TestCase for common OpenERP-specific code."""


class TransactionCase(BaseCase):
    """TestCase in which each test method is run in its own transaction,"""


class SingleTransactionCase(BaseCase):
    """TestCase in which all test methods are run in the same transaction,"""

    @classmethod
    def setUpClass(cls):
        pass


class SavepointCase(SingleTransactionCase):
    """Similar to :class:`SingleTransactionCase` in that all test methods"""


class HttpCase(TransactionCase):
    """Transactional HTTP TestCase with url_open and phantomjs helpers."""
