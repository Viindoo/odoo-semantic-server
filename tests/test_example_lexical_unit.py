# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the lexical-fallback tokeniser (`_extract_keywords`).

DB-free. Protects the tokeniser's business contract (keep >=3-char non-stopword
tokens, deduped, order-preserving) plus a linear-time guard for the tokenising
regex (N6 — `[^\\w.]+` is a single negated class with one quantifier, so it
cannot catastrophically backtrack; this test pins that property so a future
"smarter" regex with nested quantifiers / alternation would be caught).
"""
import time

from src.mcp.example_lexical import _extract_keywords


def test_keeps_long_tokens_drops_short_and_stopwords():
    """>=3-char non-stopword tokens survive; short tokens + stopwords are dropped."""
    out = _extract_keywords("the sale order is confirmed by a user")
    # 'the', 'is', 'by', 'a' are stopwords; all real tokens >=3 chars survive.
    assert "sale" in out and "order" in out and "confirmed" in out and "user" in out
    assert "the" not in out and "by" not in out and "is" not in out
    # 'a' is < 3 chars -> dropped.
    assert "a" not in out


def test_dedup_preserves_first_occurrence_order():
    """Duplicate tokens collapse to the first occurrence; order is preserved."""
    out = _extract_keywords("order ORDER sale order")
    assert out == ["order", "sale"], f"Expected deduped order-preserving list, got {out}"


def test_dotted_path_token_is_split_and_trimmed():
    """Dots are kept inside tokens but stripped at the edges (partner_id.country_id)."""
    out = _extract_keywords("partner_id.country_id")
    # The dot is in the token class, so the dotted path stays one token (trimmed).
    assert out == ["partner_id.country_id"], f"Got {out}"


def test_empty_after_tokenisation_returns_empty_list():
    """All-stopword / punctuation-only input yields []."""
    assert _extract_keywords("the a by is !!! ...") == []


def test_pathological_input_completes_fast():
    """N6: the tokeniser regex is linear — a long adversarial string must not hang.

    `[^\\w.]+` has no nested quantifier or overlapping alternation, so there is no
    catastrophic backtracking. A 200k-char string mixing the worst case (runs of
    delimiters next to word chars) must tokenise well under a generous bound.

    Fail-able: replace the regex with a backtracking-prone pattern (e.g.
    `([^\\w.]+)*`) and this assertion blows past the time budget.
    """
    pathological = ("a! " * 50_000) + ("." * 50_000)  # ~200k chars
    start = time.perf_counter()
    out = _extract_keywords(pathological)
    elapsed = time.perf_counter() - start
    # Generous ceiling: linear tokenisation of 200k chars is milliseconds; a
    # backtracking regex would take seconds-to-minutes. 2.0s leaves huge headroom
    # while still catching a true ReDoS regression.
    assert elapsed < 2.0, f"Tokeniser took {elapsed:.3f}s on pathological input (possible ReDoS)"
    # 'a!' repeats collapse to the single <3-char token 'a' (dropped) -> empty.
    assert out == []
