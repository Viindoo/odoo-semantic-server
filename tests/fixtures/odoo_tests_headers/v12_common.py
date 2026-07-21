# AST-faithful excerpt of odoo/tests/common.py @ d9d73e8973036306e96bbbfdee39036b59b1f749
# See tests/fixtures/odoo_tests_headers/README.md - not a full copy.


class TreeCase(unittest.TestCase):
    pass


class BaseCase(TreeCase, MetaCase('DummyCase', (object,), {})):
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
    """Transactional HTTP TestCase with url_open and Chrome headless helpers."""


class Form(object):
    """Server-side form view implementation (partial)"""


class O2MForm(Form):
    pass
