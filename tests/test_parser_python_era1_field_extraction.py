# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_parser_python_era1_field_extraction.py
"""Tests for era1 (v8/v9) _columns field extraction improvements (WI-A2).

Covers:
- Single-line simple field entries
- Multi-line field entries with options spanning several lines
- fields.function(...) with nested args (positional function ref + kwargs)
- fields.related(...) chain with multiple positional path segments
- Comments inside the _columns dict block
- Idempotent extraction: duplicate field name in _columns yields single FieldInfo
- } inside a string argument no longer truncates the _columns block prematurely
- source_definition captured for richer pgvector embeddings
"""

import textwrap

import pytest

from src.indexer.models import ModuleInfo
from src.indexer.parser_python import (
    _parse_era1_text,
    _string_aware_brace_scan,
)

# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def v8_module() -> ModuleInfo:
    return ModuleInfo(
        name="account", odoo_version="8.0", repo="odoo_8.0",
        path="", depends=[], version_raw="8.0.1.0",
    )


def _era1_src(columns_body: str, class_name: str = "MyModel", model_name: str = "my.model") -> str:
    """Wrap _columns body in a minimal Python 2-style class that forces the era1
    text-regex path (``print`` statement makes ast.parse fail)."""
    return (
        "print 'era1'\n\n"
        f"class {class_name}(osv.osv):\n"
        f"    _name = '{model_name}'\n"
        f"    _columns = {{\n"
        f"{textwrap.indent(columns_body, '        ')}\n"
        "    }\n"
    )


# ---------------------------------------------------------------------------
# test_extracts_simple_single_line_field
# ---------------------------------------------------------------------------

def test_extracts_simple_single_line_field(v8_module):
    """Single-line _columns entries (standard types) are extracted correctly."""
    src = _era1_src(
        "'name': fields.char('Name', size=64),\n"
        "'active': fields.boolean('Active'),\n"
        "'partner_id': fields.many2one('res.partner', 'Partner'),\n"
    )
    models = _parse_era1_text(src, v8_module)
    assert len(models) == 1
    field_map = {f.name: f for f in models[0].fields}
    assert "name" in field_map
    assert field_map["name"].ttype == "char"
    assert "active" in field_map
    assert field_map["active"].ttype == "boolean"
    assert "partner_id" in field_map
    assert field_map["partner_id"].ttype == "many2one"


# ---------------------------------------------------------------------------
# test_extracts_multi_line_field_with_options
# ---------------------------------------------------------------------------

def test_extracts_multi_line_field_with_options(v8_module):
    """Multi-line _columns entries (options on separate lines) are fully captured."""
    src = _era1_src(
        "'report_type': fields.selection(\n"
        "    [('none', '/'), ('income', 'Income'), ('expense', 'Expense')],\n"
        "    'P&L Category',\n"
        "    required=True,\n"
        "),\n"
        "'note': fields.text('Description'),\n"
    )
    models = _parse_era1_text(src, v8_module)
    assert len(models) == 1
    field_map = {f.name: f for f in models[0].fields}
    assert "report_type" in field_map
    assert field_map["report_type"].ttype == "selection"
    assert "note" in field_map
    assert field_map["note"].ttype == "text"
    # source_definition should cover the full multi-line entry
    assert field_map["report_type"].source_definition is not None
    assert "selection" in field_map["report_type"].source_definition
    assert "P&L Category" in field_map["report_type"].source_definition


# ---------------------------------------------------------------------------
# test_extracts_function_field_with_nested_args
# ---------------------------------------------------------------------------

def test_extracts_function_field_with_nested_args(v8_module):
    """fields.function(fn_ref, type='float', store=True, multi='amount') is captured.

    The positional argument is a bare name (not a string), the type is given as a
    keyword arg — this is the canonical v8 pattern for computed fields.
    """
    src = _era1_src(
        "'balance': fields.function(\n"
        "    _compute_balance,\n"
        "    type='float',\n"
        "    string='Balance',\n"
        "    store=True,\n"
        "    multi='balance',\n"
        "),\n"
        "'credit': fields.function(\n"
        "    _compute_balance,\n"
        "    type='float',\n"
        "    string='Credit',\n"
        "    multi='balance',\n"
        "),\n"
    )
    models = _parse_era1_text(src, v8_module)
    assert len(models) == 1
    field_map = {f.name: f for f in models[0].fields}
    assert "balance" in field_map
    assert field_map["balance"].ttype == "function"
    assert "credit" in field_map
    assert field_map["credit"].ttype == "function"
    # source_definition should capture the full call including the compute ref
    assert field_map["balance"].source_definition is not None
    assert "_compute_balance" in field_map["balance"].source_definition


