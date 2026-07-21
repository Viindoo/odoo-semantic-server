# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_parser_lint_rules.py
"""Lint rule parser tests (M4.5 WI3).

Three live sources, gated at v{LINT_RULES_MIN_MAJOR} (currently v14 - see
src/constants.py's LINT_RULES_MIN_MAJOR comment for the full per-version
evidence this boundary is based on):
  - pylint-odoo checker: addons/test_lint/tests/_odoo_checker_*.py with `msgs = {...}`
  - ESLint config:       addons/test_lint/tests/eslintrc (JSON, v16+; v19's real
                          file has a trailing comma - see _load_json_lenient)
  - ruff TOML:           ruff.toml (v19 only, [lint].select = [...])

All 12 versions (v8-v19) have curated static JSON with `_curate_status: "complete"`
- none are empty placeholders (see src/indexer/spec_data/lint_rules_*.json).
"""
import json
import warnings
from pathlib import Path

import pytest

from src.constants import LINT_RULES_MIN_MAJOR
from src.indexer.models import LintRuleInfo
from src.indexer.parser_lint_rules import (
    _load_json_lenient,
    _parse_eslint_config,
    _parse_pylint_odoo_source,
    _parse_ruff_toml,
    _version_has_test_lint,
    parse_lint_rules_for_version,
)
from tests._odoo_checkouts import SURVEYED_MAJORS, checkout_root

# Discovery for the smoke tests below goes through tests/_odoo_checkouts.py
# (issue #364 D2) instead of this file's own dead ``ODOO<N>_SRC`` convention
# (default ``/nonexistent/odooN`` - nothing ever set it, so these tests skipped
# unconditionally everywhere, including a dev box with the checkouts present at
# the conventional path). See that module's docstring for the resolution
# order (legacy env var still honoured).
_V14_ROOT = checkout_root(14)
_V17_ROOT = checkout_root(17)
_V19_ROOT = checkout_root(19)


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


def test_version_has_test_lint_v14_plus():
    """Code-extract path gates at v{LINT_RULES_MIN_MAJOR} (currently v14).

    Widened from v17 (issue #364 B3): v14-v16 have a real `_odoo_checker_*.py`
    checker file with a genuine `msgs = {...}` dict, verified against the real
    checkout (see src/constants.py LINT_RULES_MIN_MAJOR comment). v13 and
    below stay excluded - v13 has real content too but only reachable via a
    differently-named file the current glob does not match (documented, not
    silently dropped).
    """
    assert LINT_RULES_MIN_MAJOR == 14, (
        "this test's boundary assertions assume LINT_RULES_MIN_MAJOR == 14; "
        "update both together if the gate moves again"
    )
    assert _version_has_test_lint("14.0") is True
    assert _version_has_test_lint("15.0") is True
    assert _version_has_test_lint("16.0") is True
    assert _version_has_test_lint("17.0") is True
    assert _version_has_test_lint("18.0") is True
    assert _version_has_test_lint("19.0") is True
    assert _version_has_test_lint("13.0") is False
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
    _V17_ROOT is None or not (_V17_ROOT / "odoo" / "addons" / "test_lint" / "tests").exists(),
    reason=f"Real Odoo 17 test_lint dir not on disk (checked {_V17_ROOT})",
)
def test_parse_lint_rules_smoke_real_v17():
    """Smoke: extract real pylint-odoo + eslint rules from Odoo 17 source."""
    rules = parse_lint_rules_for_version(
        "17.0",
        odoo_source_root=str(_V17_ROOT),
    )
    # Real v17 has at least the gettext checker (E8502) + ESLint base rules.
    rule_ids = {r.rule_id for r in rules}
    assert "E8502" in rule_ids or any(rid.startswith("E") for rid in rule_ids)
    # ESLint baseline
    assert any(r.kind == "eslint-odoo" for r in rules)


