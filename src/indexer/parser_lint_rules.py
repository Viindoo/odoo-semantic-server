# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/parser_lint_rules.py
"""Extract LintRule entries from Odoo upstream lint configs (M4.5 WI3).

Three live sources for v17+:
    - addons/test_lint/tests/_odoo_checker_*.py — pylint-odoo BaseChecker subclasses
      with `msgs = {"E8502": (msg, sym, doc)}` AST literal
    - addons/test_lint/tests/eslintrc — JSON config with rules dict
    - ruff.toml at repo root (v19+) — TOML with [lint].select = [...]

Static placeholder JSON for v8-v16 (per ADR-0002 §4): `_curate_status: pending`,
empty rules list. Manual curation defer to M6.

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

# --- pylint-odoo source parsing --------------------------------------------

def _parse_pylint_odoo_source(source: str, odoo_version: str) -> list[LintRuleInfo]:
    """Parse a pylint-odoo checker .py file.

    Looks for: `class X(BaseChecker): msgs = {"<rule_id>": ("<msg>", "<sym>", "<doc>")}`.
    Multiple class definitions per file are supported.
    """
    try:
        tree = ast.parse(source)
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
    """Heuristic: test_lint addon present from v17 onward (gates code-extract path)."""
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
        ))
    return out


def parse_lint_rules_for_version(
    odoo_version: str,
    odoo_source_root: str | None = None,
    static_data_dir: str | Path | None = None,
) -> list[LintRuleInfo]:
    """Aggregate lint rules across pylint-odoo / ESLint / ruff + static fallback.

    Pipeline:
      1. If odoo_source_root + version supports test_lint (v17+): code-extract
         pylint-odoo checkers + ESLint config + ruff.toml.
      2. Always merge in any static placeholder data (v8-v16 mostly empty).

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
                for r in _parse_pylint_odoo_source(src, odoo_version):
                    _add(r)
        # ESLint config (the file name is `eslintrc`, no extension, JSON content)
        eslint_path = checker_dir / "eslintrc"
        if eslint_path.is_file():
            try:
                cfg = json.loads(eslint_path.read_text(encoding="utf-8"))
                for r in _parse_eslint_config(cfg, odoo_version):
                    _add(r)
            except (OSError, json.JSONDecodeError):
                pass
        # ruff.toml at repo root
        ruff_path = root / "ruff.toml"
        if ruff_path.is_file():
            try:
                src = ruff_path.read_text(encoding="utf-8", errors="ignore")
                for r in _parse_ruff_toml(src, odoo_version):
                    _add(r)
            except OSError:
                pass

    # Static data — always merge (placeholder for v8-v16 + ad-hoc curated entries).
    for r in _load_static_lint_rules(odoo_version, static_data_dir):
        _add(r)

    return rules


# Silence "imported but unused" if a downstream module re-exports symbols only.
_ = re  # re reserved for future regex-based parsing (e.g. selector → rule).
