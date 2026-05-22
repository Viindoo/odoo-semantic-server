# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_lint_rules_minimum_count.py
"""Enforce minimum rule counts and data integrity for curated lint_rules_*.json files.

Version-aware assertions:
  - v8, v9 (legacy era): >= 10 rules each (baseline; fewer applicable modern-era rules)
  - v10+ (modern era): >= 50 rules each (full curation depth per WI-5)

Additional assertions:
  - rule_id uniqueness within each file
  - schema-required keys present (rule_id, kind, message, severity)
  - kind and severity values within allowed enums
"""
import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SPEC_DATA_DIR = Path(__file__).parent.parent / "src" / "indexer" / "spec_data"
_SCHEMA_FILE = _SPEC_DATA_DIR / "lint_rule.schema.json"

# All production versions (excludes 99.0 test fixture).
_ALL_VERSIONS = [
    "8.0", "9.0", "10.0", "11.0", "12.0", "13.0",
    "14.0", "15.0", "16.0", "17.0", "18.0", "19.0",
]

# Modern-era versions require >=50 rules (WI-5 curation goal).
_MODERN_ERA_VERSIONS = [v for v in _ALL_VERSIONS if int(v.split(".")[0]) >= 10]

# Legacy era versions keep baseline >=10.
_LEGACY_ERA_VERSIONS = [v for v in _ALL_VERSIONS if int(v.split(".")[0]) < 10]

_MIN_RULES_MODERN = 50
_MIN_RULES_LEGACY = 10

# Allowed enum values sourced from lint_rule.schema.json.
_ALLOWED_KINDS = {"pylint-odoo", "pylint-stdlib", "eslint", "eslint-odoo", "ruff-builtin"}
_ALLOWED_SEVERITIES = {"error", "warning", "info"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_lint_file(version: str) -> dict:
    path = _SPEC_DATA_DIR / f"lint_rules_{version}.json"
    assert path.is_file(), f"Missing lint_rules_{version}.json in {_SPEC_DATA_DIR}"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data


def _get_rules(version: str) -> list:
    data = _load_lint_file(version)
    rules = data.get("rules", [])
    assert isinstance(rules, list), f"lint_rules_{version}.json 'rules' must be a list"
    return rules


# ---------------------------------------------------------------------------
# Test: minimum rule count — modern era (v10+) >= 50
# ---------------------------------------------------------------------------

class TestModernEraMinimumCount:
    """Modern-era versions (v10-v19) must have >= 50 curated rules."""

    @pytest.mark.parametrize("version", _MODERN_ERA_VERSIONS)
    def test_minimum_50_rules(self, version: str):
        rules = _get_rules(version)
        assert len(rules) >= _MIN_RULES_MODERN, (
            f"lint_rules_{version}.json has only {len(rules)} rules; "
            f"expected >= {_MIN_RULES_MODERN} for modern-era v10+."
        )


# ---------------------------------------------------------------------------
# Test: minimum rule count — legacy era (v8-v9) >= 10
# ---------------------------------------------------------------------------

class TestLegacyEraMinimumCount:
    """Legacy-era versions (v8-v9) must have >= 10 curated rules (baseline)."""

    @pytest.mark.parametrize("version", _LEGACY_ERA_VERSIONS)
    def test_minimum_10_rules(self, version: str):
        rules = _get_rules(version)
        assert len(rules) >= _MIN_RULES_LEGACY, (
            f"lint_rules_{version}.json has only {len(rules)} rules; "
            f"expected >= {_MIN_RULES_LEGACY}."
        )


# ---------------------------------------------------------------------------
# Test: rule_id uniqueness within each file
# ---------------------------------------------------------------------------

class TestRuleIdUniqueness:
    """Each rule_id must be unique within a version file."""

    @pytest.mark.parametrize("version", _ALL_VERSIONS)
    def test_rule_ids_unique(self, version: str):
        rules = _get_rules(version)
        rule_ids = [r.get("rule_id") for r in rules if isinstance(r, dict)]
        duplicates = {rid for rid in rule_ids if rule_ids.count(rid) > 1}
        assert not duplicates, (
            f"lint_rules_{version}.json has duplicate rule_ids: {sorted(duplicates)}"
        )


# ---------------------------------------------------------------------------
# Test: required schema keys present with valid enum values
# ---------------------------------------------------------------------------

class TestSchemaRequiredFields:
    """Every rule entry must have the 4 required fields with valid values."""

    @pytest.mark.parametrize("version", _ALL_VERSIONS)
    def test_required_fields_present(self, version: str):
        rules = _get_rules(version)
        for idx, rule in enumerate(rules):
            assert isinstance(rule, dict), (
                f"lint_rules_{version}.json rules[{idx}] must be a dict"
            )
            for field in ("rule_id", "kind", "message", "severity"):
                assert field in rule, (
                    f"lint_rules_{version}.json rules[{idx}] missing required field '{field}'"
                )

            # rule_id: non-empty string
            assert isinstance(rule["rule_id"], str) and len(rule["rule_id"]) >= 1, (
                f"lint_rules_{version}.json rules[{idx}].rule_id must be non-empty string"
            )

            # kind: allowed enum
            assert rule["kind"] in _ALLOWED_KINDS, (
                f"lint_rules_{version}.json rules[{idx}].kind={rule['kind']!r} "
                f"not in allowed values {_ALLOWED_KINDS}"
            )

            # severity: allowed enum
            assert rule["severity"] in _ALLOWED_SEVERITIES, (
                f"lint_rules_{version}.json rules[{idx}].severity={rule['severity']!r} "
                f"not in allowed values {_ALLOWED_SEVERITIES}"
            )

            # message: string >= 5 chars
            assert isinstance(rule["message"], str) and len(rule["message"]) >= 5, (
                f"lint_rules_{version}.json rules[{idx}].message must be string >= 5 chars"
            )
