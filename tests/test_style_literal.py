# SPDX-License-Identifier: AGPL-3.0-or-later
"""tests/test_style_literal.py — Pure-unit tests for src/mcp/style_literal.py.

No DB, no Docker, no embedder required.  All tests are deterministic.

Covers:
  - is_literal_token: comprehensive decision table incl. M3/M4 edge cases
    (compound selectors, pseudo/attribute selectors, at-rule flood guard,
    LESS variables, plain English words).
  - literal_column: column routing (entity_name vs content) by token shape.
  - ilike_pattern: LIKE metacharacter escaping (_, %, backslash).
"""
from __future__ import annotations

from src.mcp.style_literal import ilike_pattern, is_literal_token, literal_column

# ---------------------------------------------------------------------------
# is_literal_token — comprehensive decision table
# ---------------------------------------------------------------------------

class TestIsLiteralToken:
    """Decision table: input -> expected bool."""

    # --- Clear literal cases (AC1/AC2 paths) ---

    def test_dot_selector(self):
        """Standard Odoo class selector — must be True."""
        assert is_literal_token(".o_list_view") is True

    def test_dot_selector_with_pseudo(self):
        """.btn:hover starts with '.', no spaces — True."""
        assert is_literal_token(".btn:hover") is True

    def test_id_selector(self):
        """#wrapper — id selector, True."""
        assert is_literal_token("#wrapper") is True

    def test_scss_variable(self):
        """$o-brand-primary — SCSS variable, True."""
        assert is_literal_token("$o-brand-primary") is True

    def test_less_variable(self):
        """@brand-primary — LESS variable, True."""
        assert is_literal_token("@brand-primary") is True

    def test_bare_ident_with_hyphen(self):
        """o-flex-center — BEM mixin with hyphen, True."""
        assert is_literal_token("o-flex-center") is True

    def test_bare_ident_with_underscore(self):
        """o_list_view — bare class name with underscore, True."""
        assert is_literal_token("o_list_view") is True

    # --- M3: compound / descendant / combinator selectors ---

    def test_descendant_selector(self):
        """.a .b — descendant selector (every token selector-shaped), True."""
        assert is_literal_token(".a .b") is True

    def test_child_combinator_selector(self):
        """.a > .b — child combinator, every token selector-shaped, True."""
        assert is_literal_token(".a > .b") is True

    def test_scss_parent_ref(self):
        """&:hover — SCSS parent reference, True."""
        assert is_literal_token("&:hover") is True

    def test_attribute_selector(self):
        """[type=text] — CSS attribute selector, True."""
        assert is_literal_token("[type=text]") is True

    def test_attribute_selector_quoted(self):
        """[class~=foo] — attribute selector with ~=, True."""
        assert is_literal_token('[class~="foo"]') is True

    # --- M4: at-rule flood guard ---

    def test_at_media_bare_false(self):
        """@media (bare, no spaces) — known at-rule keyword, False."""
        assert is_literal_token("@media") is False

    def test_at_import_false(self):
        """@import — known at-rule, False."""
        assert is_literal_token("@import") is False

    def test_at_supports_false(self):
        """@supports — known at-rule, False."""
        assert is_literal_token("@supports") is False

    def test_at_keyframes_false(self):
        """@keyframes — known at-rule, False."""
        assert is_literal_token("@keyframes") is False

    def test_at_font_face_false(self):
        """@font-face — known at-rule, False (exact keyword match)."""
        assert is_literal_token("@font-face") is False

    def test_at_brand_primary_true(self):
        """@brand-primary — LESS variable (not a keyword at-rule), True."""
        assert is_literal_token("@brand-primary") is True

    def test_at_odoo_var_true(self):
        """@o-main-color — another LESS variable, True."""
        assert is_literal_token("@o-main-color") is True

    # --- Clearly not-literal (NL phrases, plain words) ---

    def test_nl_phrase_with_spaces(self):
        """NL phrase with spaces — False."""
        assert is_literal_token("primary button color scss variable") is False

    def test_nl_phrase_at_media_with_condition(self):
        """@media screen and (max-width: 600px) — NL at-rule query, False (has spaces)."""
        assert is_literal_token("@media screen and (max-width: 600px)") is False

    def test_plain_word_button(self):
        """button — plain English word with no CSS separator, False."""
        assert is_literal_token("button") is False

    def test_plain_word_color(self):
        """color — plain English word, False."""
        assert is_literal_token("color") is False

    def test_empty_string(self):
        """Empty string — False."""
        assert is_literal_token("") is False

    def test_whitespace_only(self):
        """Whitespace-only string — False."""
        assert is_literal_token("   ") is False

    # --- Lone sigil edge cases ---

    def test_lone_dot(self):
        """'.' alone — too short, False."""
        assert is_literal_token(".") is False

    def test_lone_dollar(self):
        """'$' alone — lone sigil, False."""
        assert is_literal_token("$") is False

    def test_lone_at(self):
        """'@' alone — lone sigil, False."""
        assert is_literal_token("@") is False

    def test_lone_hash(self):
        """'#' alone — lone sigil, False."""
        assert is_literal_token("#") is False

    # --- Compound with non-selector-shaped tokens -> False ---

    def test_compound_with_nl_token(self):
        """.o_list_view primary — second token not selector-shaped, False."""
        assert is_literal_token(".o_list_view primary") is False


