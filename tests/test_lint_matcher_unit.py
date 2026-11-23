# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_lint_matcher_unit.py
"""Pure unit tests for the lint_check line-pattern matcher, noqa and rendering.

No Neo4j required. Business rules:

- A rule fires only where its ``code_pattern`` regex matches a line; a rule
  without a compilable pattern never fires and is reported as not checked
  (matching on the rule message flagged correct code: a correct ``write()``
  override calling ``super()`` was reported as not calling it).
- Correct model code raises no finding against the real 17.0-20.0 catalogues.
- Real security/API patterns fire on the violating shape and stay quiet on the
  safe one (SQL injection, UserError formatting, sanitize=False, regressions of
  #271 and PR #275).
- ``# noqa`` suppresses a hit on its own line only.
- The rendered tree ends with the count of rules not checked.

issue #364 B5 note: the SQL-injection pattern used to be curated TWICE at
v17.0 - once as the OSM-local `W8140` and once as the real Odoo-vendored
`E8501` (`_odoo_checker_sql_injection.py`) - causing `lint_check` to
double-report one defect. The fix consolidated onto the real upstream id E8501
from v14.0 onward (`W8140` removed there; kept only at v8.0-v13.0, where no
live E8501 oracle is wired).
"""
import json
from pathlib import Path

import pytest

from src.mcp.server import (
    _LINT_BANNER,
    _build_noqa_suppress,
    _compile_lint_pattern,
    _match_lint_rule_lines,
)
from src.mcp.tools.spec import _format_lint_check, _lint_rule_pattern

_SPEC_DIR = Path(__file__).parent.parent / "src" / "indexer" / "spec_data"


def _catalogue(version: str) -> dict[str, dict]:
    data = json.loads((_SPEC_DIR / f"lint_rules_{version}.json").read_text(encoding="utf-8"))
    return {r["rule_id"]: r for r in data.get("rules", [])}


# Real spec data so the regression tests break if a production pattern is
# edited in a way that changes what it detects.
_RULES_BY_ID = _catalogue("17.0")
_RULES_V20_BY_ID = _catalogue("20.0")


def _rule(rule_id: str = "E9001", pattern: str | None = None, message: str = "") -> dict:
    rule = {"rule_id": rule_id, "severity": "warning", "message": message}
    if pattern is not None:
        rule["code_pattern"] = pattern
    return rule


def _suppressed_count(code: str, rule: dict) -> tuple[list[int], int]:
    suppress = _build_noqa_suppress(code)
    hit_lines = _match_lint_rule_lines(code, rule)
    rule_id = rule["rule_id"]
    count = sum(
        1 for ln in hit_lines
        if ln in suppress and ("*" in suppress[ln] or rule_id in suppress[ln])
    )
    return hit_lines, count


# --- rules without a line pattern are never guessed ---


def test_rule_without_pattern_never_fires_even_when_code_shares_its_words():
    """A rule with no code_pattern must not fire on code that repeats its message."""
    rule = _rule("W8162", message="`write()` override does not call `super()`.")
    code = "def write(self, vals):\n    return super().write(vals)  # write override super"
    assert _match_lint_rule_lines(code, rule) == []
    assert _lint_rule_pattern(rule) is None


_CORRECT_EXTENSION = '''from odoo import api, models


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def action_confirm(self):
        return super().action_confirm()
'''

_CORRECT_CRUD_OVERRIDES = '''class ResPartner(models.Model):
    _inherit = "res.partner"

    @api.model_create_multi
    def create(self, vals_list):
        return super().create(vals_list)

    def write(self, vals):
        return super().write(vals)

    def copy(self, default=None):
        return super().copy(default)

    def unlink(self):
        return super().unlink()
'''


@pytest.mark.parametrize("version", ["17.0", "18.0", "19.0", "20.0"])
@pytest.mark.parametrize("code", [_CORRECT_EXTENSION, _CORRECT_CRUD_OVERRIDES],
                         ids=["inherit-extension", "crud-overrides-calling-super"])
def test_correct_model_code_raises_no_finding(version, code):
    """An `_inherit` extension and CRUD overrides that call super() are correct code."""
    python_rules = [
        r for r in _catalogue(version).values()
        if (r.get("kind") or "").startswith("pylint")
    ]
    hits = [r["rule_id"] for r in python_rules if _match_lint_rule_lines(code, r)]
    assert hits == [], f"correct code flagged at {version}: {hits}"


