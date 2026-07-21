# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/parser_lint_rules.py
"""Extract LintRule entries from Odoo upstream lint configs (M4.5 WI3).

Three live sources, gated per-source by real vendored-in-checkout structure
(see LINT_RULES_MIN_MAJOR in src/constants.py for the full per-version
evidence - this docstring only summarizes):
    - addons/test_lint/tests/_odoo_checker_*.py — pylint-odoo BaseChecker subclasses
      with `msgs = {"E8502": (msg, sym, doc)}` AST literal (v14+; v11-v13 have a
      real checker too but under a filename this glob does not match - see
      LINT_RULES_MIN_MAJOR's comment)
    - addons/test_lint/tests/eslintrc - JSON config with rules dict (v16+).
      Real v19 ships this file with a trailing comma before a closing
      `]`/`}}` (valid JSON5/JSONC, invalid strict JSON) - `_load_json_lenient`
      tolerates exactly that one drift class so the v19 eslint rule family is
      not silently dropped.
    - ruff.toml at repo root (v19 only, confirmed absent v16-v18) - TOML with
      [lint].select = [...]

All 12 versions (v8-v19) have curated static JSON in spec_data/ with
`_curate_status: "complete"` - none are placeholders; that data is the SSOT
for editorial/convention rules the live sources above cannot express (see
issue #364 phase-2 audit). This module's job is only the falsifiable,
source-derivable subset.

Public API:
    parse_lint_rules_for_version(odoo_version, odoo_source_root, static_data_dir)
"""
from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path

from src.constants import LINT_RULES_MIN_MAJOR

from .models import LintRuleInfo
from .parser_util import parse_external_source

# --- pylint-odoo source parsing --------------------------------------------