@pytest.mark.skipif(
    _V14_ROOT is None or not (_V14_ROOT / "odoo" / "addons" / "test_lint" / "tests").exists(),
    reason=f"Real Odoo 14 test_lint dir not on disk (checked {_V14_ROOT})",
)
def test_parse_lint_rules_smoke_real_v14():
    """Content-level guard for the new v14 boundary (issue #364 B3).

    Real v14 vendors exactly two `_odoo_checker_*.py` files - gettext
    (E8502) and sql_injection (E8501) - both matching the glob cleanly, no
    eslintrc/ruff.toml yet. This asserts the SPECIFIC rule_ids, not just
    "non-empty", so a future regression that matches the wrong file (or a
    silently-broken glob) fails loudly instead of passing on an unrelated hit.
    """
    rules = parse_lint_rules_for_version("14.0", odoo_source_root=str(_V14_ROOT))
    rule_ids = {r.rule_id for r in rules}
    assert "E8502" in rule_ids, f"v14 gettext checker (E8502) missing; got {sorted(rule_ids)}"
    assert "E8501" in rule_ids, f"v14 sql_injection checker (E8501) missing; got {sorted(rule_ids)}"
    assert not any(r.kind == "eslint-odoo" for r in rules), (
        "v14 has no eslintrc in real source; eslint-odoo rules should not appear"
    )


def test_eslint_config_trailing_comma_json5_tolerated():
    """`_load_json_lenient` must parse a trailing comma before a closing
    bracket - the exact shape real Odoo v19 ships in
    `addons/test_lint/tests/eslintrc` (a trailing comma after the last
    selector object in `no-restricted-syntax`).

    Before this fix, strict `json.loads` raised `JSONDecodeError` on this
    file and the caller silently swallowed it - the entire eslint-odoo rule
    family for v19 was dropped with zero signal (issue #364 B3).
    """
    text = """
    {
        "rules": {
            "no-restricted-syntax": [
                "error",
                "PrivateIdentifier",
                {"selector": "X", "message": "Y"},
            ],
            "no-undef": "error"
        }
    }
    """
    cfg = _load_json_lenient(text)
    assert cfg is not None, "trailing-comma JSON5 must still parse"
    rules = _parse_eslint_config(cfg, "19.0")
    rule_ids = {r.rule_id for r in rules}
    assert {"no-restricted-syntax", "no-undef"} <= rule_ids


def test_load_json_lenient_rejects_genuinely_malformed_json():
    """`_load_json_lenient` must NOT mask a real syntax error - only the one
    narrow trailing-comma drift class is tolerated."""
    assert _load_json_lenient("{not json at all") is None


_V19_ESLINTRC = (
    _V19_ROOT / "odoo" / "addons" / "test_lint" / "tests" / "eslintrc"
    if _V19_ROOT is not None else None
)


@pytest.mark.skipif(
    _V19_ESLINTRC is None or not _V19_ESLINTRC.exists(),
    reason=f"Real Odoo 19 eslintrc not on disk (checked {_V19_ESLINTRC})",
)
def test_parse_lint_rules_smoke_real_v19_eslint_survives_trailing_comma():
    """Regression guard against real source: v19's real eslintrc must yield
    eslint-odoo rules through the full `parse_lint_rules_for_version` path.

    Before `_load_json_lenient`, this silently returned zero eslint-odoo
    rules for v19 (the file's trailing comma failed strict `json.loads`,
    the exception was swallowed) while v16-v18 worked fine - a version-
    specific silent gap in an already-in-gate version (issue #364 B3).
    """
    real_text = _V19_ESLINTRC.read_text(encoding="utf-8")
    # Confirm the fixture premise still holds against the live checkout:
    # strict JSON must fail on it (else this test is not exercising the fix).
    with pytest.raises(json.JSONDecodeError):
        json.loads(real_text)

    rules = parse_lint_rules_for_version("19.0", odoo_source_root=str(_V19_ROOT))
    eslint_rules = [r for r in rules if r.kind == "eslint-odoo"]
    assert eslint_rules, (
        "v19 real eslintrc must yield eslint-odoo rules (trailing-comma regression)"
    )


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


