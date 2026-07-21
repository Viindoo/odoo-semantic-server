# SPDX-License-Identifier: AGPL-3.0-or-later
"""Genuine Python-2-syntax excerpt of Odoo 8's openerp/tests/common.py.

WHY THIS FILE EXISTS (issue #362 WI-1 adversarial review, section 1.5/C5)
---------------------------------------------------------------------------
The v8_common.py / v9_common.py fixtures in THIS SAME DIRECTORY are
deliberately AST-faithful excerpts that parse cleanly under Python 3 (see this
directory's README.md) - they exist to power the CI parity comparison in
tests/test_framework_bases_parity.py, which needs `ast.parse` to succeed so it
can diff class facts against the curated table.

That leaves a real gap the review found: no committed fixture anywhere in the
repo actually contains Python-2-only syntax, so `src/indexer/framework_bases.py`'s
`SyntaxError` fallback (the text-regex class-header scanner,
`_scan_classes_text` / `_scan_source`) had ZERO coverage on CI - the only thing
that ever exercised it was the `@pytest.mark.odoo_source` dev-box layer against
a real /home/tuan/git/odoo8 checkout, which SKIPS on CI (no Odoo checkout on
disk there). A mechanism written specifically to handle a hazard had no test
for the hazard, anywhere CI actually runs.

This file closes that gap. It is a small, faithful excerpt of the REAL
openerp/tests/common.py on the odoo8 branch (verified against
/home/tuan/git/odoo8 in this session): the same base-class chain
(BaseCase(unittest2.TestCase) -> TransactionCase(BaseCase),
SingleTransactionCase(BaseCase), SavepointCase(SingleTransactionCase),
HttpCase(TransactionCase)), the same has_setUpClass shape
(SingleTransactionCase only), and the EXACT genuine Python-2-only construct
that makes the real file fail `ast.parse` under Python 3 - the
`except select.error, e:` clause inside HttpCase.phantom_poll
(odoo8/openerp/tests/common.py:297) - copied verbatim, not paraphrased. Method
bodies elsewhere are reduced to `pass`; only the one construct that matters to
this test is kept faithful.

See tests/test_framework_bases_text_scan_fallback.py, which asserts (a) this
file genuinely fails ast.parse under Python 3 (so the test is not vacuous -
it is not accidentally exercising the AST path instead), and (b)
parse_framework_bases still recovers the correct {name, bases, has_setUpClass}
facts for every class here via the text-regex fallback.
"""
import errno
import select
import unittest2


class BaseCase(unittest2.TestCase):
    """Subclass of TestCase for common OpenERP-specific code."""
    pass


class TransactionCase(BaseCase):
    """Case in which each test method is run in its own transaction."""
    pass


class SingleTransactionCase(BaseCase):
    """Case in which all test methods share the same transaction."""

    @classmethod
    def setUpClass(cls):
        pass


class SavepointCase(SingleTransactionCase):
    """Similar to TransactionCase but creates a savepoint before each test."""
    pass


class HttpCase(TransactionCase):
    """Transactional HTTP TestCase with phantomjs support."""

    def phantom_poll(self, phantom, timeout):
        while True:
            try:
                ready, _, _ = select.select([phantom.stdout], [], [], 0.5)
            except select.error, e:
                # Python 2 only: `except <expr>, <name>:` - the exact
                # construct that fails Python 3's ast.parse and is the reason
                # framework_bases.parse_framework_bases falls back to a
                # text-regex class-header scan for v8/v9 source.
                err, _ = e.args
                if err == errno.EINTR:
                    continue
                raise
            break
