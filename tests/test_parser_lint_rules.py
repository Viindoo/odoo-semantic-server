# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_parser_lint_rules.py
"""Lint rule parser tests (M4.5 WI3).

Three live sources (v17+):
  - pylint-odoo checker: addons/test_lint/tests/_odoo_checker_*.py with `msgs = {...}`
  - ESLint config:       addons/test_lint/tests/eslintrc (JSON)
  - ruff TOML:           ruff.toml (v19+, [lint].select = [...])

Static placeholder JSON for v8-v16 (per ADR-0002 §4): empty list, _curate_status='pending'.
"""
import json
import os
from pathlib import Path

import pytest

from src.indexer.models import LintRuleInfo
from src.indexer.parser_lint_rules import (
    _parse_eslint_config,
    _parse_pylint_odoo_source,
    _parse_ruff_toml,
    _version_has_test_lint,
    parse_lint_rules_for_version,
)

ODOO17_SRC = os.environ.get("ODOO17_SRC", "/nonexistent/odoo17")


def test_parse_pylint_odoo_msgs_dict_extracts_rule_id():
    """`msgs = {"E8502": (msg, sym, doc)}` → LintRuleInfo(rule_id="E8502", kind=pylint-odoo)."""
    src = '''
import astroid
from pylint.checkers import BaseChecker

class OdooBaseChecker(BaseChecker):
    name = 'odoo'
    msgs = {
        "E8502": (
            'Bad usage of _, _lt function.',
            'gettext-variable',
            'See translation docs',
        ),
        "E8401": (
            'SQL injection risk',
            'sql-injection',
            'docs',
        ),
    }
'''
    rules = _parse_pylint_odoo_source(src, "17.0")
    rule_ids = {r.rule_id for r in rules}
    assert rule_ids == {"E8502", "E8401"}
    e8502 = next(r for r in rules if r.rule_id == "E8502")
    assert e8502.kind == "pylint-odoo"
    assert e8502.odoo_version == "17.0"
    assert "Bad usage" in e8502.message


def test_parse_eslint_config_extracts_rules():
    """ESLint config rules dict → LintRuleInfo per rule."""
    config = {
        "rules": {
            "no-undef": "error",
            "no-debugger": ["error"],
            "no-restricted-syntax": ["error", "PrivateIdentifier"],
        },
    }
    rules = _parse_eslint_config(config, "18.0")
    rule_ids = {r.rule_id for r in rules}
    assert rule_ids == {"no-undef", "no-debugger", "no-restricted-syntax"}
    nu = next(r for r in rules if r.rule_id == "no-undef")
    assert nu.kind == "eslint-odoo"
    assert nu.severity == "error"


def test_parse_ruff_toml_extracts_select_categories():
    """ruff.toml [lint].select = [...] → LintRuleInfo per category."""
    toml_src = '''
target-version = "py310"

[lint]
preview = true
select = [
    "BLE",
    "E",
    "I",
    "UP",
]
ignore = ["E501"]
'''
    rules = _parse_ruff_toml(toml_src, "19.0")
    rule_ids = {r.rule_id for r in rules}
    # All select categories must be picked up; ignore is not surfaced as a rule.
    assert {"BLE", "E", "I", "UP"} <= rule_ids
    assert "E501" not in rule_ids
    bl = next(r for r in rules if r.rule_id == "BLE")
    assert bl.kind == "ruff-builtin"
    assert bl.odoo_version == "19.0"


def test_version_has_test_lint_v17_plus():
    """test_lint addon present from v17 onward (heuristic — gates code-extract)."""
    assert _version_has_test_lint("17.0") is True
    assert _version_has_test_lint("18.0") is True
    assert _version_has_test_lint("19.0") is True
    assert _version_has_test_lint("16.0") is False
    assert _version_has_test_lint("9.0") is False


def test_static_placeholder_v10_returns_empty(tmp_path):
    """Static placeholder JSON for v10 → empty list (curated 'pending' per ADR-0002 §4)."""
    placeholder = tmp_path / "lint_rules_10.0.json"
    placeholder.write_text(json.dumps({
        "_curate_status": "pending",
        "_generated_at": "2026-05-08",
        "rules": [],
    }))
    rules = parse_lint_rules_for_version(
        "10.0",
        odoo_source_root=None,
        static_data_dir=str(tmp_path),
    )
    assert rules == []


