# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_lint_matcher_unit.py
"""Pure unit tests for _match_lint_rule + V0 banner (PR#11 WI-F6).

No Neo4j required — exercises only the token matching logic and output header.
"""
from src.mcp.server import _LINT_V0_BANNER, _match_lint_rule


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
