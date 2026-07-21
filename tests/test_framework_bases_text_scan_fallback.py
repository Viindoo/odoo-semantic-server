# SPDX-License-Identifier: AGPL-3.0-or-later
"""CI coverage for the SyntaxError -> text-regex fallback in
src/indexer/framework_bases.py (issue #362 WI-1 adversarial review, section
1.5/C5: "the recovery/fallback path has ZERO CI coverage").

NEW FILE - purely additive, does not touch any existing test.

WHY THIS FILE EXISTS
---------------------
Before this file, nothing that runs on CI ever exercised
`parse_framework_bases`'s `SyntaxError` fallback (`_scan_classes_text` /
`_scan_source` in framework_bases.py): every committed fixture under
tests/fixtures/odoo_tests_headers/ (v8_common.py through v19_common.py) is
DELIBERATELY AST-faithful - it parses cleanly under Python 3 - because those
fixtures exist to power the parity comparison in
tests/test_framework_bases_parity.py, which needs a clean parse on both sides
of the diff. The only thing that ever exercised the fallback was the
`@pytest.mark.odoo_source` dev-box layer against a real
/home/tuan/git/odoo8 checkout - which SKIPS on CI, where no Odoo checkout is
on disk. A mechanism written specifically to survive Python-2 syntax had zero
coverage anywhere that syntax could actually appear on a CI run.

This file closes that gap using
tests/fixtures/odoo_tests_headers/py2_fallback_common.py - a small, faithful
excerpt of the real openerp/tests/common.py on the odoo8 branch that contains
the exact genuine Python-2-only `except select.error, e:` construct
(odoo8/openerp/tests/common.py:297) that makes the real v8/v9 files fail
`ast.parse` under Python 3.
"""
from __future__ import annotations

from pathlib import Path

import pytest

FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "odoo_tests_headers" / "py2_fallback_common.py"
)


def _import_framework_bases():
    from src.indexer import framework_bases
    return framework_bases


def _import_parse_external_source():
    from src.indexer.parser_util import parse_external_source
    return parse_external_source


def _materialize_v8_source_root(tmp_path: Path) -> Path:
    """Copy the py2-syntax fixture to <tmp_path>/openerp/tests/common.py - the
    exact era-prefixed path parse_framework_bases resolves for v8/v9 (mirrors
    tests/test_framework_bases_parity.py's _materialize_source_root helper)."""
    tests_dir = tmp_path / "openerp" / "tests"
    tests_dir.mkdir(parents=True)
    target = tests_dir / "common.py"
    target.write_text(FIXTURE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def test_fixture_genuinely_fails_ast_parse_under_python_3():
    """Precondition / red-before-green proof: this fixture must NOT be
    accidentally valid Python 3 syntax, or every assertion below would pass
    for the wrong reason (going through the AST path instead of the fallback
    this file exists to cover). If a future CPython (PEP 758, >=3.14 - see
    framework_bases.py module docstring "PYTHON-VERSION DEPENDENCE") makes
    this construct legal to parse, this assertion is the one that documents
    why and fails first, rather than the fallback-specific assertions below
    passing vacuously.
    """
    parse_external_source = _import_parse_external_source()
    source = FIXTURE_PATH.read_text(encoding="utf-8")
    with pytest.raises(SyntaxError):
        parse_external_source(source, filename=str(FIXTURE_PATH))


def test_parse_framework_bases_recovers_correct_facts_via_text_regex_fallback(tmp_path):
    """Business rule: given the precondition above holds (this fixture fails
    ast.parse), parse_framework_bases("8.0") must STILL return the correct
    {name, bases, has_setUpClass} facts for every framework base class in the
    fixture - proving the text-regex fallback (not the AST path, which is
    proven unreachable by the precondition test) recovers real class facts
    from genuine Python-2 source, exactly the case the recovery mechanism
    exists for.
    """
    fb = _import_framework_bases()
    root = _materialize_v8_source_root(tmp_path)

    parsed = fb.parse_framework_bases(root, "8.0")

    assert parsed is not None, (
        "the text-regex fallback must recover a non-None result even though "
        "ast.parse fails on this fixture - a None here would mean the "
        "fallback gave up, exactly the silent-oracle-failure this file exists "
        "to catch"
    )

    expected_bases = {
        "BaseCase": ["TestCase"],  # unittest2.TestCase -> terminal "TestCase"
        "TransactionCase": ["BaseCase"],
        "SingleTransactionCase": ["BaseCase"],
        "SavepointCase": ["SingleTransactionCase"],
        "HttpCase": ["TransactionCase"],
    }
    assert set(parsed.keys()) == set(expected_bases), (
        f"expected exactly {sorted(expected_bases)}, got {sorted(parsed.keys())}"
    )
    for name, bases in expected_bases.items():
        assert parsed[name].bases == bases, (
            f"{name}: expected bases {bases}, got {parsed[name].bases}"
        )
        assert parsed[name].file_path == "openerp/tests/common.py"
        assert parsed[name].line > 0
        assert parsed[name].is_deprecated is False, (
            f"{name}: fixture carries no deprecation marker, is_deprecated must be False"
        )

    assert parsed["SingleTransactionCase"].has_setUpClass is True, (
        "SingleTransactionCase defines its own setUpClass in the fixture "
        "(mirrors the real v8 shape) - the text-regex fallback must detect it"
    )
    for name in ("BaseCase", "TransactionCase", "SavepointCase", "HttpCase"):
        assert parsed[name].has_setUpClass is False, (
            f"{name}: fixture defines no setUpClass of its own"
        )


def test_framework_bases_menu_stays_correct_when_enriched_through_the_fallback(tmp_path):
    """Business rule: the PUBLIC, end-to-end contract - framework_bases("8.0",
    source_root) - must still return the correct curated menu, enriched with
    the fallback-derived file_path/line/has_setUpClass, when the only
    available source is genuine Python-2 syntax. This is the actual consumer-
    facing behavior the fallback exists to protect, one level above the
    lower-level parse_framework_bases check above.
    """
    fb = _import_framework_bases()
    root = _materialize_v8_source_root(tmp_path)

    single_txn = fb.framework_base("8.0", "SingleTransactionCase", odoo_source_root=root)
    assert single_txn is not None
    assert single_txn.file_path == "openerp/tests/common.py"
    assert single_txn.has_setUpClass is True
    # `line` is curated as None (the curated tables never set it - module
    # docstring "line is parse-derived ONLY") - a real integer here is only
    # possible if the fallback-derived enrichment actually ran, so this is
    # the assertion that distinguishes "enrichment via the fallback worked"
    # from "the curated values happened to already match".
    assert single_txn.line is not None and single_txn.line > 0

    http_case = fb.framework_base("8.0", "HttpCase", odoo_source_root=root)
    assert http_case is not None
    assert http_case.file_path == "openerp/tests/common.py"
    assert http_case.has_setUpClass is False
    assert http_case.line is not None and http_case.line > 0

    # Without a source root, the curated menu (line=None) is unaffected - this
    # is the control that proves the two assertions above are actually driven
    # by the fallback-enriched path, not a curated default.
    no_root_single_txn = fb.framework_base("8.0", "SingleTransactionCase")
    assert no_root_single_txn is not None
    assert no_root_single_txn.line is None
