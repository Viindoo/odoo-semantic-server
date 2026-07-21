# AST-faithful excerpt of odoo/tests/common.py @ 14ac0f3eb5ec6ef12023d0fec59fbe1e7504ff39
# See tests/fixtures/odoo_tests_headers/README.md - not a full copy.


class BaseCase(case.TestCase):
    """Subclass of TestCase for Odoo-specific code. This class is abstract and"""

    @classmethod
    def setUpClass(cls):
        pass


class TransactionCase(BaseCase):
    """Test class in which all test methods are run in a single transaction,"""

    @classmethod
    def setUpClass(cls):
        pass


class SingleTransactionCase(BaseCase):
    """TestCase in which all test methods are run in the same transaction,"""

    @classmethod
    def setUpClass(cls):
        pass


class HttpCase(TransactionCase):
    """Transactional HTTP TestCase with url_open and Chrome headless helpers."""

    @classmethod
    def setUpClass(cls):
        pass
