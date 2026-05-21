# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_spec_data_lint_rules_curated.py
"""Acceptance tests for curated lint_rules_*.json static data files (WI-A4).

Two tests:
  1. test_each_version_has_curated_status_complete — all 12 versioned files
     must have _curate_status == "complete" and len(rules) >= 10.
  2. test_rule_schema_valid — every rule entry in every file must conform to
     lint_rule.schema.json (jsonschema validation).
"""
import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SPEC_DATA_DIR = Path(__file__).parent.parent / "src" / "indexer" / "spec_data"
_SCHEMA_FILE = _SPEC_DATA_DIR / "lint_rule.schema.json"

# Versions required per WI-A4 spec (excludes 99.0 test fixture).
_REQUIRED_VERSIONS = [
    "8.0", "9.0", "10.0", "11.0", "12.0", "13.0",
    "14.0", "15.0", "16.0", "17.0", "18.0", "19.0",
]

_MIN_RULES_PER_VERSION = 10


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_lint_file(version: str) -> dict:
    path = _SPEC_DATA_DIR / f"lint_rules_{version}.json"
    assert path.is_file(), f"Missing lint_rules_{version}.json in {_SPEC_DATA_DIR}"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data


def _load_schema() -> dict:
    assert _SCHEMA_FILE.is_file(), f"Missing schema file: {_SCHEMA_FILE}"
    return json.loads(_SCHEMA_FILE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Test 1: curate_status + minimum rule count
# ---------------------------------------------------------------------------

class TestEachVersionHasCuratedStatusComplete:
    """All 12 versioned lint_rules_*.json files must be marked complete with >= 10 rules."""

    @pytest.mark.parametrize("version", _REQUIRED_VERSIONS)
    def test_curate_status_complete(self, version: str):
        data = _load_lint_file(version)
        status = data.get("_curate_status")
        assert status == "complete", (
            f"lint_rules_{version}.json has _curate_status={status!r}; expected 'complete'."
        )

    @pytest.mark.parametrize("version", _REQUIRED_VERSIONS)
    def test_minimum_rule_count(self, version: str):
        data = _load_lint_file(version)
        rules = data.get("rules", [])
        assert len(rules) >= _MIN_RULES_PER_VERSION, (
            f"lint_rules_{version}.json has only {len(rules)} rules; "
            f"expected >= {_MIN_RULES_PER_VERSION}."
        )

    @pytest.mark.parametrize("version", _REQUIRED_VERSIONS)
    def test_has_note_field(self, version: str):
        data = _load_lint_file(version)
        note = data.get("_note", "")
        assert len(note) > 10, (
            f"lint_rules_{version}.json has empty or missing _note field."
        )


# ---------------------------------------------------------------------------
# Test 2: schema validation
# ---------------------------------------------------------------------------

class TestRuleSchemaValid:
    """Every rule entry must conform to lint_rule.schema.json."""

    @pytest.fixture(scope="class")
    def schema(self):
        return _load_schema()

    def _validate_rule(self, rule: dict, schema: dict, version: str, idx: int) -> None:
        """Manual schema validation (avoids jsonschema dependency which may not be installed)."""
        # Required fields
        for required_field in schema.get("required", []):
            assert required_field in rule, (
                f"lint_rules_{version}.json rules[{idx}] missing required field "
                f"'{required_field}': {rule}"
            )

        props = schema.get("properties", {})

        # rule_id: non-empty string
        rule_id = rule.get("rule_id", "")
        assert isinstance(rule_id, str) and len(rule_id) >= 1, (
            f"lint_rules_{version}.json rules[{idx}].rule_id must be non-empty string, "
            f"got {rule_id!r}"
        )

        # kind: must be one of allowed enum values
        kind_enum = props.get("kind", {}).get("enum", [])
        assert rule.get("kind") in kind_enum, (
            f"lint_rules_{version}.json rules[{idx}].kind={rule.get('kind')!r} "
            f"not in allowed values {kind_enum}"
        )

        # severity: must be one of allowed enum values
        severity_enum = props.get("severity", {}).get("enum", [])
        assert rule.get("severity") in severity_enum, (
            f"lint_rules_{version}.json rules[{idx}].severity={rule.get('severity')!r} "
            f"not in allowed values {severity_enum}"
        )

        # message: non-empty string with min length 5
        message = rule.get("message", "")
        assert isinstance(message, str) and len(message) >= 5, (
            f"lint_rules_{version}.json rules[{idx}].message must be string >= 5 chars"
        )

        # optional nullable fields: file_pattern, fix_template, core_symbol_qname
        for nullable_field in ("file_pattern", "fix_template", "core_symbol_qname"):
            val = rule.get(nullable_field)
            if val is not None:
                assert isinstance(val, str), (
                    f"lint_rules_{version}.json rules[{idx}].{nullable_field} "
                    f"must be string or null, got {type(val)}"
                )

        # No extra keys beyond schema properties
        allowed_keys = set(props.keys())
        extra_keys = set(rule.keys()) - allowed_keys
        if schema.get("additionalProperties") is False:
            assert not extra_keys, (
                f"lint_rules_{version}.json rules[{idx}] has unexpected keys: {extra_keys}"
            )

    @pytest.mark.parametrize("version", _REQUIRED_VERSIONS)
    def test_rule_schema_valid(self, schema, version: str):
        data = _load_lint_file(version)
        rules = data.get("rules", [])
        assert isinstance(rules, list), f"lint_rules_{version}.json 'rules' must be a list"
        for idx, rule in enumerate(rules):
            assert isinstance(rule, dict), (
                f"lint_rules_{version}.json rules[{idx}] must be a dict, got {type(rule)}"
            )
            self._validate_rule(rule, schema, version, idx)

    def test_schema_file_is_valid_json(self, schema):
        """Schema file itself is valid JSON with expected top-level keys."""
        assert "$schema" in schema
        assert "properties" in schema
        assert "required" in schema
        required = schema["required"]
        assert "rule_id" in required
        assert "kind" in required
        assert "message" in required
        assert "severity" in required
