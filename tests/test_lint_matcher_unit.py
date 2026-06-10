# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_lint_matcher_unit.py
"""Pure unit tests for _match_lint_rule_lines + V0/V0.5 banner + noqa + pattern-first (WI-8).

No Neo4j required - exercises only the token matching logic, pattern-first path,
match-kind labelling, noqa suppression, and the V0.5 banner constant.

WI-8 additions:
- SQL injection snippet fires W8140 with match_kind 'pattern' (regression for #271).
- UserError string formatting fires W8201 with match_kind 'pattern'.
- Safe parameterized cr.execute does NOT fire W8140 (no false positive).
- Label distinguishes [pattern] from [fuzzy] via _lint_match_kind.
- noqa suppresses pattern hits on the annotated line.
- Invalid regex in code_pattern falls back to fuzzy without crashing.
"""
import json
from pathlib import Path

from src.mcp.server import (
    _LINT_V0_BANNER,
    _build_noqa_suppress,
    _compile_lint_pattern,
    _lint_match_kind,
    _match_lint_rule_lines,
)


def _fires(code: str, rule: dict) -> bool:
    """Whether *rule* fires on *code* (matches on at least one line).

    Replaces the removed deprecated ``_match_lint_rule`` boolean wrapper: the
    SSOT matcher ``_match_lint_rule_lines`` returns the matching line numbers, so
    a non-empty result means the rule fired. Keeps the historical token-overlap
    tests asserting the same fire/no-fire contract.
    """
    return bool(_match_lint_rule_lines(code, rule))

# Load real spec data from the 17.0 JSON so regression tests use the actual
# production patterns rather than hardcoded strings. This ensures tests break
# if the data is edited in a way that breaks the patterns.
_SPEC_DATA_PATH = (
    Path(__file__).parent.parent / "src" / "indexer" / "spec_data" / "lint_rules_17.0.json"
)
_SPEC_17 = json.loads(_SPEC_DATA_PATH.read_text(encoding="utf-8"))
_RULES_BY_ID: dict[str, dict] = {r["rule_id"]: r for r in _SPEC_17.get("rules", [])}


def _rule(message: str, rule_id: str = "E9001", severity: str = "warning") -> dict:
    return {"rule_id": rule_id, "severity": severity, "message": message}


# --- Two-token-overlap requirement ---


def test_lint_match_requires_two_token_overlap_single_token_no_fire():
    """Rule with only 1 significant token in code → must NOT fire."""
    rule = _rule("Do not use concatenation here")
    # Code has 'concatenation' but only 1 significant token overlap
    code = "x = 'foo' + 'bar'  # some concatenation"
    result = _fires(code, rule)
    assert result is False, "Single token overlap must not fire (V0 tighten)"


def test_lint_match_fires_on_two_token_overlap():
    """Rule with 2 significant tokens present in code → must fire."""
    rule = _rule("Bad usage of percent format string literal")
    # Code clearly has 'percent' and 'format' — 2 tokens
    code = "msg = 'Hello %s' % name  # percent format"
    result = _fires(code, rule)
    assert result is True, "Two matching tokens must trigger a match"


def test_lint_match_ignores_stopwords_only_token():
    """Rule message with only stopword tokens → no match even if code has them."""
    rule = _rule("Must use this with that")  # all stopwords
    code = "must use this with that"
    result = _fires(code, rule)
    assert result is False, "Stopwords-only rule must not fire"


def test_lint_match_case_insensitive():
    """Token matching is case-insensitive — uppercase in rule, lowercase in code."""
    rule = _rule("Deprecated: Name_Get returns tuples, display_name preferred")
    code = "result = self.name_get()  # display_name preferred"
    result = _fires(code, rule)
    # 'name_get', 'display_name', 'returns', 'tuples', 'preferred' — multiple significant tokens
    assert result is True


def test_lint_match_empty_message_no_fire():
    """Rule with empty message → must not fire."""
    rule = _rule("")
    code = "x = 1"
    result = _fires(code, rule)
    assert result is False


# --- V0 banner ---


def test_lint_v0_banner_constant_exists():
    """_LINT_V0_BANNER constant must be defined and contain 'V0'."""
    assert _LINT_V0_BANNER is not None
    assert "V0" in _LINT_V0_BANNER or "v0" in _LINT_V0_BANNER.lower()
    assert len(_LINT_V0_BANNER) > 10


# ===========================================================================
# M10A D4 — noqa suppression unit tests
# ===========================================================================


# --- _build_noqa_suppress ---