def _parse_pylint_odoo_source(
    source: str, odoo_version: str, file_path: str | None = None,
) -> list[LintRuleInfo]:
    """Parse a pylint-odoo checker .py file.

    Looks for: `class X(BaseChecker): msgs = {"<rule_id>": ("<msg>", "<sym>", "<doc>")}`.
    Multiple class definitions per file are supported.
    """
    try:
        # External pylint-odoo checker source — scope away SyntaxWarning noise, pass
        # the real path so any diagnostic is attributable (not <unknown>). See parser_util.
        tree = parse_external_source(source, filename=file_path)
    except SyntaxError:
        return []

    rules: list[LintRuleInfo] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        # Find class-body assignment named `msgs` whose value is an ast.Dict.
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign):
                continue
            target_names = [
                t.id for t in stmt.targets if isinstance(t, ast.Name)
            ]
            if "msgs" not in target_names or not isinstance(stmt.value, ast.Dict):
                continue
            for k, v in zip(stmt.value.keys, stmt.value.values):
                if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                    continue
                rule_id = k.value
                # value should be a Tuple: (message, symbol, doc, ...)
                msg = None
                if isinstance(v, ast.Tuple) and v.elts:
                    first = v.elts[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        msg = first.value
                # Severity convention: 'E' → error, 'W' → warning, 'C' → convention.
                first_char = rule_id[0] if rule_id else ""
                severity = (
                    "error" if first_char == "E"
                    else "info" if first_char == "C"
                    else "warning"
                )
                rules.append(LintRuleInfo(
                    rule_id=rule_id,
                    odoo_version=odoo_version,
                    kind="pylint-odoo",
                    message=msg,
                    severity=severity,
                ))
    return rules


# --- ESLint config parsing -------------------------------------------------

def _load_json_lenient(text: str) -> dict | None:
    """Parse JSON, tolerating one specific real-world drift: a trailing comma
    directly before a closing ``]``/``}`` (valid JSON5/JSONC, invalid strict
    JSON).

    Real Odoo v19 ships `addons/test_lint/tests/eslintrc` with exactly this
    shape - a trailing comma after the last object in the
    `no-restricted-syntax` selector array. Strict `json.loads` raises
    `JSONDecodeError` on it; before this fix that exception was caught and
    silently swallowed by the caller, so the entire eslint-odoo rule family
    for v19 was dropped with no signal (issue #364 B3 - the exact
    "widened onto a version that silently returns empty" hazard the B3 brief
    warned about, just for a source-format reason rather than a missing-file
    reason).

    Deliberately narrow: this does NOT implement JSON5 (comments, unquoted
    keys, single-quoted strings) - only the one construct observed in real
    Odoo source. Returns None if the text is not valid JSON even after the
    trailing-comma strip, so callers degrade the same way they always did
    for a genuinely malformed file.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    stripped = re.sub(r",(\s*[}\]])", r"\1", text)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _normalize_severity(sev) -> str:
    """ESLint severity: 'off'/'warn'/'error' or 0/1/2 → string."""
    if isinstance(sev, int):
        return {0: "off", 1: "warning", 2: "error"}.get(sev, "warning")
    if isinstance(sev, str):
        return {"off": "off", "warn": "warning", "error": "error"}.get(sev, sev)
    return "warning"


def _parse_eslint_config(config: dict, odoo_version: str) -> list[LintRuleInfo]:
    """Parse an ESLint config dict (loaded from eslintrc JSON)."""
    rules_section = config.get("rules", {})
    out: list[LintRuleInfo] = []
    for rule_id, raw in rules_section.items():
        # raw can be: "error" | ["error", ...config] | 2 | [2, ...]
        if isinstance(raw, list):
            severity = _normalize_severity(raw[0] if raw else "warning")
        else:
            severity = _normalize_severity(raw)
        out.append(LintRuleInfo(
            rule_id=rule_id,
            odoo_version=odoo_version,
            kind="eslint-odoo",
            severity=severity,
        ))
    return out


# --- ruff TOML parsing -----------------------------------------------------

def _parse_ruff_toml(toml_src: str, odoo_version: str) -> list[LintRuleInfo]:
    """Parse `ruff.toml` (or `[tool.ruff.lint]` section of pyproject).

    Selected rule categories (e.g. 'BLE', 'E', 'I', 'UP') become individual
    LintRule entries. Specific rules in `ignore` are NOT surfaced — they are
    explicit opt-outs, not active rules.
    """
    try:
        data = tomllib.loads(toml_src)
    except tomllib.TOMLDecodeError:
        return []

    # Support both top-level [lint] and nested [tool.ruff.lint] (pyproject style).
    lint_section = data.get("lint") or data.get("tool", {}).get("ruff", {}).get("lint", {})
    select = lint_section.get("select", [])

    out: list[LintRuleInfo] = []
    for category in select:
        if not isinstance(category, str):
            continue
        out.append(LintRuleInfo(
            rule_id=category,
            odoo_version=odoo_version,
            kind="ruff-builtin",
            severity="warning",
        ))
    return out


# --- Version dispatch + static fallback ------------------------------------

def _version_has_test_lint(odoo_version: str) -> bool:
    """Gate the code-extract path at LINT_RULES_MIN_MAJOR (currently v14).

    Not a raw "addon present" check - the test_lint addon itself exists from
    v10 onward. This gates on "a checker file this parser's glob can actually
    recover something from", which is v14+ (see LINT_RULES_MIN_MAJOR's
    comment in src/constants.py for the full per-version evidence, including
    why v11-v13 are excluded despite having real, differently-named source).
    """
    try:
        major = int(odoo_version.split(".")[0])
    except (ValueError, IndexError, AttributeError):
        return False
    return major >= LINT_RULES_MIN_MAJOR


_SPEC_DATA_DIR_DEFAULT = Path(__file__).parent / "spec_data"


def _load_static_lint_rules(
    odoo_version: str, static_data_dir: str | Path | None,
) -> list[LintRuleInfo]:
    """Load static placeholder JSON for a version, if present. Returns [] otherwise."""
    base = Path(static_data_dir) if static_data_dir else _SPEC_DATA_DIR_DEFAULT
    static_path = base / f"lint_rules_{odoo_version}.json"
    if not static_path.is_file():
        return []
    try:
        data = json.loads(static_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[LintRuleInfo] = []
    for r in data.get("rules", []):
        if not isinstance(r, dict) or "rule_id" not in r:
            continue
        out.append(LintRuleInfo(
            rule_id=r["rule_id"],
            odoo_version=odoo_version,
            kind=r.get("kind", "pylint-odoo"),
            message=r.get("message"),
            severity=r.get("severity", "warning"),
            file_pattern=r.get("file_pattern"),
            fix_template=r.get("fix_template"),
            core_symbol_qname=r.get("core_symbol_qname"),
            code_pattern=r.get("code_pattern"),
        ))
    return out


def _apply_code_patterns_overlay(
    rules: list[LintRuleInfo],
    odoo_version: str,
    static_data_dir: str | Path | None,
) -> None:
    """Overlay code_pattern from static JSON onto rules already merged (including live-parse).

    Live-parse rules (v17+) win the dedup race in parse_lint_rules_for_version, so their
    code_pattern would be None even when the static JSON has a pattern for the same rule_id.
    This post-pass patches code_pattern by rule_id from the static data, regardless of which
    source won the dedup. SSOT for patterns stays in the static JSON files.

    Modifies rules in-place. Only sets code_pattern when static JSON has a non-null value
    and the rule's current code_pattern is None (never overwrites an existing pattern).
    """
    base = Path(static_data_dir) if static_data_dir else _SPEC_DATA_DIR_DEFAULT
    static_path = base / f"lint_rules_{odoo_version}.json"
    if not static_path.is_file():
        return
    try:
        data = json.loads(static_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    # Build rule_id -> code_pattern map from static JSON
    pattern_map: dict[str, str] = {}
    for r in data.get("rules", []):
        if isinstance(r, dict) and r.get("rule_id") and r.get("code_pattern"):
            pattern_map[r["rule_id"]] = r["code_pattern"]
    if not pattern_map:
        return
    # Patch rules in-place
    for rule in rules:
        if rule.code_pattern is None and rule.rule_id in pattern_map:
            rule.code_pattern = pattern_map[rule.rule_id]


def parse_lint_rules_for_version(
    odoo_version: str,
    odoo_source_root: str | None = None,
    static_data_dir: str | Path | None = None,
) -> list[LintRuleInfo]:
    """Aggregate lint rules across pylint-odoo / ESLint / ruff + static fallback.

    Pipeline:
      1. If odoo_source_root + version supports test_lint (v{LINT_RULES_MIN_MAJOR}+,
         currently v14): code-extract pylint-odoo checkers + ESLint config + ruff.toml.
      2. Always merge in the curated static JSON (all 12 versions v8-v19 are
         `_curate_status: "complete"` - none are empty placeholders; static
         data is the SSOT for editorial rules the live sources can't express).

    Args:
        odoo_version: Odoo version label, e.g. "17.0".
        odoo_source_root: Optional path to the Odoo upstream checkout.
        static_data_dir: Optional override for the static spec_data directory.
    """
    rules: list[LintRuleInfo] = []
    seen: set[tuple[str, str]] = set()

    def _add(r: LintRuleInfo) -> None:
        key = (r.rule_id, r.kind)
        if key in seen:
            return
        seen.add(key)
        rules.append(r)

    if odoo_source_root and _version_has_test_lint(odoo_version):
        root = Path(odoo_source_root)
        # pylint-odoo checkers
        checker_dir = root / "odoo" / "addons" / "test_lint" / "tests"
        if checker_dir.is_dir():
            for f in sorted(checker_dir.glob("_odoo_checker_*.py")):
                try:
                    src = f.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for r in _parse_pylint_odoo_source(src, odoo_version, file_path=str(f)):
                    _add(r)
        # ESLint config (the file name is `eslintrc`, no extension, JSON content).
        # Uses the lenient loader - real v19 has a trailing comma (see
        # _load_json_lenient's docstring) that strict json.loads rejects.
        eslint_path = checker_dir / "eslintrc"
        if eslint_path.is_file():
            try:
                text = eslint_path.read_text(encoding="utf-8")
            except OSError:
                text = None
            if text is not None:
                cfg = _load_json_lenient(text)
                if cfg is not None:
                    for r in _parse_eslint_config(cfg, odoo_version):
                        _add(r)
        # ruff.toml at repo root
        ruff_path = root / "ruff.toml"
        if ruff_path.is_file():
            try:
                src = ruff_path.read_text(encoding="utf-8", errors="ignore")
                for r in _parse_ruff_toml(src, odoo_version):
                    _add(r)
            except OSError:
                pass

    # Static data - always merge (curated editorial + version-boundary rules,
    # complete for all 12 versions; see module docstring).
    for r in _load_static_lint_rules(odoo_version, static_data_dir):
        _add(r)

    # Overlay code_pattern from static JSON onto all rules (including live-parse winners).
    # Live-parse rules win the dedup race above, so their code_pattern would be None even
    # when static has a pattern for the same rule_id. This post-pass patches them from the
    # static SSOT without reversing the merge order.
    _apply_code_patterns_overlay(rules, odoo_version, static_data_dir)

    return rules