_NEW_API_MODEL = '''from odoo import fields, models


class SaleOrder(models.Model):
    _inherit = "sale.order"

    note = fields.Char()
'''


@pytest.mark.parametrize("version", ["10.0", "11.0", "12.0", "13.0", "14.0", "15.0", "16.0"])
def test_new_api_model_is_not_reported_as_old_api(version):
    """A `models.Model` class with `fields.*` attributes is the v10+ API, not `_columns`."""
    python_rules = [
        r for r in _catalogue(version).values()
        if (r.get("kind") or "").startswith("pylint")
    ]
    hits = [r["rule_id"] for r in python_rules if _match_lint_rule_lines(_NEW_API_MODEL, r)]
    assert hits == [], f"new-API model flagged at {version}: {hits}"


def test_the_replacement_cli_flag_is_not_reported_as_the_removed_one():
    """`--gevent-port` is the 18.0 replacement of `--longpolling-port` (W8212)."""
    assert _match_lint_rule_lines("odoo-bin --gevent-port 8072", _catalogue("18.0")["W8212"]) == []


def test_invalid_code_pattern_is_not_checked_and_does_not_crash():
    """A rule whose regex does not compile is reported as not checked, never raised."""
    bad_pattern = r"(?invalid-regex"
    assert _compile_lint_pattern(bad_pattern) is None
    rule = _rule("W9997", pattern=bad_pattern, message="percent format string literal usage")
    assert _lint_rule_pattern(rule) is None
    assert _match_lint_rule_lines("msg = 'Hello %s' % name  # percent format", rule) == []


# --- line numbers ---


def test_match_lint_rule_lines_returns_line_numbers():
    """The matcher returns the 1-based numbers of the lines the pattern matches."""
    rule = _rule(pattern=r"%\s*name")
    code = "x = 1\ny = 'hello %s' % name\nz = 3\nw = '%s' % name"
    assert _match_lint_rule_lines(code, rule) == [2, 4]


def test_match_lint_rule_lines_no_match_returns_empty():
    assert _match_lint_rule_lines("x = 1", _rule(pattern=r"\bzebra\b")) == []


# --- noqa ---


def test_build_noqa_suppress_specific_rule():
    assert _build_noqa_suppress("x = foo()  # noqa: E8001") == {1: {"E8001"}}


def test_build_noqa_suppress_multiple_rules():
    assert _build_noqa_suppress("x = foo()  # noqa: E8001, W9002") == {1: {"E8001", "W9002"}}


def test_build_noqa_suppress_bare_noqa():
    assert _build_noqa_suppress("x = foo()  # noqa") == {1: {"*"}}


def test_build_noqa_suppress_multiline():
    suppress = _build_noqa_suppress("x = bad()\ny = other()  # noqa: E8001")
    assert 1 not in suppress
    assert suppress[2] == {"E8001"}


def test_build_noqa_suppress_no_noqa():
    assert _build_noqa_suppress("def f():\n    return 1\n") == {}


_PERCENT_RULE = _rule("E8001", pattern=r"%\s*name")


def test_noqa_specific_rule_suppresses_matching_line():
    code = "msg = 'Hello %s' % name  # noqa: E8001"
    hit_lines, suppressed = _suppressed_count(code, _PERCENT_RULE)
    assert hit_lines and suppressed == len(hit_lines)


def test_noqa_bare_suppresses_all_rules():
    hit_lines, suppressed = _suppressed_count("msg = 'Hello %s' % name  # noqa", _PERCENT_RULE)
    assert hit_lines and suppressed == len(hit_lines)


def test_noqa_different_rule_does_not_suppress():
    code = "msg = 'Hello %s' % name  # noqa: W9002"
    hit_lines, suppressed = _suppressed_count(code, _PERCENT_RULE)
    assert hit_lines and suppressed == 0


def test_noqa_on_different_line_does_not_suppress_other_line():
    code = "msg = 'Hello %s' % name\nclean = 42  # noqa: E8001"
    hit_lines, suppressed = _suppressed_count(code, _PERCENT_RULE)
    assert hit_lines == [1]
    assert suppressed == 0


def test_noqa_suppresses_pattern_hit():
    """noqa: E8501 on the SQL injection line suppresses the real E8501 hit."""
    rule = _RULES_BY_ID["E8501"]
    assert _match_lint_rule_lines(_SQL_INJECTION_CODE, rule), "E8501 must fire before noqa"
    hit_lines, suppressed = _suppressed_count(_SQL_INJECTION_CODE + "  # noqa: E8501", rule)
    assert hit_lines and suppressed == len(hit_lines)