def test_build_noqa_suppress_specific_rule():
    """Single rule ID on noqa line is parsed correctly."""
    code = "x = foo()  # noqa: E8001"
    suppress = _build_noqa_suppress(code)
    assert suppress == {1: {"E8001"}}, f"Unexpected suppress map: {suppress}"


def test_build_noqa_suppress_multiple_rules():
    """Comma-separated rule IDs on one noqa comment are each captured."""
    code = "x = foo()  # noqa: E8001, W9002"
    suppress = _build_noqa_suppress(code)
    assert suppress == {1: {"E8001", "W9002"}}, f"Unexpected suppress map: {suppress}"


def test_build_noqa_suppress_bare_noqa():
    """Bare noqa (no colon) maps to wildcard '*'."""
    code = "x = foo()  # noqa"
    suppress = _build_noqa_suppress(code)
    assert suppress == {1: {"*"}}, f"Unexpected suppress map: {suppress}"


def test_build_noqa_suppress_multiline():
    """noqa on line 2 only — line 1 must not appear in suppress map."""
    code = "x = bad_percent_format()\ny = another_percent_format()  # noqa: E8001"
    suppress = _build_noqa_suppress(code)
    assert 1 not in suppress, f"Line 1 should not be in suppress map: {suppress}"
    assert 2 in suppress and suppress[2] == {"E8001"}, f"Line 2 wrong: {suppress}"


def test_build_noqa_suppress_no_noqa():
    """Code with no noqa comments returns empty dict."""
    code = "def f():\n    return 1\n"
    suppress = _build_noqa_suppress(code)
    assert suppress == {}, f"Expected empty suppress map, got: {suppress}"


# --- _match_lint_rule_lines ---


def test_match_lint_rule_lines_returns_line_numbers():
    """_match_lint_rule_lines returns 1-based line numbers of matching lines."""
    rule = _rule("Bad usage of percent format string literal")
    code = "x = 1\ny = 'hello %s' % name  # percent format\nz = 3"
    lines = _match_lint_rule_lines(code, rule)
    assert 2 in lines, f"Expected line 2 to match; got: {lines}"


def test_match_lint_rule_lines_no_match_returns_empty():
    """Rule that doesn't match returns empty list."""
    rule = _rule("Completely unrelated message about zebra patterns here")
    code = "x = 1"
    lines = _match_lint_rule_lines(code, rule)
    assert lines == [], f"Expected no match; got: {lines}"


def test_match_lint_rule_lines_fallback_line1_when_tokens_spread():
    """When tokens match across lines but no single line has ≥2, fallback to line 1."""
    rule = _rule("percent format string literal syntax")
    # 'percent' on line 1, 'format' on line 2 — neither line alone triggers
    code = "x = percent_result\ny = format_string"
    lines = _match_lint_rule_lines(code, rule)
    # If whole-code triggers, fallback to [1]; if not, []
    # This just confirms no crash and type is list.
    assert isinstance(lines, list)


# --- Integration: noqa suppresses violations ---


def _make_rule_and_code_with_noqa(noqa_comment: str) -> tuple[dict, str]:
    """Return a (rule, code) pair where the matching line has *noqa_comment*."""
    rule = _rule("Bad usage of percent format string literal", rule_id="E8001")
    code = f"msg = 'Hello %s' % name  # percent format {noqa_comment}"
    return rule, code


def test_noqa_specific_rule_suppresses_matching_line():
    """noqa: E8001 on the matching line — rule must be suppressed."""
    rule, code = _make_rule_and_code_with_noqa("# noqa: E8001")
    from src.mcp.server import _build_noqa_suppress, _match_lint_rule_lines
    suppress = _build_noqa_suppress(code)
    hit_lines = _match_lint_rule_lines(code, rule)
    rule_id = rule["rule_id"]
    suppressed_count = sum(
        1 for ln in hit_lines
        if ln in suppress and ("*" in suppress[ln] or rule_id in suppress[ln])
    )
    assert suppressed_count == len(hit_lines), (
        f"Expected all hit lines suppressed by noqa: E8001; "
        f"hit_lines={hit_lines}, suppress={suppress}"
    )


def test_noqa_bare_suppresses_all_rules():
    """Bare noqa on the matching line suppresses any rule ID."""
    rule, code = _make_rule_and_code_with_noqa("# noqa")
    suppress = _build_noqa_suppress(code)
    hit_lines = _match_lint_rule_lines(code, rule)
    rule_id = rule["rule_id"]
    suppressed_count = sum(
        1 for ln in hit_lines
        if ln in suppress and ("*" in suppress[ln] or rule_id in suppress[ln])
    )
    assert suppressed_count == len(hit_lines), (
        f"Expected bare noqa to suppress all; hit_lines={hit_lines}, suppress={suppress}"
    )


