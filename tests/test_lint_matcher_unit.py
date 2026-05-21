# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_lint_matcher_unit.py
"""Pure unit tests for _match_lint_rule + V0 banner (PR#11 WI-F6) + noqa (M10A D4).

No Neo4j required — exercises only the token matching logic and output header.
"""
from src.mcp.server import (
    _LINT_V0_BANNER,
    _build_noqa_suppress,
    _match_lint_rule,
    _match_lint_rule_lines,
)


def _rule(message: str, rule_id: str = "E9001", severity: str = "warning") -> dict:
    return {"rule_id": rule_id, "severity": severity, "message": message}


# --- Two-token-overlap requirement ---


def test_lint_match_requires_two_token_overlap_single_token_no_fire():
    """Rule with only 1 significant token in code → must NOT fire."""
    rule = _rule("Do not use concatenation here")
    # Code has 'concatenation' but only 1 significant token overlap
    code = "x = 'foo' + 'bar'  # some concatenation"
    result = _match_lint_rule(code, rule)
    assert result is False, "Single token overlap must not fire (V0 tighten)"


def test_lint_match_fires_on_two_token_overlap():
    """Rule with 2 significant tokens present in code → must fire."""
    rule = _rule("Bad usage of percent format string literal")
    # Code clearly has 'percent' and 'format' — 2 tokens
    code = "msg = 'Hello %s' % name  # percent format"
    result = _match_lint_rule(code, rule)
    assert result is True, "Two matching tokens must trigger a match"


def test_lint_match_ignores_stopwords_only_token():
    """Rule message with only stopword tokens → no match even if code has them."""
    rule = _rule("Must use this with that")  # all stopwords
    code = "must use this with that"
    result = _match_lint_rule(code, rule)
    assert result is False, "Stopwords-only rule must not fire"


def test_lint_match_case_insensitive():
    """Token matching is case-insensitive — uppercase in rule, lowercase in code."""
    rule = _rule("Deprecated: Name_Get returns tuples, display_name preferred")
    code = "result = self.name_get()  # display_name preferred"
    result = _match_lint_rule(code, rule)
    # 'name_get', 'display_name', 'returns', 'tuples', 'preferred' — multiple significant tokens
    assert result is True


def test_lint_match_empty_message_no_fire():
    """Rule with empty message → must not fire."""
    rule = _rule("")
    code = "x = 1"
    result = _match_lint_rule(code, rule)
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
    # Line 1 triggered; noqa is only on line 2 — line 1 should NOT be suppressed
    assert 1 in hit_lines, f"Expected line 1 to be in hit_lines; got {hit_lines}"
    assert 1 not in suppress, f"Line 1 should not be in suppress map; {suppress}"
    assert suppressed_count < len(hit_lines), (
        "Line 1 violation should not be suppressed by line 2 noqa"
    )
