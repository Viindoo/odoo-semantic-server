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
    from src.data.ee_modules import EE_CONFUSION

    # Base forbidden strings
    forbidden = ["enterprise/", "OEEL-1", "OEEL "]

    # Add all 16 EE module keys
    forbidden.extend(EE_CONFUSION.keys())

    # Add all non-None Viindoo equivalents (values)
    forbidden.extend(v for v in EE_CONFUSION.values() if v is not None)

    for p in _load_patterns_list():
        haystack = p.get("snippet_text", "") + " " + " ".join(p.get("gotchas", []))
        for needle in forbidden:
            assert needle not in haystack, (
                f"Pattern {p['pattern_id']!r} contains forbidden reference {needle!r}"
            )