def test_v17_attrs_removal_lint_rule_matches_attrs_usage(tmp_path):
    """GAP-1: the v17 catalogue MUST flag `attrs=` view XML as removed-in-v17.

    Behavioral contract: a real attrs="..." occurrence in view XML must be matched
    by the rule's code_pattern (so find_deprecated_usage/lint_check surfaces it),
    and the rule must carry a migrate-to-direct-expression message.
    """
    import re

    real_json = Path(__file__).parent.parent / "src/indexer/spec_data/lint_rules_17.0.json"
    (tmp_path / "lint_rules_17.0.json").write_text(real_json.read_text())
    rules = parse_lint_rules_for_version(
        "17.0", odoo_source_root=None, static_data_dir=str(tmp_path),
    )
    by_id = {r.rule_id: r for r in rules}

    # attrs= rule present, XML-scoped, with a code_pattern that matches real usage.
    attrs_rule = by_id.get("W8168")
    assert attrs_rule is not None, "v17 attrs-removal lint rule (W8168) missing"
    assert attrs_rule.file_pattern == "**/*.xml"
    assert attrs_rule.code_pattern, "W8168 must carry a code_pattern"
    sample = """<field name="x" attrs="{'invisible': [('state','=','draft')]}"/>"""
    assert re.search(attrs_rule.code_pattern, sample), (
        "W8168 code_pattern must match a real attrs= occurrence"
    )
    # Message must steer the migration to direct expressions, not just say "removed".
    assert "v17" in attrs_rule.message
    assert "invisible" in attrs_rule.message or "domain" in attrs_rule.message


def test_v17_states_removal_lint_rule_matches_states_usage(tmp_path):
    """GAP-1: the v17 catalogue MUST flag `states=` view XML as removed-in-v17."""
    import re

    real_json = Path(__file__).parent.parent / "src/indexer/spec_data/lint_rules_17.0.json"
    (tmp_path / "lint_rules_17.0.json").write_text(real_json.read_text())
    rules = parse_lint_rules_for_version(
        "17.0", odoo_source_root=None, static_data_dir=str(tmp_path),
    )
    by_id = {r.rule_id: r for r in rules}

    states_rule = by_id.get("W8169")
    assert states_rule is not None, "v17 states-removal lint rule (W8169) missing"
    assert states_rule.file_pattern == "**/*.xml"
    assert states_rule.code_pattern, "W8169 must carry a code_pattern"
    sample = """<button name="confirm" states="draft,sent"/>"""
    assert re.search(states_rule.code_pattern, sample), (
        "W8169 code_pattern must match a real states= occurrence"
    )
    assert "v17" in states_rule.message


# ---------------------------------------------------------------------------
# WI D2 (issue #364) - wake the dormant guard to its full potential + make
# dormancy legible.
#
# The smoke test above only ever exercised v17 (the one hardcoded version the
# original dead env-var convention happened to name). Now that discovery goes
# through tests/_odoo_checkouts.py, the same live oracle
# (`parse_lint_rules_for_version`) can be exercised across every ELIGIBLE
# surveyed major this machine has a checkout for. "Eligible" is a real
# architectural ceiling, not a test gap: `_version_has_test_lint()` gates the
# code-extract path to v{LINT_RULES_MIN_MAJOR}+ (now v14, widened from v17 by
# issue #364 B3 - see src/constants.py's LINT_RULES_MIN_MAJOR comment for the
# full per-version evidence table this was verified against, not merely
# asserted from the phase-2 audit).
#
# B3 also re-verified v11-v13 directly (the phase-2 audit's own claim about
# v13 undercounted its real file set - it does NOT belong in the gate today,
# but not for the reason the audit gave): v11-v13 vendor a real checker with
# a genuine `msgs = {...}` dict (`_odoo_checkers.py`, rule E3110), but under a
# filename `checker_dir.glob("_odoo_checker_*.py")` does not match (no
# separating "_" before the suffix). v13 additionally has
# `_odoo_checker_sql_injection.py` (E8501) which DOES match the glob today -
# turning the gate on for v13 alone would recover E8501 but silently miss
# E3110 sitting right next to it, i.e. exactly the "partial extraction that
# under-reports" hazard #364 warns is worse than an honest exclusion. So
# v11-v13 stay OUT, and the honest reason is a glob gap, not (as previously
# stated) "no real source below v14". v8-v10 genuinely have zero checker
# source of any name. Skipping majors below v14 here is the documented-reason
# case (#364's own acceptance bar, criterion (b)), not a guard-that-doesn't-guard.
#
# SCOPE NOTE (do not confuse this with a content-parity test): the test below
# asserts only that the live parse recovers a NON-EMPTY result for each
# eligible version - i.e. that the oracle actually parsed real source instead
# of silently degrading to an empty list. It does NOT compare curated
# `lint_rules_<version>.json` field values (message/severity/kind) against
# the live parse - that is a separate, dedicated content-parity effort, out
# of scope for this file (see phase3-synthesis.md S2 vs S3). Content-level
# coverage for the specific new v14 boundary is asserted separately by
# `test_parse_lint_rules_smoke_real_v14` above.
# ---------------------------------------------------------------------------


