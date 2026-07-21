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
    @classmethod
    def schema(cls):
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
# Real jsonschema-library validation (issue #364 A7)
# ---------------------------------------------------------------------------
# TestRuleSchemaValid above is a hand-rolled re-implementation of a subset of
# lint_rule.schema.json. This class additionally runs the actual `jsonschema`
# library (already a pinned dependency) so every constraint the schema
# declares is enforced mechanically, not just the ones a human remembered to
# re-check by hand. Mirrors tests/test_patterns_schema.py's established
# pattern. TestRuleSchemaValid is left unweakened - this is additive.

class TestJsonschemaLibraryValidation:
    @pytest.fixture(scope="class")
    @classmethod
    def jsonschema_validator(cls):
        from jsonschema import validators
        schema = _load_schema()
        validator_cls = validators.validator_for(schema)
        validator_cls.check_schema(schema)
        return validator_cls(schema)

    @pytest.mark.parametrize("version", _REQUIRED_VERSIONS)
    def test_all_rules_validate_against_real_jsonschema(self, jsonschema_validator, version: str):
        data = _load_lint_file(version)
        errors = []
        for i, rule in enumerate(data.get("rules", [])):
            for err in jsonschema_validator.iter_errors(rule):
                errors.append(f"rules[{i}] ({rule.get('rule_id')}): {err.message}")
        assert not errors, (
            f"lint_rules_{version}.json has jsonschema violations:\n" + "\n".join(errors)
        )

    def test_validator_actually_rejects_a_broken_record(self, jsonschema_validator):
        """A validator that cannot fail is the thing issue #364 A7 eliminates -
        prove this one can (missing the now-required rule_id_source, issue
        #364 B4)."""
        broken = {
            "rule_id": "W9999",
            "kind": "pylint-odoo",
            "message": "Example broken rule missing provenance.",
            "severity": "warning",
        }
        errors = list(jsonschema_validator.iter_errors(broken))
        assert errors, "expected the broken record (missing rule_id_source) to fail validation"


# ---------------------------------------------------------------------------
# Rule-id provenance (issue #364 B4)
# ---------------------------------------------------------------------------
# 18-19 of the 69 distinct curated pylint-odoo rule_ids collide with real,
# currently-assigned OCA pylint-odoo codes under a COMPLETELY DIFFERENT
# meaning (e.g. curated W8110 = "_columns dict deprecated" vs real pylint-odoo
# 10.0.7 W8110 = "missing-return" / "Missing `return` (`super` is used)").
# rule_id_source/rule_id_collision (lint_rule.schema.json) make that latent
# collision visible in the data instead of an agent discovering it only by
# looking the code up externally and getting a different rule. These tests
# guard the CONTRACT of that provenance field, not its exact classification
# (which is a point-in-time verification result, expected to need periodic
# re-verification as the real pylint-odoo package evolves - see the schema
# description).