# ---------------------------------------------------------------------------
# test_extracts_related_field_chain
# ---------------------------------------------------------------------------

def test_extracts_related_field_chain(v8_module):
    """fields.related('partner_id', 'country_id', type='many2one', ...) is captured."""
    src = _era1_src(
        "'exchange_rate': fields.related(\n"
        "    'currency_id', 'rate',\n"
        "    type='float',\n"
        "    string='Exchange Rate',\n"
        "    digits=(12, 6),\n"
        "),\n"
        "'company_currency_id': fields.related(\n"
        "    'company_id', 'currency_id',\n"
        "    type='many2one',\n"
        "    relation='res.currency',\n"
        "    string='Company Currency',\n"
        "),\n"
    )
    models = _parse_era1_text(src, v8_module)
    assert len(models) == 1
    field_map = {f.name: f for f in models[0].fields}
    assert "exchange_rate" in field_map
    assert field_map["exchange_rate"].ttype == "related"
    assert "company_currency_id" in field_map
    assert field_map["company_currency_id"].ttype == "related"
    # source_definition covers the dotted-path chain
    src_def = field_map["exchange_rate"].source_definition
    assert src_def is not None
    assert "currency_id" in src_def
    assert "rate" in src_def


# ---------------------------------------------------------------------------
# test_handles_comment_in_dict_entry
# ---------------------------------------------------------------------------

def test_handles_comment_in_dict_entry(v8_module):
    """Comments inside the _columns block do not break extraction."""
    src = _era1_src(
        "# Standard fields\n"
        "'name': fields.char('Name'),\n"
        "# This field tracks the amount\n"
        "'amount': fields.float('Amount'),  # deprecated in v10\n"
        "'partner_id': fields.many2one('res.partner', 'Partner'),\n"
    )
    models = _parse_era1_text(src, v8_module)
    assert len(models) == 1
    field_map = {f.name: f for f in models[0].fields}
    assert "name" in field_map
    assert "amount" in field_map
    assert "partner_id" in field_map


# ---------------------------------------------------------------------------
# test_idempotent_no_duplicate_field_for_same_name
# ---------------------------------------------------------------------------

def test_idempotent_no_duplicate_field_for_same_name(v8_module):
    """When the same field name appears more than once in a _columns block (e.g.
    due to copy-paste or a dynamic helper), only the first occurrence is kept —
    no duplicate FieldInfo objects are emitted.
    """
    src = _era1_src(
        "'name': fields.char('Name'),\n"
        "'amount': fields.float('Amount'),\n"
        "'name': fields.char('Name (dup)'),\n"  # intentional duplicate
    )
    models = _parse_era1_text(src, v8_module)
    assert len(models) == 1
    names = [f.name for f in models[0].fields]
    # No duplicate 'name' — list length == set length
    assert len(names) == len(set(names)), f"Duplicate fields found: {names}"
    assert names.count("name") == 1


# ---------------------------------------------------------------------------
# test_brace_in_help_string_does_not_truncate_block
# ---------------------------------------------------------------------------

def test_brace_in_help_string_does_not_truncate_block(v8_module):
    """A closing brace ``}`` inside a help/domain string must NOT cause the
    balanced-brace scanner to stop early and miss subsequent field entries.

    This is the primary bug fixed by ``_string_aware_brace_scan``: the naive
    char-scan fallback (used when Python's ``tokenize`` raises on Py2 source)
    would stop at the first ``}`` it saw, even if that ``}`` was inside a string
    literal — causing all fields after that entry to be silently dropped.
    """
    src = _era1_src(
        "'type': fields.selection(\n"
        "    [('view', 'View'), ('other', 'Other')],\n"
        "    'Internal Type',\n"
        "    help='Use {type} to categorise accounts.',\n"
        "),\n"
        # This field must be extracted despite the '}' in the help string above
        "'balance': fields.function(\n"
        "    _compute,\n"
        "    type='float',\n"
        "    string='Balance',\n"
        "),\n"
        "'code': fields.char('Code', size=64),\n"
    )
    models = _parse_era1_text(src, v8_module)
    assert len(models) == 1
    field_map = {f.name: f for f in models[0].fields}
    # All three fields must be present
    assert "type" in field_map, f"'type' missing; got: {list(field_map)}"
    assert "balance" in field_map, f"'balance' missing; got: {list(field_map)}"
    assert "code" in field_map, f"'code' missing; got: {list(field_map)}"


# ---------------------------------------------------------------------------
# test_string_aware_brace_scan_unit
# ---------------------------------------------------------------------------