@pytest.mark.odoo_source
@pytest.mark.parametrize("major", SURVEYED_MAJORS)
def test_live_lint_rules_nonempty_where_eligible(major, tmp_path):
    """T-D2 - business rule: for every surveyed major BOTH eligible (test_lint
    source is vendored in the checkout from v{LINT_RULES_MIN_MAJOR} onward)
    AND with a checkout on this machine, a live parse of the real
    pylint-odoo/eslint/ruff source must recover at least one LintRule
    (existence guard only; see module note above).
    """
    version = f"{major}.0"
    if not _version_has_test_lint(version):
        pytest.skip(
            f"v{major}: below v{LINT_RULES_MIN_MAJOR} - either no checker source "
            "at all (v8-v10), or real source exists but under a filename this "
            "parser's glob does not match (v11-v13, see src/constants.py "
            "LINT_RULES_MIN_MAJOR comment for the exact per-version reason) - "
            "architectural gap, not a guard failure."
        )
    root = checkout_root(major)
    if root is None:
        pytest.skip(f"Odoo {major} checkout not found (set OSM_ODOO_CHECKOUTS to override)")
    empty_static = tmp_path / "empty_static"
    empty_static.mkdir()
    rules = parse_lint_rules_for_version(
        version, odoo_source_root=str(root), static_data_dir=str(empty_static),
    )
    assert len(rules) > 0, (
        f"v{major}: live parse of real pylint-odoo/eslint/ruff source returned ZERO "
        "LintRule entries - the oracle likely failed silently instead of parsing"
    )


def test_lint_rules_live_parser_coverage_is_reported():
    """T-D2 coverage visibility - business rule: a skipped guard must
    announce itself, not just fade into individual SKIPPED lines nobody
    aggregates. Reports, via a warning always shown in pytest's terminal
    summary (regardless of -q/-v), how many of the 12 surveyed majors are
    even architecturally ELIGIBLE for a live lint_rules oracle, and how many
    of those this session actually has a checkout for. Also self-checks the
    surveyed range itself so a future accidental narrowing of
    SURVEYED_MAJORS cannot silently shrink this report's coverage.
    """
    assert SURVEYED_MAJORS == list(range(8, 20))
    eligible = [m for m in SURVEYED_MAJORS if _version_has_test_lint(f"{m}.0")]
    exercised = [m for m in eligible if checkout_root(m) is not None]
    warnings.warn(
        UserWarning(
            "lint_rules live-parser coverage: "
            f"{len(exercised)}/{len(SURVEYED_MAJORS)} surveyed majors exercised "
            f"({len(eligible)}/{len(SURVEYED_MAJORS)} are architecturally eligible - "
            f"v8-v{LINT_RULES_MIN_MAJOR - 1} excluded: v8-v10 have zero checker source "
            "of any name; v11-v13 have real source (`_odoo_checkers.py`) but under a "
            "filename the glob does not match, an honest documented exclusion, not a "
            f"silent gap - see src/constants.py LINT_RULES_MIN_MAJOR comment; "
            f"exercised now: {exercised or 'NONE'}). Set "
            "OSM_ODOO_CHECKOUTS to point at your checkouts if this reads 0. NOTE: this "
            "test verifies EXISTENCE only - it does not verify the curated JSON's field "
            "values (message/severity/kind) match real source; see phase3-synthesis.md S3 "
            "for the separate content-parity work."
        ),
        stacklevel=1,
    )
