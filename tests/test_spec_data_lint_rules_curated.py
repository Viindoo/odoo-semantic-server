# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_spec_data_lint_rules_curated.py
"""Acceptance tests for curated lint_rules_*.json static data files (WI-A4/WI-8).

Tests:
  1. test_each_version_has_curated_status_complete - all 12 versioned files
     must have _curate_status == "complete" and len(rules) >= 10.
  2. test_rule_schema_valid - every rule entry in every file must conform to
     lint_rule.schema.json (jsonschema validation).
  3. WI-8 additions:
     a. test_code_pattern_regex_compiles - every non-null code_pattern must
        compile without error (re.compile).
     b. test_code_pattern_no_redos_shape - reject patterns with naive nested
        quantifier shapes e.g. (...+)+ that cause exponential backtracking.
     c. test_code_pattern_cross_version_consistent - same rule_id in multiple
        versions must carry the same code_pattern (no silent drift).
     d. test_overlay_propagates_code_pattern - _apply_code_patterns_overlay
        patches code_pattern from static JSON onto a synthetic live-parse list,
        locking the overlay mechanism against future refactor regressions.
"""
import json
import re
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

# Modern-era (v10+) curation depth floor — moved here from the former
# test_lint_rules_minimum_count.py (WI-5 curation goal). Legacy v8/v9 keep the
# >=10 baseline already enforced by TestEachVersionHasCuratedStatusComplete
# .test_minimum_rule_count (all 12 versions). Thresholds unchanged.
_MODERN_ERA_VERSIONS = [v for v in _REQUIRED_VERSIONS if int(v.split(".")[0]) >= 10]
_MIN_RULES_MODERN = 50


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

    @pytest.mark.parametrize("version", _MODERN_ERA_VERSIONS)
    def test_minimum_rule_count_modern(self, version: str):
        """Modern-era versions (v10+) must have >= 50 curated rules (WI-5 depth).

        Moved verbatim from the former test_lint_rules_minimum_count.py
        (TestModernEraMinimumCount). Floor `>=` is unaffected by #242 adding rules.
        """
        data = _load_lint_file(version)
        rules = data.get("rules", [])
        assert len(rules) >= _MIN_RULES_MODERN, (
            f"lint_rules_{version}.json has only {len(rules)} rules; "
            f"expected >= {_MIN_RULES_MODERN} for modern-era v10+."
        )

    @pytest.mark.parametrize("version", _REQUIRED_VERSIONS)
    def test_has_note_field(self, version: str):
        data = _load_lint_file(version)
        note = data.get("_note", "")
        assert len(note) > 10, (
            f"lint_rules_{version}.json has empty or missing _note field."
        )