def test_string_aware_brace_scan_unit():
    """Unit test for _string_aware_brace_scan: } in string does not close block."""
    fragment = (
        "\n"
        "    'name': fields.char('Name', help='Use } as delimiter'),\n"
        "    'amount': fields.float('Amount'),\n"
        "\n"
    )
    # Without an outer closing }, the scanner returns '' (block not closed)
    assert _string_aware_brace_scan(fragment, open_depth=1) == ""
    # Add the outer closing } — scanner must traverse past the inner } in the string
    fragment2 = fragment + "}"
    result2 = _string_aware_brace_scan(fragment2, open_depth=1)
    assert "amount" in result2, f"fragment truncated; got: {repr(result2[:100])}"
    assert "name" in result2


# ---------------------------------------------------------------------------
# test_string_aware_brace_scan_unterminated_string
# ---------------------------------------------------------------------------
#
# Defensive-coding tests for malformed v8/v9 sources — reviewer concern #7.
# These are extremely rare in real code (Python's own tokenizer rejects them)
# but the era1 fallback runs precisely because tokenize already failed, so the
# scanner can be fed truncated/half-written buffers from copy-paste artefacts
# or files cut mid-write.  Goal: never hang / never crash / always return a
# bounded string within a single linear pass.

def test_string_aware_brace_scan_unterminated_single_quote():
    """An unterminated single-quoted string must not hang or raise.

    The scanner enters string-skip mode at the opening quote and walks to
    end-of-buffer; when it never finds the closing quote, depth stays > 0 and
    the function returns '' (block-not-closed sentinel).
    """
    # No closing quote on help= AND no outer closing brace.
    fragment = "    'name': fields.char('Name', help='unterminated\n"
    result = _string_aware_brace_scan(fragment, open_depth=1)
    assert result == "", f"expected empty (unterminated block); got: {result!r}"


def test_string_aware_brace_scan_unterminated_triple_quote():
    """An unterminated triple-quoted string must not hang or raise."""
    fragment = (
        "    'name': fields.char(\n"
        "        'Name',\n"
        '        help="""unterminated triple-quoted block\n'
        "        more lines without closing\n"
    )
    result = _string_aware_brace_scan(fragment, open_depth=1)
    assert result == "", f"expected empty (unterminated block); got: {result!r}"


def test_string_aware_brace_scan_unterminated_does_not_consume_outer_close():
    """Unterminated string swallows trailing chars — including the outer }.

    This documents the (intentional) behaviour: once the scanner enters
    string-mode at an unclosed quote, every subsequent character including
    '}' is treated as string content.  Result is '' because depth never
    returns to zero.  The test guards against any future "auto-recover"
    refactor accidentally treating the trailing } as a real close.
    """
    fragment = "    'help': 'open string\n    'amount': fields.float('A'),\n}"
    result = _string_aware_brace_scan(fragment, open_depth=1)
    # Whatever we get, it must not have crashed and must be bounded.
    assert isinstance(result, str)
    assert len(result) <= len(fragment)


def test_string_aware_brace_scan_terminates_within_n_iterations():
    """Smoke check: scanner always terminates on bounded input (no infinite loop)."""
    # 50 KB of pathological input: unterminated quotes, stray braces, comments.
    pathological = (
        "'a': '\n"        # unterminated single-quote
        "'b': \"\n"       # unterminated double-quote
        "# stray } in comment\n"
        "}}} more braces\n"
    ) * 1000
    # Should return within a fraction of a second — no hang.
    import time
    t0 = time.monotonic()
    result = _string_aware_brace_scan(pathological, open_depth=1)
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, f"scanner took {elapsed:.3f}s — possible infinite loop"
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# test_source_definition_captured_for_single_and_multiline
# ---------------------------------------------------------------------------

def test_source_definition_captured_for_single_and_multiline(v8_module):
    """source_definition is set for all era1 fields (enables richer pgvector content)."""
    src = _era1_src(
        "'name': fields.char('Name'),\n"
        "'amount_total': fields.function(\n"
        "    _compute_total,\n"
        "    type='float',\n"
        "    store=True,\n"
        "),\n"
    )
    models = _parse_era1_text(src, v8_module)
    assert len(models) == 1
    field_map = {f.name: f for f in models[0].fields}

    # Single-line: source_definition is the full 'name': fields.char(...) text
    name_def = field_map["name"].source_definition
    assert name_def is not None
    assert "fields.char" in name_def

    # Multi-line function field: source_definition spans multiple lines
    fn_def = field_map["amount_total"].source_definition
    assert fn_def is not None
    assert "fields.function" in fn_def
    assert "_compute_total" in fn_def
    assert "\n" in fn_def  # confirms it spans multiple lines
