# AST-faithful excerpt of odoo/tests/common.py @ 6a8e31a8f7b6680f6316bc1b021d9986043c2ab0
# See tests/fixtures/odoo_tests_headers/README.md - not a full copy.


class BaseCase(unittest.TestCase):
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
