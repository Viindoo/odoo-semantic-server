# AST-faithful excerpt of odoo/tests/common.py @ 5a3b4ffecb06b6af1a45c9b8c6a2369b10892eaf
# See tests/fixtures/odoo_tests_headers/README.md - not a full copy.


class TreeCase(unittest.TestCase):
    pass


class BaseCase(TreeCase):
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