# ===========================================================================
# Real production patterns (issue #271 / PR #275 regressions)
# ===========================================================================

_SQL_INJECTION_CODE = (
    'self.env.cr.execute("SELECT id FROM res_partner WHERE name = \'%s\'" % self.name)'
)
# Tuple-interpolation form (review PR #275 HIGH #2 / r3 #2): the old `(?!\()`
# lookahead after `%` blocked this equally dangerous shape, false-greening it.
_SQL_INJECTION_TUPLE_CODE = (
    'cr.execute("SELECT id FROM res_partner WHERE id = %s" % (self.id,))'
)
_SQL_SAFE_CODE = "cr.execute(\"SELECT id FROM res_partner WHERE id = %s\", (self.id,))"
_USER_ERROR_CODE = "raise UserError('Hi %s' % n)"


def _pattern_rule(catalogue: dict[str, dict], rule_id: str) -> dict:
    rule = catalogue.get(rule_id)
    assert rule is not None, f"{rule_id} must be present in the catalogue"
    assert rule.get("code_pattern"), f"{rule_id} must have a non-null code_pattern"
    return rule


def test_pattern_e8501_fires_on_sql_injection():
    """E8501 (SQL injection) fires on cr.execute with string interpolation (#271)."""
    assert _match_lint_rule_lines(_SQL_INJECTION_CODE, _pattern_rule(_RULES_BY_ID, "E8501"))


def test_pattern_e8501_silent_on_safe_parameterized():
    """E8501 stays quiet on a parameterized query (values passed as a tuple)."""
    assert _match_lint_rule_lines(_SQL_SAFE_CODE, _pattern_rule(_RULES_BY_ID, "E8501")) == []


def test_pattern_e8501_fires_on_tuple_interpolation():
    """E8501 fires on the tuple-`%` interpolation form (PR #275 HIGH #2)."""
    assert _match_lint_rule_lines(_SQL_INJECTION_TUPLE_CODE, _pattern_rule(_RULES_BY_ID, "E8501"))


def test_pattern_w8229_fires_on_single_line_html_with_sanitize_disabled():
    """W8229 fires on a single-line `fields.Html(..., sanitize=False)`."""
    code = 'body = fields.Html(string="Body", sanitize=False)'
    assert _match_lint_rule_lines(code, _pattern_rule(_RULES_V20_BY_ID, "W8229"))


def test_pattern_w8229_silent_on_multiline_open_line_with_sanitize_on():
    """W8229 stays quiet on the opening line of a multi-line `fields.Html(` (PR #275 MED #3)."""
    multiline = "body = fields.Html(\n    sanitize=True,\n)"
    assert _match_lint_rule_lines(multiline, _pattern_rule(_RULES_V20_BY_ID, "W8229")) == []


def test_pattern_w8201_fires_on_usererror_format():
    """W8201 (UserError string formatting) fires on 'raise UserError(... % n)' (#271)."""
    assert _match_lint_rule_lines(_USER_ERROR_CODE, _pattern_rule(_RULES_BY_ID, "W8201"))


# ===========================================================================
# Rendering
# ===========================================================================


def _tree(out: str) -> list[str]:
    return [ln for ln in out.splitlines() if ln.startswith(("├─", "└─"))]


def test_rendered_findings_carry_the_pattern_label():
    out = _format_lint_check([_RULES_BY_ID["E8501"]], "17.0", _SQL_INJECTION_CODE)
    assert out.splitlines()[0] == _LINT_BANNER
    assert "[pattern] E8501" in _tree(out)[-1]
    assert _tree(out)[-1].startswith("└─")


def test_rendered_tree_ends_with_the_rules_not_checked():
    """Rules without a line pattern are disclosed: 'no violations' is not a clean bill."""
    out = _format_lint_check([], "20.0", "x = 1", unchecked=23, total=58)
    tree = _tree(out)
    assert tree[-2] == "├─ no violations"
    assert tree[-1] == "└─ Not checked: 23 of 58 rules have no line pattern"


def test_rendered_tree_has_no_not_checked_line_when_every_rule_is_checked():
    out = _format_lint_check([], "20.0", "x = 1", unchecked=0, total=4)
    assert _tree(out)[-1] == "└─ no violations"
    assert "Not checked" not in out
