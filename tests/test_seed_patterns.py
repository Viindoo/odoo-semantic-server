"""Tests for src/data/patterns.json + src/indexer/seed_patterns.py CLI."""
import json
from pathlib import Path

import pytest

from src.indexer.seed_patterns import _load_patterns

PATTERNS_PATH = Path(__file__).resolve().parent.parent / "src" / "data" / "patterns.json"


def test_patterns_json_valid_and_non_empty():
    """patterns.json parses as JSON list with ≥50 entries."""
    data = json.loads(PATTERNS_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert len(data) >= 50, f"Expected ≥50 patterns, got {len(data)}"


def test_patterns_required_core_ids_present():
    """15 core pattern IDs from M4.6 plan WI4 — all must be present."""
    data = json.loads(PATTERNS_PATH.read_text(encoding="utf-8"))
    ids = {p["pattern_id"] for p in data}
    required = {
        "computed-field-cross-model",
        "computed-field-lang-context",
        "create-multi-v17",
        "write-read-before-super",
        "xpath-avoid-replace",
        "xpath-specific-expr",
        "owl-patch-v17",
        "inherits-vs-inherit",
        "action-return-super",
        "crud-return-value",
        "old-style-super",
        "missing-return-override",
        "store-computed-field",
        "model-create-multi-batch",
        "depends-full-dotted-path",
    }
    missing = required - ids
    assert not missing, f"Missing required pattern IDs: {missing}"


def test_patterns_no_empty_snippet_or_gotchas():
    """Every pattern must have non-empty snippet_text + ≥1 gotcha."""
    data = json.loads(PATTERNS_PATH.read_text(encoding="utf-8"))
    for p in data:
        assert p["snippet_text"].strip(), (
            f"Empty snippet for pattern_id={p['pattern_id']}"
        )
        assert len(p.get("gotchas") or []) >= 1, (
            f"No gotchas for pattern_id={p['pattern_id']}"
        )


def test_patterns_required_fields_per_entry():
    """Every entry has the canonical schema (per ADR-0003 §1)."""
    data = json.loads(PATTERNS_PATH.read_text(encoding="utf-8"))
    required_fields = {
        "pattern_id", "intent_keywords", "file_ref", "snippet_text",
        "gotchas", "odoo_version_min", "language",
    }
    for p in data:
        missing = required_fields - p.keys()
        assert not missing, (
            f"Pattern {p.get('pattern_id', '<unknown>')} missing fields: {missing}"
        )
        assert p["language"] in ("python", "xml", "js"), (
            f"Pattern {p['pattern_id']} has invalid language={p['language']!r}"
        )
        # Slug constraint per ADR-0003 §4: pattern_id must NOT contain '__'
        assert "__" not in p["pattern_id"], (
            f"pattern_id {p['pattern_id']!r} contains '__' which collides "
            "with language__id slug encoding (per ADR-0003 §4)"
        )


def test_load_patterns_filters_by_version(tmp_path):
    """_load_patterns honours --version filter."""
    sample = [
        {
            "pattern_id": "p1",
            "intent_keywords": [],
            "file_ref": "f:1",
            "snippet_text": "x",
            "gotchas": ["g"],
            "odoo_version_min": "17.0",
            "language": "python",
        },
        {
            "pattern_id": "p2",
            "intent_keywords": [],
            "file_ref": "f:2",
            "snippet_text": "y",
            "gotchas": ["g"],
            "odoo_version_min": "18.0",
            "language": "python",
        },
    ]
    path = tmp_path / "p.json"
    path.write_text(json.dumps(sample))

    only_17 = _load_patterns(path, version_filter="17.0")
    assert [p.pattern_id for p in only_17] == ["p1"]

    all_ver = _load_patterns(path, version_filter=None)
    assert {p.pattern_id for p in all_ver} == {"p1", "p2"}


def test_seed_cli_help_smoke():
    """`seed_patterns --help` exits 0 — argparse wiring sanity check."""
    import subprocess
    import sys
    result = subprocess.run(
        [sys.executable, "-m", "src.indexer.seed_patterns", "--help"],
        capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent,
    )
    assert result.returncode == 0
    assert "patterns.json" in result.stdout

    # Cross-version uniqueness sanity: pattern_id is globally unique
    data = json.loads(PATTERNS_PATH.read_text(encoding="utf-8"))
    ids = [p["pattern_id"] for p in data]
    assert len(ids) == len(set(ids)), (
        f"Duplicate pattern_id in patterns.json: "
        f"{[i for i in ids if ids.count(i) > 1]}"
    )


@pytest.mark.parametrize("language", ["python", "xml", "js"])
def test_patterns_each_language_has_entries(language):
    """Every supported language has at least one entry — coverage smoke."""
    data = json.loads(PATTERNS_PATH.read_text(encoding="utf-8"))
    matches = [p for p in data if p["language"] == language]
    assert matches, f"No patterns for language={language!r}"
