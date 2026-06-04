# SPDX-License-Identifier: AGPL-3.0-or-later
"""Literal CSS-selector / SCSS-variable detection + ILIKE lookup (issue #255).

Pure helper: no DB connection, no embedder, no pipeline peers imported.
server.py owns the cursor + RLS transaction; this module only decides
"is this a literal token?" and "which column to search?" and builds the
ILIKE pattern.

Column routing (M2 — Decision A revised):
  selector tokens (./#/bare-ident/[.../&/combinators) -> entity_name ILIKE
  variable tokens  ($/@)                               -> content ILIKE
  For SCSS variables the writer stores the individual var name only in content
  (entity_name = 'variable:{stem}:variables'); for selectors the name appears
  in entity_name for both css (raw) and scss/less (selector: prefix).
"""
import re

# Prefix characters that unambiguously signal a literal style token.
_SELECTOR_PREFIXES = (".", "#", "[", "&", "*", ">", "~", "+", ":")
_VARIABLE_PREFIXES = ("$", "@")

# At-rule keywords that should NOT be treated as literal tokens because they
# produce noisy flood results (matches every media block, every @import line,
# etc.).  LESS variable names that happen to look like at-rules are typically
# not in this set (e.g. @brand-primary).
_AT_RULE_KEYWORDS: frozenset[str] = frozenset({
    "@media", "@import", "@supports", "@keyframes", "@font-face",
    "@charset", "@namespace", "@layer", "@container",
})

# Bare BEM/utility identifier pattern (no leading sigil).
# Must contain a hyphen or underscore (CSS separator) — so plain English words
# like "button" or "color" fall through to semantic search, while mixin names
# like "o-flex-center" and utility classes like "o_list_view" are literal.
# Length capped at 64 to exclude NL phrases that happen to match.
_BARE_IDENT_RE = re.compile(r"^[A-Za-z_][\w-]{1,63}$")

# Selector charset for tokens in compound / descendant selectors.
# A whitespace-split token is "selector-shaped" when it starts with a known
# selector prefix, is a combinator, or matches the bare-ident pattern.
_CSS_COMBINATORS: frozenset[str] = frozenset({">", "~", "+", "||"})


def _is_selector_token(tok: str) -> bool:
    """True if a single whitespace-split token looks like a CSS selector piece."""
    if tok in _CSS_COMBINATORS:
        return True
    if tok.startswith(_SELECTOR_PREFIXES) or tok.startswith(_VARIABLE_PREFIXES):
        return len(tok) > 1
    return bool(_BARE_IDENT_RE.match(tok)) and ("-" in tok or "_" in tok)


def is_literal_token(text: str) -> bool:
    """True if *text* looks like a verbatim CSS/SCSS token, not an NL description.

    Decision rules (M3/M4 from DEBATE.md):

    1. Empty string -> False.
    2. Known at-rule keywords (@media, @import, ...) -> False  (flood guard M4).
    3. Variable prefix ($/@) with no internal spaces -> True   (AC2 path).
    4. Selector prefix (./#/[/&/*/>/~/+/:) with no internal spaces -> True.
    5. Compound/descendant selector (.a .b, .btn > .icon, &:hover) where EVERY
       whitespace-split token is selector-shaped -> True  (M3 broadening).
    6. Bare BEM/utility ident with a hyphen or underscore (mixin o-flex-center,
       utility o_list_view) -> True.
    7. Anything else (plain words, NL phrases) -> False.
    """
    t = text.strip()
    if not t:
        return False

    # At-rule flood guard (M4): reject well-known at-rule keywords verbatim.
    if t.lower() in _AT_RULE_KEYWORDS:
        return False

    # Variable tokens ($..., @...) — must have no spaces.
    if t[0] in ("$", "@"):
        return " " not in t and len(t) > 1

    # Single-token selector prefixes — no spaces allowed.
    if t[0] in ("[", "&", "*", ">", "~", "+", ":"):
        return " " not in t and len(t) > 1

    if t[0] in (".", "#"):
        if " " not in t:
            return len(t) > 1
        # Compound / descendant selector: every token must be selector-shaped.
        parts = t.split()
        return all(_is_selector_token(p) for p in parts)

    # Bare BEM/utility identifier (no leading sigil) — must have CSS separator.
    return bool(_BARE_IDENT_RE.match(t)) and ("-" in t or "_" in t)


def literal_column(text: str) -> str:
    """Return the SQL column to ILIKE-match for a literal style token.

    Routing (M2 Decision A revised):
      - Variable tokens ($..., @...)  -> 'content'
        (var names only appear in the definition chunk's content field)
      - Everything else (selectors, mixins, bare-idents) -> 'entity_name'
        (selector name appears in entity_name for both css-raw and scss/less
         with 'selector:' prefix; content ILIKE would over-match usage sites)

    Args:
        text: A token already confirmed literal by is_literal_token().

    Returns:
        Either 'entity_name' or 'content'.
    """
    t = text.strip()
    if t and t[0] in ("$", "@"):
        return "content"
    return "entity_name"


def ilike_pattern(text: str) -> str:
    """Build a safe substring ILIKE pattern for *text*.

    Escapes LIKE metacharacters (%, _, and backslash) so that underscores in
    class names like .o_list_view do NOT act as single-character wildcards.
    The companion SQL must include ``ESCAPE '\\'``.

    Args:
        text: The raw selector or variable string (e.g. '.o_list_view').

    Returns:
        A pattern string like '%.o\\_list\\_view%' safe for parameterised SQL.
    """
    esc = text.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"
