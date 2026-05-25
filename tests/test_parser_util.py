# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_parser_util.py
"""Tests for parse_external_source — the shared external-source AST choke-point.

Root-cause coverage for the reindex-log noise:
    <unknown>:NN: SyntaxWarning: invalid escape sequence '\\s' (etc.)

These warnings come from THIRD-PARTY Odoo source (their own non-raw regex/SQL
string literals such as ``odoo/tools/sql.py`` doing ``.replace('%', '\\%')``),
not from this project's code. The helper scopes the suppression to the single
external parse and threads a real filename so future diagnostics aren't
``<unknown>``. It must NOT swallow SyntaxError (callers depend on the py2 fallback).
"""
import ast
import warnings

import pytest

from src.indexer.parser_util import parse_external_source

# Source snippets mirroring real Odoo upstream patterns. Built at runtime via
# chr(92) so the backslash lands in the *target* string under test, while this
# test module itself stays free of invalid escape sequences (own code = clean).
BS = chr(92)  # a single backslash
_SQL_LIKE_SRC = (
    "def esc(s):\n"
    f"    return s.replace('%', '{BS}%').replace('_', '{BS}_')\n"
)
_REGEX_SRC = (
    "import re\n"
    f"PAT = re.compile('^({BS}s*[a-z]+)$')\n"
)


def test_suppresses_syntaxwarning_from_external_sql_like():
    # Mirrors odoo/tools/sql.py: non-raw '\%' / '\_' LIKE-escape literals.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        tree = parse_external_source(_SQL_LIKE_SRC, filename="odoo/tools/sql.py")
    assert isinstance(tree, ast.Module)
    syntax_warns = [w for w in caught if issubclass(w.category, SyntaxWarning)]
    assert syntax_warns == [], (
        f"external SyntaxWarning leaked out: {[str(w.message) for w in syntax_warns]}"
    )


def test_suppresses_syntaxwarning_from_external_regex():
    # Mirrors odoo/models.py: non-raw '\s' regex literal.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        tree = parse_external_source(_REGEX_SRC, filename="odoo/models.py")
    assert isinstance(tree, ast.Module)
    assert not [w for w in caught if issubclass(w.category, SyntaxWarning)]


def test_raw_ast_parse_would_warn_proving_helper_is_what_suppresses():
    # Control: the SAME source through bare ast.parse DOES emit the warning, so the
    # silence above is the helper's scoped filter — not the snippet being benign.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ast.parse(_SQL_LIKE_SRC)
    assert [w for w in caught if issubclass(w.category, SyntaxWarning)], (
        "expected bare ast.parse to emit SyntaxWarning for the non-raw '\\%' literal"
    )


def test_filter_scope_is_restored_after_call():
    # The suppression must be scoped: after the helper returns, a subsequent bare
    # parse of external-style source must warn again (filter not left installed).
    parse_external_source(_SQL_LIKE_SRC, filename="odoo/tools/sql.py")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ast.parse(_REGEX_SRC)
    assert [w for w in caught if issubclass(w.category, SyntaxWarning)], (
        "helper must NOT leave a process-wide SyntaxWarning filter installed"
    )


def test_syntaxerror_is_not_swallowed():
    # SyntaxError is a real error (e.g. Python-2-only syntax) — callers rely on it
    # to trigger their regex fallback. The helper must propagate it unchanged.
    with pytest.raises(SyntaxError):
        parse_external_source("def (:\n    pass\n", filename="broken.py")


def test_filename_is_threaded_into_diagnostics():
    # A real filename must replace <unknown> in error attribution.
    with pytest.raises(SyntaxError) as exc:
        parse_external_source("def (:\n", filename="addons/foo/models/bar.py")
    assert exc.value.filename == "addons/foo/models/bar.py"


def test_default_filename_when_none_given():
    with pytest.raises(SyntaxError) as exc:
        parse_external_source("def (:\n")
    assert exc.value.filename == "<external>"