def test_noqa_different_rule_does_not_suppress():
    """noqa: W9002 does NOT suppress rule E8001 on the same line."""
    rule = _rule("Bad usage of percent format string literal", rule_id="E8001")
    code = "msg = 'Hello %s' % name  # percent format  # noqa: W9002"
    suppress = _build_noqa_suppress(code)
    hit_lines = _match_lint_rule_lines(code, rule)
    rule_id = rule["rule_id"]
    suppressed_count = sum(
        1 for ln in hit_lines
        if ln in suppress and ("*" in suppress[ln] or rule_id in suppress[ln])
    )
    assert suppressed_count == 0, (
        f"noqa: W9002 must not suppress E8001; "
        f"hit_lines={hit_lines}, suppress={suppress}"
    )


def test_noqa_on_different_line_does_not_suppress_other_line():
    """noqa on line 2 must not suppress a violation on line 1."""
    rule = _rule("Bad usage of percent format string literal", rule_id="E8001")
    # Line 1 triggers, line 2 has noqa but does NOT trigger
    code = "msg = 'Hello %s' % name  # percent format\nclean = 42  # noqa: E8001"
    suppress = _build_noqa_suppress(code)
    hit_lines = _match_lint_rule_lines(code, rule)
    rule_id = rule["rule_id"]
    # Line 1 should NOT be in suppress
    suppressed_count = sum(
        1 for ln in hit_lines
        if ln in suppress and ("*" in suppress[ln] or rule_id in suppress[ln])
    )
    # Line 1 triggered; noqa is only on line 2 - line 1 should NOT be suppressed
    assert 1 in hit_lines, f"Expected line 1 to be in hit_lines; got {hit_lines}"
    assert 1 not in suppress, f"Line 1 should not be in suppress map; {suppress}"
    assert suppressed_count < len(hit_lines), (
        "Line 1 violation should not be suppressed by line 2 noqa"
    )


# ===========================================================================
# WI-8 Pattern-first regression tests (issue #271 fix)
# ===========================================================================
# These tests use real production patterns from lint_rules_17.0.json and guard
# against regressions in the pattern-first matcher introduced in WI-6.
# Each test MUST be able to fail: if the code_pattern in the JSON is removed or
# broken, the corresponding assertion will fail.


# SQL injection snippet from issue #271 - was false-green under V0 fuzzy matcher.
_SQL_INJECTION_CODE = (
    'self.env.cr.execute("SELECT id FROM res_partner WHERE name = \'%s\'" % self.name)'
)
# Tuple-interpolation form (review PR #275 HIGH #2 / r3 #2): the old `(?!\()`
# lookahead after `%` blocked this equally dangerous shape, false-greening it.
_SQL_INJECTION_TUPLE_CODE = (
    'cr.execute("SELECT id FROM res_partner WHERE id = %s" % (self.id,))'
)
# Safe parameterized variant - must never fire W8140 (no false positive).
_SQL_SAFE_CODE = "cr.execute(\"SELECT id FROM res_partner WHERE id = %s\", (self.id,))"
# UserError string formatting snippet - was false-green under V0 fuzzy matcher.
_USER_ERROR_CODE = "raise UserError('Hi %s' % n)"


def test_pattern_w8140_fires_on_sql_injection():
    """W8140 (SQL injection) must fire on cr.execute with string interpolation.

    Regression for issue #271: V0 fuzzy matcher never fired this rule because
    the rule message vocabulary ('injection', 'interpolation') does not appear
    in the code. Pattern-first matcher uses the real regex from the JSON data.
    """
    rule = _RULES_BY_ID.get("W8140")
    assert rule is not None, "W8140 must be present in lint_rules_17.0.json"
    assert rule.get("code_pattern"), "W8140 must have a non-null code_pattern"

    lines = _match_lint_rule_lines(_SQL_INJECTION_CODE, rule)
    assert len(lines) >= 1, (
        f"W8140 must fire on SQL injection snippet; got no violations.\n"
        f"code_pattern: {rule['code_pattern']!r}\n"
        f"code: {_SQL_INJECTION_CODE!r}"
    )


