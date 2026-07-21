# AST-faithful excerpt of odoo/tests/common.py @ fca652c0bef69066ced92cc162a18da0a4101275
# See tests/fixtures/odoo_tests_headers/README.md - not a full copy.


class BaseCase(case.TestCase, metaclass=MetaCase):
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