class TestRuleIdUniqueness:
    """Each rule_id must be unique within a version file.

    Moved verbatim from the former test_lint_rules_minimum_count.py
    (TestRuleIdUniqueness) — not covered by the schema-validity test.
    """

    @pytest.mark.parametrize("version", _REQUIRED_VERSIONS)
    def test_rule_ids_unique(self, version: str):
        data = _load_lint_file(version)
        rules = data.get("rules", [])
        rule_ids = [r.get("rule_id") for r in rules if isinstance(r, dict)]
        duplicates = {rid for rid in rule_ids if rule_ids.count(rid) > 1}
        assert not duplicates, (
            f"lint_rules_{version}.json has duplicate rule_ids: {sorted(duplicates)}"
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


# ---------------------------------------------------------------------------
# WI-8 Test E: cross-version consistency + regex safety
# ---------------------------------------------------------------------------

class TestCodePatternDataIntegrity:
    """WI-8 data integrity checks for code_pattern across all 12 version files."""

    def _all_rules_with_patterns(self) -> list[tuple[str, str, str]]:
        """Return [(version, rule_id, code_pattern)] for every non-null pattern."""
        result = []
        for version in _REQUIRED_VERSIONS:
            data = _load_lint_file(version)
            for r in data.get("rules", []):
                if isinstance(r, dict) and r.get("code_pattern"):
                    result.append((version, r["rule_id"], r["code_pattern"]))
        return result

    def test_code_pattern_regex_compiles(self):
        """Every non-null code_pattern in every version file must compile via re.compile.

        A broken regex in the data causes silent fallback to fuzzy matching for all
        rules in that version during index-core. Compile-check at test time catches
        typos early.
        """
        failures = []
        for version, rule_id, pattern in self._all_rules_with_patterns():
            try:
                re.compile(pattern)
            except re.error as exc:
                failures.append(
                    f"  lint_rules_{version}.json {rule_id}: "
                    f"code_pattern={pattern!r} - re.error: {exc}"
                )
        assert not failures, (
            "The following code_pattern values fail re.compile:\n" + "\n".join(failures)
        )

    # Naive ReDoS shape detector: flag patterns that contain (...+)+ or (.*)*
    # or (.+)+ forms at the string level. This is a simple string check, not a
    # full ReDoS analyser - it catches the most common exponential-backtracking
    # shapes documented in OWASP ReDoS guidance without requiring an external
    # library. Legitimate complex alternations (e.g. (?:...)+) that happen to
    # match the substring are also flagged conservatively.
    _REDOS_SHAPE_RE = re.compile(r"\((?:[^()]*[+*])[^()]*\)[+*]")

    def test_code_pattern_no_redos_shape(self):
        """No code_pattern should contain naive nested quantifier shapes like (...+)+.

        Such patterns cause exponential backtracking on adversarial input and can
        make the MCP server hang during lint_check tool calls.
        """
        failures = []
        for version, rule_id, pattern in self._all_rules_with_patterns():
            if self._REDOS_SHAPE_RE.search(pattern):
                failures.append(
                    f"  lint_rules_{version}.json {rule_id}: "
                    f"code_pattern={pattern!r} contains nested quantifier shape"
                )
        assert not failures, (
            "The following code_pattern values contain ReDoS-prone nested quantifiers:\n"
            + "\n".join(failures)
        )

    # Backreference detector: the schema description explicitly promises
    # "no backreferences", which the ReDoS-shape test above did not cover.
    # Catches numeric backrefs (\1-\9) and named backrefs ((?P=name)). A
    # backreference forces the engine to revisit captured text and is a
    # documented catastrophic-backtracking vector.
    _BACKREF_RE = re.compile(r"\\[1-9]|\(\?P=")

    def test_code_pattern_no_backreferences(self):
        """No code_pattern may contain a backreference (schema description promise).

        The lint_rule.schema.json code_pattern description states "no
        backreferences". This locks the data to that contract — a numeric (\\1)
        or named ((?P=x)) backreference in any curated pattern fails the test.
        """
        failures = []
        for version, rule_id, pattern in self._all_rules_with_patterns():
            if self._BACKREF_RE.search(pattern):
                failures.append(
                    f"  lint_rules_{version}.json {rule_id}: "
                    f"code_pattern={pattern!r} contains a backreference"
                )
        assert not failures, (
            "The following code_pattern values contain backreferences "
            "(forbidden by the schema description):\n" + "\n".join(failures)
        )

    def test_backref_detector_actually_fires(self):
        """Sanity: the backreference detector must match a known backref shape.

        A detector that never matches anything would make the guard above a
        false-green. Confirms the regex flags both numeric and named backrefs.
        """
        assert self._BACKREF_RE.search(r"(\w)\1"), "numeric backref must be caught"
        assert self._BACKREF_RE.search(r"(?P<x>\w)(?P=x)"), "named backref must be caught"
        assert not self._BACKREF_RE.search(r"\bfields\.Html\s*\("), (
            "a plain pattern must not be flagged as a backreference"
        )

    def test_code_pattern_cross_version_consistent(self):
        """Same rule_id appearing in multiple version files must have identical code_pattern.

        Silent drift (e.g. fixing a regex in v17 but not v16) causes inconsistent
        behaviour across Odoo versions. Cross-version consistency is mandatory.
        """
        # Build: rule_id -> {pattern -> [versions]}
        pattern_map: dict[str, dict[str, list[str]]] = {}
        for version, rule_id, pattern in self._all_rules_with_patterns():
            pattern_map.setdefault(rule_id, {}).setdefault(pattern, []).append(version)

        failures = []
        for rule_id, patterns_to_versions in pattern_map.items():
            if len(patterns_to_versions) > 1:
                # Multiple distinct patterns for same rule_id - drift detected.
                details = "; ".join(
                    f"{pattern!r} in {sorted(versions)}"
                    for pattern, versions in sorted(patterns_to_versions.items())
                )
                failures.append(f"  {rule_id}: {details}")

        assert not failures, (
            "The following rule_ids have inconsistent code_pattern across versions:\n"
            + "\n".join(failures)
        )


# ---------------------------------------------------------------------------
# WI-8 Test D: overlay mechanism lock
# ---------------------------------------------------------------------------

class TestCodePatternOverlayMechanism:
    """Lock the _apply_code_patterns_overlay post-pass against future refactor breakage.

    The overlay is critical: live-parse rules (v17+) win the dedup race in
    parse_lint_rules_for_version, so without the overlay their code_pattern would
    remain None even when the static JSON has a pattern for the same rule_id.

    This test exercises the overlay function directly with a synthetic live-parse
    list so it does not require an Odoo source tree.
    """

    def test_overlay_patches_code_pattern_from_static_json(self):
        """After overlay, a live-parse rule with code_pattern=None gets patched from static JSON.

        Simulates: live-parse produced E8501 with code_pattern=None (because the
        live-parse path does not read the static JSON patterns). The overlay must
        patch E8501.code_pattern from the 17.0 static JSON.
        """
        from src.indexer.models import LintRuleInfo
        from src.indexer.parser_lint_rules import _apply_code_patterns_overlay

        # Synthetic live-parse rule - E8501 is present in lint_rules_17.0.json with a pattern.
        live_parse_rule = LintRuleInfo(
            rule_id="E8501",
            odoo_version="17.0",
            kind="pylint-odoo",
            message="Possible SQL injection risk",
            severity="error",
            code_pattern=None,  # live-parse does not set this
        )

        # Verify precondition: E8501 exists in static JSON with a non-null pattern.
        static_data = _load_lint_file("17.0")
        static_e8501 = next(
            (r for r in static_data.get("rules", []) if r["rule_id"] == "E8501"), None
        )
        assert static_e8501 is not None, "E8501 must be present in lint_rules_17.0.json"
        assert static_e8501.get("code_pattern"), (
            "E8501 must have a non-null code_pattern in lint_rules_17.0.json"
        )
        expected_pattern = static_e8501["code_pattern"]

        rules = [live_parse_rule]
        _apply_code_patterns_overlay(rules, "17.0", _SPEC_DATA_DIR)

        assert rules[0].code_pattern == expected_pattern, (
            f"Overlay must patch E8501.code_pattern from static JSON.\n"
            f"Expected: {expected_pattern!r}\n"
            f"Got: {rules[0].code_pattern!r}"
        )

    def test_overlay_does_not_overwrite_existing_pattern(self):
        """If a rule already has a code_pattern, the overlay must not overwrite it.

        The overlay uses an 'only set when None' policy so live-parse rules that
        happen to define their own pattern are not silently replaced.
        """
        from src.indexer.models import LintRuleInfo
        from src.indexer.parser_lint_rules import _apply_code_patterns_overlay

        custom_pattern = r"custom_specific_pattern"
        rule_with_own_pattern = LintRuleInfo(
            rule_id="W8140",
            odoo_version="17.0",
            kind="pylint-odoo",
            message="SQL injection risk",
            severity="warning",
            code_pattern=custom_pattern,  # already set - must be preserved
        )

        rules = [rule_with_own_pattern]
        _apply_code_patterns_overlay(rules, "17.0", _SPEC_DATA_DIR)

        assert rules[0].code_pattern == custom_pattern, (
            f"Overlay must not overwrite an existing code_pattern.\n"
            f"Expected: {custom_pattern!r}\n"
            f"Got: {rules[0].code_pattern!r}"
        )

    def test_real_merge_order_live_parse_wins_overlay_supplies_pattern(self, tmp_path):
        """Lock the REAL production merge order, not just the overlay function (PR #275 r3 #5).

        ``test_overlay_patches_code_pattern_from_static_json`` calls the overlay
        on a synthetic rule directly, so it cannot detect a regression in how
        ``parse_lint_rules_for_version`` orders live-parse vs. static merge vs.
        the overlay post-pass. This test drives the full public entry point with
        a real (temp) Odoo source tree that live-parses E8501 with NO
        code_pattern (the live source never carries one), then asserts the final
        E8501 carries the code_pattern from the static JSON.

        Why this fails-red on a broken merge order: the live-parse rule wins the
        ``(rule_id, kind)`` dedup (it is ``_add``-ed first), so the merged E8501
        has ``code_pattern=None`` until the overlay runs LAST. Remove the overlay,
        run it before the static merge, or let the static rule win the dedup
        instead, and the final pattern is wrong → assertion fails.
        """
        from src.indexer.parser_lint_rules import parse_lint_rules_for_version

        # Build a minimal Odoo source tree so the live-parse path activates for v17.
        checker_dir = tmp_path / "odoo" / "addons" / "test_lint" / "tests"
        checker_dir.mkdir(parents=True)
        # A pylint-odoo BaseChecker with E8501 in `msgs` - mirrors the real shape.
        # The live-parse path (`_parse_pylint_odoo_source`) extracts rule_id +
        # message + severity but NEVER a code_pattern.
        (checker_dir / "_odoo_checker_sql.py").write_text(
            "class OdooChecker:\n"
            "    msgs = {\n"
            '        "E8501": (\n'
            '            "Possible SQL injection risk", "sql-injection", "doc"\n'
            "        ),\n"
            "    }\n",
            encoding="utf-8",
        )

        # Static SSOT for the pattern lives in the real spec_data dir.
        static_e8501 = next(
            (r for r in _load_lint_file("17.0").get("rules", [])
             if r["rule_id"] == "E8501"),
            None,
        )
        assert static_e8501 and static_e8501.get("code_pattern"), (
            "Precondition: E8501 must carry a code_pattern in lint_rules_17.0.json"
        )
        expected_pattern = static_e8501["code_pattern"]

        merged = parse_lint_rules_for_version(
            "17.0",
            odoo_source_root=str(tmp_path),
            static_data_dir=_SPEC_DATA_DIR,
        )
        by_id = {r.rule_id: r for r in merged}

        # The live-parse rule must be the one that survived dedup (proves order).
        assert "E8501" in by_id, "E8501 must be present after the full merge"
        assert by_id["E8501"].message == "Possible SQL injection risk", (
            "The live-parse E8501 (not the static one) must win the dedup race - "
            "if this fails the static rule won, inverting the documented order."
        )
        # ...yet its code_pattern must come from the static SSOT via the overlay.
        assert by_id["E8501"].code_pattern == expected_pattern, (
            "Final merge order is broken: the live-parse E8501 won the dedup but "
            "the overlay did not supply its code_pattern from the static JSON.\n"
            f"Expected: {expected_pattern!r}\nGot: {by_id['E8501'].code_pattern!r}"
        )