class TestRuleIdProvenance:
    @pytest.mark.parametrize("version", _REQUIRED_VERSIONS)
    def test_every_rule_has_a_source(self, version: str):
        data = _load_lint_file(version)
        for i, rule in enumerate(data.get("rules", [])):
            src = rule.get("rule_id_source")
            assert src in ("upstream", "osm-local"), (
                f"lint_rules_{version}.json rules[{i}] ({rule.get('rule_id')}): "
                f"rule_id_source={src!r} must be 'upstream' or 'osm-local'"
            )

    @pytest.mark.parametrize("version", _REQUIRED_VERSIONS)
    def test_collision_only_set_on_osm_local(self, version: str):
        """rule_id_collision must be null whenever rule_id_source='upstream' -
        collision is only a meaningful concept for an OSM-invented id (an
        'upstream' id IS the real one, so it cannot collide with itself)."""
        data = _load_lint_file(version)
        for i, rule in enumerate(data.get("rules", [])):
            if rule.get("rule_id_source") == "upstream":
                assert rule.get("rule_id_collision") is None, (
                    f"lint_rules_{version}.json rules[{i}] ({rule.get('rule_id')}): "
                    f"rule_id_source='upstream' but rule_id_collision is set - "
                    f"an upstream id cannot collide with itself"
                )

    @pytest.mark.parametrize("version", _REQUIRED_VERSIONS)
    def test_same_rule_id_has_consistent_provenance_across_versions(self, version: str):
        """The same rule_id must carry the same rule_id_source in every
        version it appears in - provenance is a fact about the ID, not about
        which version file happens to hold it (same principle as the existing
        code_pattern cross-version consistency check below)."""
        data = _load_lint_file(version)
        seen: dict[tuple[str, str], str] = {}
        for rule in data.get("rules", []):
            key = (rule.get("kind"), rule.get("rule_id"))
            src = rule.get("rule_id_source")
            if key in seen:
                assert seen[key] == src, (
                    f"lint_rules_{version}.json: {key} has inconsistent "
                    f"rule_id_source within the same file: {seen[key]!r} vs {src!r}"
                )
            seen[key] = src

    def test_known_collision_count_within_expected_range(self):
        """Pin (loosely) the collision headcount so a future curated-data edit
        that silently adds/removes a collision without updating
        rule_id_collision is caught. Verified 2026-07-21 against installed
        pylint-odoo 10.0.7: 19 of 69 distinct curated pylint-odoo rule_ids
        collide with a real, differently-meaning code (issue #364 B4) - a
        range (not an exact pin) because the real package's own id set shifts
        across its own minor releases (confirmed: 56/58/58 ODOO_MSGS entries
        across three installed 10.0.x builds on this dev box)."""
        collisions: set[str] = set()
        all_pylint_ids: set[str] = set()
        for version in _REQUIRED_VERSIONS:
            data = _load_lint_file(version)
            for rule in data.get("rules", []):
                if rule.get("kind") != "pylint-odoo":
                    continue
                all_pylint_ids.add(rule["rule_id"])
                if rule.get("rule_id_source") == "osm-local" and rule.get("rule_id_collision"):
                    collisions.add(rule["rule_id"])
        assert 15 <= len(collisions) <= 25, (
            f"expected roughly 15-25 colliding pylint-odoo rule_ids, got "
            f"{len(collisions)}: {sorted(collisions)}"
        )
        assert len(all_pylint_ids) >= 60, (
            f"expected >= 60 distinct curated pylint-odoo rule_ids, got "
            f"{len(all_pylint_ids)}"
        )


# ---------------------------------------------------------------------------
# Duplicate reporting guard (issue #364 B5)
# ---------------------------------------------------------------------------
# W8140 (static, OSM-local) and E8501 (the real Odoo-vendored
# `_odoo_checker_sql_injection.py` id) used to carry an IDENTICAL
# code_pattern regex at v17.0-v19.0, so lint_check double-reported one real
# defect under two different rule_ids. The fix consolidated onto the real
# upstream id E8501 from v14.0 (the first version its checker source is
# glob-reachable, LINT_RULES_MIN_MAJOR) through v19.0, and left v8.0-v13.0
# untouched (no live E8501 oracle wired for those versions - W8140 remains
# the only way to express the fact there). This guard is intentionally
# GENERAL (not just "no W8140+E8501 together") so it also catches a FUTURE
# reintroduction of the same failure mode under different rule_ids.

