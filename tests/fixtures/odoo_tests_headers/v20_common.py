# AST-faithful excerpt of odoo/tests/common.py @ 530bc2789e2f5df794cc30f6daa50624db65e5c0
# See tests/fixtures/odoo_tests_headers/README.md - not a full copy.


class BaseCase(case.TestCase):
    """Subclass of TestCase for Odoo-specific code. This class is abstract and"""

    @classmethod
    def setUpClass(cls):
        pass


class CrossModule(case.TestCase):
    _cross_module = True


class TransactionCase(BaseCase):
    """Test class in which all test methods are run in a single transaction,"""

    @classmethod
    def setUpClass(cls):
        pass


class HttpCase(TransactionCase):
    """Transactional HTTP TestCase with url_open and Chrome headless helpers."""

    @classmethod
    def setUpClass(cls):
        pass