# ---------------------------------------------------------------------------
# literal_column — routing decision
# ---------------------------------------------------------------------------

class TestLiteralColumn:
    """Column routing: entity_name for selectors, content for variables."""

    def test_dot_selector_routes_entity_name(self):
        assert literal_column(".o_list_view") == "entity_name"

    def test_hash_selector_routes_entity_name(self):
        assert literal_column("#wrapper") == "entity_name"

    def test_bare_ident_routes_entity_name(self):
        assert literal_column("o-flex-center") == "entity_name"

    def test_scss_variable_routes_content(self):
        """$o-brand-primary — variable, routes to content."""
        assert literal_column("$o-brand-primary") == "content"

    def test_less_variable_routes_content(self):
        """@brand-primary — LESS variable, routes to content."""
        assert literal_column("@brand-primary") == "content"

    def test_attribute_selector_routes_entity_name(self):
        assert literal_column("[type=text]") == "entity_name"

    def test_ampersand_routes_entity_name(self):
        assert literal_column("&:hover") == "entity_name"


# ---------------------------------------------------------------------------
# ilike_pattern — LIKE metacharacter escaping
# ---------------------------------------------------------------------------

class TestIlikePattern:
    """Verify escaping of %, _, and backslash."""

    def test_underscore_escaped(self):
        """Underscores must be escaped so .o_list_view != .oXlist_view."""
        result = ilike_pattern(".o_list_view")
        # Each _ becomes \_
        assert result == r"%.o\_list\_view%"

    def test_percent_escaped(self):
        """% must be escaped (LIKE wildcard)."""
        result = ilike_pattern("a%b")
        assert result == r"%a\%b%"

    def test_backslash_escaped(self):
        """Backslash must be doubled (escape character)."""
        result = ilike_pattern("a\\b")
        assert result == "%a\\\\b%"

    def test_plain_selector_wrapped(self):
        """Simple selector wrapped with %...%."""
        result = ilike_pattern(".o_form_view")
        assert result.startswith("%")
        assert result.endswith("%")
        assert ".o" in result

    def test_variable_wrapped(self):
        """Variable pattern includes the $ sigil."""
        result = ilike_pattern("$o-brand-primary")
        assert "$o-brand-primary" in result

    def test_strips_whitespace(self):
        """Leading/trailing whitespace is stripped before building pattern."""
        result = ilike_pattern("  .o_list_view  ")
        assert result == r"%.o\_list\_view%"
