# SPDX-License-Identifier: AGPL-3.0-or-later
"""Schema validation guards for src/data/patterns.json (per ADR-0009)."""
import json
import re
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent.parent / "src" / "data" / "patterns.schema.json"
DATA_PATH = Path(__file__).parent.parent / "src" / "data" / "patterns.json"


def _load_patterns_list() -> list[dict]:
    """Return the list of patterns regardless of root-array or {patterns: [...]} shape."""
    raw = json.loads(DATA_PATH.read_text())
    return raw if isinstance(raw, list) else raw["patterns"]


def test_patterns_schema_valid():
    """Entire patterns.json validates against schema."""
    from jsonschema import Draft202012Validator
    schema = json.loads(SCHEMA_PATH.read_text())
    data = json.loads(DATA_PATH.read_text())
    Draft202012Validator(schema).validate(data)


def test_pattern_ids_unique():
    """No duplicate pattern_id across the catalogue."""
    ids = [p["pattern_id"] for p in _load_patterns_list()]
    assert len(ids) == len(set(ids)), f"Duplicates: {[i for i in ids if ids.count(i) > 1]}"


def test_pattern_ids_kebab_case():
    """Every pattern_id matches kebab-case regex."""
    pat = re.compile(r"^[a-z][a-z0-9-]*$")
    bad = [p["pattern_id"] for p in _load_patterns_list() if not pat.match(p["pattern_id"])]
    assert not bad, f"Non-kebab-case IDs: {bad}"


def test_no_enterprise_references():
    """No Odoo Enterprise references in snippet_text or gotchas."""
    import re

    from src.data.ee_modules import EE_CONFUSION

    # Substring-match strings (always literal — no false-positive risk).
    forbidden_substrings = ["enterprise/", "OEEL-1", "OEEL "]

    # Word-boundary needles: module-name tokens that would false-match as
    # substrings (e.g. 'sign' inside 'signature', 'hr' inside 'href').
    forbidden_words = list(EE_CONFUSION.keys())
    forbidden_words.extend(v for v in EE_CONFUSION.values() if v is not None)

    for p in _load_patterns_list():
        haystack = p.get("snippet_text", "") + " " + " ".join(p.get("gotchas", []))
        for needle in forbidden_substrings:
            assert needle not in haystack, (
                f"Pattern {p['pattern_id']!r} contains forbidden reference {needle!r}"
            )
        for needle in forbidden_words:
            # Match only as a module reference, not as an English word.
            # Forms: `import <m>`, `from <m>`, `addons/<m>`, `addons.<m>`,
            #        `'<m>'`, `"<m>"`, `'<m>.`, `"<m>.`
            esc = re.escape(needle)
            module_ref_re = (
                rf"\bimport\s+{esc}\b"
                rf"|\bfrom\s+{esc}[\s.]"
                rf"|addons/{esc}\b"
                rf"|addons\.{esc}\b"
                rf"|['\"]{esc}['\"]"
                rf"|['\"]{esc}\."
            )
            assert not re.search(module_ref_re, haystack), (
                f"Pattern {p['pattern_id']!r} contains forbidden module reference {needle!r}"
            )