def test_pattern_w8140_silent_on_safe_parameterized():
    """W8140 must NOT fire on cr.execute with tuple parameters (no false positive).

    The pattern specifically targets string interpolation; parameterized
    queries pass the values as a separate tuple argument, not in the SQL string.
    """
    rule = _RULES_BY_ID.get("W8140")
    assert rule is not None, "W8140 must be present in lint_rules_17.0.json"
    assert rule.get("code_pattern"), "W8140 must have a non-null code_pattern"

    lines = _match_lint_rule_lines(_SQL_SAFE_CODE, rule)
    assert lines == [], (
        f"W8140 must NOT fire on safe parameterized query; got lines={lines}.\n"
        f"code_pattern: {rule['code_pattern']!r}\n"
        f"code: {_SQL_SAFE_CODE!r}"
    )


def test_pattern_w8140_fires_on_tuple_interpolation():
    """W8140 must fire on the tuple-`%` interpolation form (PR #275 HIGH #2).

    `cr.execute("... %s" % (self.id,))` is an equally dangerous SQL-injection
    shape. The pre-fix pattern carried a `(?!\\()` lookahead right after `%`
    which blocked this form because the char after `% ` is `(`. Removing the
    lookahead makes the must-fire set cover single-value AND tuple forms.
    """
    rule = _RULES_BY_ID.get("W8140")
    assert rule is not None, "W8140 must be present in lint_rules_17.0.json"
    assert rule.get("code_pattern"), "W8140 must have a non-null code_pattern"

    lines = _match_lint_rule_lines(_SQL_INJECTION_TUPLE_CODE, rule)
    assert len(lines) >= 1, (
        f"W8140 must fire on tuple-interpolation SQL injection; got no violations.\n"
        f"code_pattern: {rule['code_pattern']!r}\n"
        f"code: {_SQL_INJECTION_TUPLE_CODE!r}"
    )


def test_pattern_e8501_fires_on_tuple_interpolation():
    """E8501 must also fire on the tuple-`%` form (sibling rule of W8140, v17+).

    E8501 duplicates the W8140 branch-0 pattern; the same lookahead removal
    applies. Guards against the two rules drifting apart on this fix.
    """
    rule = _RULES_BY_ID.get("E8501")
    assert rule is not None, "E8501 must be present in lint_rules_17.0.json"
    assert rule.get("code_pattern"), "E8501 must have a non-null code_pattern"

    lines = _match_lint_rule_lines(_SQL_INJECTION_TUPLE_CODE, rule)
    assert len(lines) >= 1, (
        f"E8501 must fire on tuple-interpolation SQL injection; got no violations.\n"
        f"code_pattern: {rule['code_pattern']!r}\n"
        f"code: {_SQL_INJECTION_TUPLE_CODE!r}"
    )


def test_pattern_w8178_fires_on_single_line_unsanitized_html():
    """W8178 must still fire on a single-line `fields.Html(...)` lacking sanitize.

    Recall guard for the multi-line FP fix (PR #275 MED #3): tightening the
    pattern to require `)` on the same line must not lose the single-line case.
    """
    rule = _RULES_BY_ID.get("W8178")
    assert rule is not None, "W8178 must be present in lint_rules_17.0.json"
    assert rule.get("code_pattern"), "W8178 must have a non-null code_pattern"

    code = 'body = fields.Html(string="Body")'
    lines = _match_lint_rule_lines(code, rule)
    assert len(lines) >= 1, (
        f"W8178 must fire on single-line unsanitized fields.Html; got none.\n"
        f"code_pattern: {rule['code_pattern']!r}\ncode: {code!r}"
    )


def test_pattern_w8178_silent_on_multiline_open_line():
    """W8178 must NOT fire on the opening line of a multi-line `fields.Html(`.

    Regression for PR #275 MED #3: the per-line matcher previously flagged every
    multi-line Html field's opening line because the `sanitize=` keyword (and the
    closing `)`) live on a later line, so the negative lookahead vacuously
    succeeded. The fix requires a `)` on the same line for the pattern to apply,
    so a bare opening line no longer fires - including when `sanitize=True`
    appears on a subsequent line.
    """
    rule = _RULES_BY_ID.get("W8178")
    assert rule is not None, "W8178 must be present in lint_rules_17.0.json"
    assert rule.get("code_pattern"), "W8178 must have a non-null code_pattern"

    # The matcher is per-line; the opening line is what previously false-fired.
    multiline = "body = fields.Html(\n    sanitize=True,\n)"
    lines = _match_lint_rule_lines(multiline, rule)
    assert lines == [], (
        f"W8178 must NOT fire on a multi-line fields.Html opening line; got "
        f"lines={lines}.\ncode_pattern: {rule['code_pattern']!r}\ncode: {multiline!r}"
    )