def test_static_placeholder_v8_loads_curated_rules(tmp_path):
    """When a v8 static JSON has actual rules, they're loaded with kind preserved."""
    placeholder = tmp_path / "lint_rules_8.0.json"
    placeholder.write_text(json.dumps({
        "_curate_status": "pending",
        "rules": [
            {"rule_id": "X001", "kind": "pylint-odoo", "message": "test rule"},
        ],
    }))
    rules = parse_lint_rules_for_version(
        "8.0",
        odoo_source_root=None,
        static_data_dir=str(tmp_path),
    )
    assert len(rules) == 1
    assert rules[0].rule_id == "X001"
    assert rules[0].odoo_version == "8.0"


def test_lint_rule_info_dataclass_minimal():
    """LintRuleInfo can be instantiated with just required fields."""
    r = LintRuleInfo(rule_id="E8502", odoo_version="17.0", kind="pylint-odoo")
    assert r.severity == "warning"  # default
    assert r.message is None
    assert r.core_symbol_qname is None


@pytest.mark.skipif(
    not Path(ODOO17_SRC + "/odoo/addons/test_lint/tests").exists(),
    reason="Real Odoo 17 test_lint dir not on disk",
)
def test_parse_lint_rules_smoke_real_v17():
    """Smoke: extract real pylint-odoo + eslint rules from Odoo 17 source."""
    rules = parse_lint_rules_for_version(
        "17.0",
        odoo_source_root=ODOO17_SRC,
    )
    # Real v17 has at least the gettext checker (E8502) + ESLint base rules.
    rule_ids = {r.rule_id for r in rules}
    assert "E8502" in rule_ids or any(rid.startswith("E") for rid in rule_ids)
    # ESLint baseline
    assert any(r.kind == "eslint-odoo" for r in rules)


def test_translation_format_interpolation_in_static_v16(tmp_path):
    """W8201 translation-format-interpolation rule present in static v16 catalogue."""
    # Copy the real spec_data file so the test is self-contained.
    real_json = Path(__file__).parent.parent / "src/indexer/spec_data/lint_rules_16.0.json"
    (tmp_path / "lint_rules_16.0.json").write_text(real_json.read_text())
    rules = parse_lint_rules_for_version(
        "16.0",
        odoo_source_root=None,
        static_data_dir=str(tmp_path),
    )
    rule_ids = {r.rule_id for r in rules}
    assert "W8201" in rule_ids, f"W8201 missing from v16 catalogue; got: {sorted(rule_ids)}"
    w8201 = next(r for r in rules if r.rule_id == "W8201")
    assert w8201.kind == "pylint-odoo"
    assert w8201.severity == "warning"
    assert "UserError" in (w8201.message or "")


def test_translation_format_interpolation_in_static_v17(tmp_path):
    """W8201 present in static v17 catalogue and merged into parse_lint_rules_for_version result."""
    real_json = Path(__file__).parent.parent / "src/indexer/spec_data/lint_rules_17.0.json"
    (tmp_path / "lint_rules_17.0.json").write_text(real_json.read_text())
    # No odoo_source_root — tests static-only path for v17.
    rules = parse_lint_rules_for_version(
        "17.0",
        odoo_source_root=None,
        static_data_dir=str(tmp_path),
    )
    rule_ids = {r.rule_id for r in rules}
    assert "W8201" in rule_ids, f"W8201 missing from v17 catalogue; got: {sorted(rule_ids)}"
    w8201 = next(r for r in rules if r.rule_id == "W8201")
    assert w8201.odoo_version == "17.0"
    assert w8201.kind == "pylint-odoo"


def test_translation_format_interpolation_in_static_v18(tmp_path):
    """W8201 present in static v18 catalogue."""
    real_json = Path(__file__).parent.parent / "src/indexer/spec_data/lint_rules_18.0.json"
    (tmp_path / "lint_rules_18.0.json").write_text(real_json.read_text())
    rules = parse_lint_rules_for_version(
        "18.0",
        odoo_source_root=None,
        static_data_dir=str(tmp_path),
    )
    rule_ids = {r.rule_id for r in rules}
    assert "W8201" in rule_ids, f"W8201 missing from v18 catalogue; got: {sorted(rule_ids)}"
