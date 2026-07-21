# AST-faithful excerpt of odoo/tests/common.py @ 6f6d921bd7f40c472ed7f0c829ab8e0ec125385e
# See tests/fixtures/odoo_tests_headers/README.md - not a full copy.
import warnings


class BaseCase(unittest.TestCase, metaclass=MetaCase):
    """Subclass of TestCase for Odoo-specific code. This class is abstract and"""


class TransactionCase(BaseCase):
    """Test class in which all test methods are run in a single transaction,"""

    @classmethod
    def setUpClass(cls):
        pass


class SavepointCase(TransactionCase):
    def __init_subclass__(cls):
        super().__init_subclass__()
        warnings.warn(
            "Deprecated class SavepointCase has been merged into TransactionCase",
            DeprecationWarning, stacklevel=2,
        )


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


class HttpSavepointCase(HttpCase):
    def __init_subclass__(cls):
        super().__init_subclass__()
        warnings.warn(
            "Deprecated class HttpSavepointCase has been merged into HttpCase",
            DeprecationWarning, stacklevel=2,
        )


class Form(object):
    """Server-side form view implementation (partial)"""


class O2MForm(Form):
    pass