def test_pattern_w8201_fires_on_usererror_format():
    """W8201 (UserError string formatting) must fire on 'raise UserError(... % n)'.

    Regression for issue #271: V0 fuzzy matcher failed because 'usererror'
    appears in the rule message but the code uses CamelCase 'UserError' which
    after lower() becomes 'usererror' - only 1 token matched (below the
    2-token threshold). Pattern-first resolves this deterministically.
    """
    rule = _RULES_BY_ID.get("W8201")
    assert rule is not None, "W8201 must be present in lint_rules_17.0.json"
    assert rule.get("code_pattern"), "W8201 must have a non-null code_pattern"

    lines = _match_lint_rule_lines(_USER_ERROR_CODE, rule)
    assert len(lines) >= 1, (
        f"W8201 must fire on UserError string formatting; got no violations.\n"
        f"code_pattern: {rule['code_pattern']!r}\n"
        f"code: {_USER_ERROR_CODE!r}"
    )


def test_lint_match_kind_pattern_for_rule_with_code_pattern():
    """_lint_match_kind returns 'pattern' when rule has a valid code_pattern."""
    rule = _RULES_BY_ID.get("W8140")
    assert rule is not None, "W8140 must be present in lint_rules_17.0.json"
    assert rule.get("code_pattern"), "W8140 must have a non-null code_pattern"

    kind = _lint_match_kind(rule)
    assert kind == "pattern", (
        f"_lint_match_kind must return 'pattern' for a rule with code_pattern; got {kind!r}"
    )


def test_lint_match_kind_fuzzy_for_rule_without_code_pattern():
    """_lint_match_kind returns 'fuzzy' when rule has no code_pattern."""
    rule = _rule("Some rule message without pattern", rule_id="W9999")
    # No 'code_pattern' key -> falls back to fuzzy.
    kind = _lint_match_kind(rule)
    assert kind == "fuzzy", (
        f"_lint_match_kind must return 'fuzzy' for a rule without code_pattern; got {kind!r}"
    )


def test_noqa_suppresses_pattern_hit():
    """noqa: W8140 on the SQL injection line suppresses the pattern-match violation.

    noqa suppression must work for pattern hits, not just fuzzy hits.
    """
    rule = _RULES_BY_ID.get("W8140")
    assert rule is not None, "W8140 must be present in lint_rules_17.0.json"

    code = _SQL_INJECTION_CODE + "  # noqa: W8140"
    suppress = _build_noqa_suppress(code)
    hit_lines = _match_lint_rule_lines(code, rule)
    rule_id = rule["rule_id"]

    # Without suppression the pattern must have fired (sanity guard).
    raw_lines = _match_lint_rule_lines(_SQL_INJECTION_CODE, rule)
    assert raw_lines, "Prerequisite: W8140 must fire before noqa is applied"

    # All hit lines should be covered by the noqa annotation.
    suppressed_count = sum(
        1 for ln in hit_lines
        if ln in suppress and ("*" in suppress[ln] or rule_id in suppress[ln])
    )
    assert suppressed_count == len(hit_lines), (
        f"All pattern hits must be suppressed by '# noqa: W8140'; "
        f"hit_lines={hit_lines}, suppress={suppress}"
    )


def test_invalid_code_pattern_falls_back_to_fuzzy_no_crash():
    """A rule with an invalid regex code_pattern falls back to fuzzy without crashing.

    _compile_lint_pattern must cache None for bad patterns so callers get fuzzy
    behaviour rather than an unhandled exception.
    """
    bad_pattern = r"(?invalid-regex"
    # _compile_lint_pattern must return None, not raise.
    result = _compile_lint_pattern(bad_pattern)
    assert result is None, (
        f"_compile_lint_pattern must return None for invalid regex; got {result!r}"
    )

    # With a bad pattern the rule should fall back to fuzzy (no crash).
    rule_with_bad_pattern = {
        "rule_id": "W9997",
        "severity": "warning",
        "message": "percent format string literal usage",
        "code_pattern": bad_pattern,
    }
    code = "msg = 'Hello %s' % name  # percent format"
    # Must not raise; result is either [] or a list from fuzzy fallback.
    lines = _match_lint_rule_lines(code, rule_with_bad_pattern)
    assert isinstance(lines, list), (
        f"_match_lint_rule_lines must return a list even with bad code_pattern; got {lines!r}"
    )