class TestNoDuplicateCodePatternReporting:
    @pytest.mark.parametrize("version", _REQUIRED_VERSIONS)
    def test_no_two_rule_ids_share_an_identical_code_pattern(self, version: str):
        data = _load_lint_file(version)
        by_kind_pattern: dict[tuple[str, str], list[str]] = {}
        for rule in data.get("rules", []):
            cp = rule.get("code_pattern")
            if not cp:
                continue
            key = (rule["kind"], cp)
            by_kind_pattern.setdefault(key, []).append(rule["rule_id"])
        dups = {k: ids for k, ids in by_kind_pattern.items() if len(ids) > 1}
        assert not dups, (
            f"lint_rules_{version}.json: multiple rule_ids share an identical "
            f"code_pattern (double-reports the same defect - issue #364 B5): "
            f"{dups}"
        )

    def test_w8140_and_e8501_no_longer_coexist_v14_plus(self):
        """The specific B5 regression: W8140 (OSM-local) and E8501 (real,
        live-extractable from v14+) must not both be present from v14.0
        onward - E8501 wins."""
        for version in _REQUIRED_VERSIONS:
            if float(version) < 14.0:
                continue
            data = _load_lint_file(version)
            ids = {r.get("rule_id") for r in data.get("rules", [])}
            assert not ("W8140" in ids and "E8501" in ids), (
                f"lint_rules_{version}.json: W8140 and E8501 both present - "
                f"B5 duplicate SQL-injection reporting regressed"
            )
            assert "E8501" in ids, (
                f"lint_rules_{version}.json: E8501 missing at v{version} "
                f"(>=14.0) - the SQL-injection fact must survive under the "
                f"real upstream id"
            )


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

    # Sequential-lazy-quantifier detector: the schema description forbids
    # "sequential lazy quantifiers (.*?...*?...*?) which cause polynomial
    # backtracking". The dangerous shape is two or more UNBOUNDED dot-based lazy
    # quantifiers (.*? / .+? / .??) in one matching path — bounded char-class
    # lazies like [^)]*? do not blow up (they cannot cross their excluded char)
    # and our curated W8140/E8501 patterns use those across SEPARATE `|`
    # branches, so they are correctly NOT flagged. We split on top-level `|` and
    # require >=2 unbounded dot-lazies within a single branch.
    _DOT_LAZY_RE = re.compile(r"\.[*+?]\?")

    @staticmethod
    def _split_top_level_alternation(pattern: str) -> list[str]:
        """Split a regex on `|` only at depth 0 (outside groups and char classes)."""
        branches, depth, in_class, buf, i = [], 0, False, [], 0
        while i < len(pattern):
            c = pattern[i]
            if c == "\\" and i + 1 < len(pattern):
                buf.append(pattern[i:i + 2])
                i += 2
                continue
            if in_class:
                buf.append(c)
                if c == "]":
                    in_class = False
            elif c == "[":
                in_class = True
                buf.append(c)
            elif c == "(":
                depth += 1
                buf.append(c)
            elif c == ")":
                depth -= 1
                buf.append(c)
            elif c == "|" and depth == 0:
                branches.append("".join(buf))
                buf = []
            else:
                buf.append(c)
            i += 1
        branches.append("".join(buf))
        return branches

    def test_code_pattern_no_sequential_lazy_quantifiers(self):
        """No code_pattern branch may chain >=2 unbounded dot-lazy quantifiers.

        Locks the schema's "no sequential lazy quantifiers (.*?...*?...*?)"
        clause — the polynomial-backtracking shape distinct from the nested
        (.*)* shape already covered by test_code_pattern_no_redos_shape.
        """
        failures = []
        for version, rule_id, pattern in self._all_rules_with_patterns():
            for branch in self._split_top_level_alternation(pattern):
                if len(self._DOT_LAZY_RE.findall(branch)) >= 2:
                    failures.append(
                        f"  lint_rules_{version}.json {rule_id}: branch {branch!r} "
                        "chains >=2 unbounded dot-lazy quantifiers"
                    )
        assert not failures, (
            "The following code_pattern branches contain sequential lazy "
            "quantifiers (polynomial-backtracking risk):\n" + "\n".join(failures)
        )

    def test_sequential_lazy_detector_actually_fires(self):
        """Sanity: the sequential-lazy detector must catch the schema's bad shape
        and must NOT flag the safe bounded-lazy-in-separate-branches form."""
        bad = ".*?foo.*?bar"
        assert any(
            len(self._DOT_LAZY_RE.findall(b)) >= 2
            for b in self._split_top_level_alternation(bad)
        ), "two sequential dot-lazies in one branch must be caught"
        safe = r"\.execute\s*\([^)]*?x|\bexecute\s*\([^)]*?y"
        assert all(
            len(self._DOT_LAZY_RE.findall(b)) < 2
            for b in self._split_top_level_alternation(safe)
        ), "bounded [^)]*? lazies in separate branches must NOT be flagged"

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
