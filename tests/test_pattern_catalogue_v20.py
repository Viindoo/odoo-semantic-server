# SPDX-License-Identifier: AGPL-3.0-or-later
"""Odoo 20.0 guards for the curated pattern catalogue (src/data/patterns.json).

Rule: ``suggest_pattern(odoo_version='20.0')`` never serves a pattern that is wrong at 20.0.
A pattern is served for a version when ``float(min) <= v`` and (``max`` is unset or
``v <= float(max)``) - see ``_version_covers`` in src/mcp/tools/guidance.py.

Curated-data bar (ADR-0055), three layers:

1. Live oracle (needs a real 20.0 checkout via ``tests._odoo_checkouts.checkout_root(20)``,
   skipped when absent): every pattern served at 20.0 has a ``file_ref`` whose file exists in
   that checkout with the line inside it, every snippet line of a pattern that starts at 20.0
   is a line of its ``file_ref`` file, and each idiom listed in ``_REMOVED_AT_20`` is absent
   from the checkout.
2. Idiom oracle (no checkout): no pattern served at 20.0 has a snippet that contains an idiom
   from ``_REMOVED_AT_20``.
3. Anti-truncation: the hard-coded ids below are independent of patterns.json, so capping one
   less pattern, or dropping a 20.0 pattern, makes a test fail.

Patterns with ``odoo_version_max: "19.0"`` are wrong at 20.0; their 8.0-19.0 content is
untouched.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests._odoo_checkouts import checkout_root

_PATTERNS_PATH = Path(__file__).resolve().parent.parent / "src" / "data" / "patterns.json"
_VERSION = 20.0
_MAJOR = 20

# Patterns whose snippet or gotchas are wrong at 20.0 (removed API, moved file, Owl 2 syntax) and
# so end at 19.0. Evidence for each is in the 20.0 checkout; see the idiom oracle for the
# mechanically checkable ones.
_CAPPED_AT_19 = {
    "safe-eval-v19-signature",
    "read-group-v19-deprecated",
    "sql-constraint-name-uniq",
    "record-rule-domain-force",
    "owl-private-attr-v19",
    "owl-component-state-vs-props",
    "owl-services-injection",
    "owl-onmounted-lifecycle",
    "owl-usestate-reactive-mutation",
    "owl-useservice-reactivity",
    "owl-wysiwyg-not-contenteditable",
    "owl-field-widget-register-v17",
    "owl-field-widget-with-template-v17",
    "owl-t-on-handler-binding",
    "owl-dialog-default-slot",
    "multi-company-ir-rule-domain-force",
    "security-ir-rule-portal-domain",
    "domain-or-operator-prefix-notation",
    "security-acl-csv-group-model",
    "security-access-csv-v12",
    "mail-thread-override-message-post",
    "company-dependent-field",
    "multi-company-property-field",
    "xml-tree-optional-column-v15",
    "inherit-extension-pattern-v10",
    "odoo-module-owl1-component-v15",
    "use-position-hook-v15",
    "odoo-module-view-extension-v15",
    "api-depends-dotted-path-v10",
    "query-init-cr-vs-env",
    "context-as-frozendict",
    "sudo-vs-with-user",
    "selection-extend-callable",
    "cron-deferred-task",
    "wizard-transient-default-get-context",
    "wizard-action-close-vs-open",
    "manifest-transition-v10",
    "python3-super-no-args-v11",
    "ir-sequence-lambda-default-v11",
    "mail-thread-mixin-v11",
    "inherits-delegation-v12",
    "api-model-class-method-v12",
    "super-py3-create-v12",
    "check-company-constrains-v13",
    "model-extension-mail-thread-v14",
    "view-inherit-xpath-v10",
    "computed-field-stored-v10",
    "one2many-domain-v13",
    "compute-inverse-editable-field-v12",
}

# Patterns that start at 20.0: the idioms that replace a capped one, plus 20.0-only mechanisms.
_NEW_AT_20 = {
    "owl3-use-props-types",
    "owl3-proxy-and-signal-state",
    "owl3-signal-ref-onmounted",
    "owl3-template-this-prefix",
    "owl3-field-widget-register",
    "owl3-dialog-slots",
    "owl3-use-service-orm",
    "owl3-plugin-class",
    "models-constraint-v20",
    "ormcache-from-odoo-api-v20",
    "read-group-tuples-v20",
    "sudo-with-user-check-access-v20",
    "has-access-filtered-access-v20",
    "cached-model-v20",
    "card-view-kanban-card-id-v20",
    "message-post-override-v20",
    "company-dependent-field-v20",
    "cron-record-v20",
    "api-private-v20",
    "safe-eval-v20",
    "ir-access-csv-group-operation-v20",
    "ir-access-company-restriction-v20",
    "ir-access-portal-domain-v20",
    "ir-access-personal-or-domain-v20",
    "selection-callable-v20",
    "post-init-hook-env-v20",
    "context-frozendict-v20",
    "wizard-default-get-v20",
    "wizard-act-window-close-v20",
    "check-company-auto-v20",
    "xml-list-optional-column-v20",
    "compute-inverse-editable-field-v20",
}

# (name, regex applied to a snippet / to the checkout text, checkout globs, evidence).
# Each idiom is absent from the 20.0 checkout, so a snippet that uses it is wrong at 20.0.
_REMOVED_AT_20 = [
    (
        "useState imported from owl",
        r'import\s*\{[^}]*\buseState\b[^}]*\}\s*from\s*["\']@odoo/owl["\']',
        ("addons/**/*.js",),
        "Owl 3 exports proxy/signal, not useState (addons/web/static/lib/owl/owl.js export list)",
    ),
    (
        "ir.rule record",
        r'''model\s*=\s*["']ir\.rule["']''',
        ("addons/**/*.xml", "odoo/addons/**/*.xml"),
        "access rules are rows of ir.access.csv (odoo/addons/base/models/ir_access.py)",
    ),
    (
        "ir.model.access.csv header",
        r"model_id:id,group_id:id,perm_read",
        ("addons/**/*.csv", "odoo/addons/**/*.csv"),
        "the access file is ir.access.csv: id,name,model_id,group_id/id,operation,domain",
    ),
    (
        "api.returns decorator",
        r"@api\.returns\b",
        ("addons/**/*.py", "odoo/**/*.py"),
        "odoo/api/__init__.py exports no returns decorator",
    ),
    (
        "check_access_rights / check_access_rule call",
        r"(?<![_\w])check_access_(?:rights|rule)\(",
        ("addons/**/*.py", "odoo/**/*.py"),
        "BaseModel has check_access / has_access / _filtered_access (odoo/orm/models.py)",
    ),
    (
        "SingleTransactionCase",
        r"\bSingleTransactionCase\b",
        ("addons/**/*.py", "odoo/**/*.py"),
        "odoo/tests/common.py defines no SingleTransactionCase",
    ),
    (
        "_sql_constraints assignment",
        r"^\s*_sql_constraints\s*=",
        ("addons/**/*.py", "odoo/**/*.py"),
        "odoo/orm/model_classes.py: '_sql_constraints' is not supported, define models.Constraint",
    ),
    (
        "owl 2 hooks namespace",
        r"\bowl\.hooks\b|\bQWeb\.registerComponent\b",
        ("addons/**/*.js",),
        "Owl 3 has neither owl.hooks nor QWeb.registerComponent",
    ),
]


def _load() -> list[dict]:
    return json.loads(_PATTERNS_PATH.read_text(encoding="utf-8"))


def _serves(p: dict, version: float = _VERSION) -> bool:
    """Same window rule as src/mcp/tools/guidance.py::_version_covers (numeric compare)."""
    if float(p["odoo_version_min"]) > version:
        return False
    vmax = p.get("odoo_version_max")
    return vmax is None or float(vmax) >= version


def _resolve_file(root: Path, file_ref: str) -> tuple[Path | None, int]:
    path, _, line = file_ref.rpartition(":")
    for candidate in (root / path, root / "odoo" / path):
        if candidate.is_file():
            return candidate, int(line)
    return None, int(line)


# ---------------------------------------------------------------------------
# Layer 3 - anti-truncation (no checkout)
# ---------------------------------------------------------------------------

def test_wrong_at_20_patterns_end_at_19():
    """Each pattern in the wrong-at-20.0 list carries max 19.0 and is not served at 20.0."""
    by_id = {p["pattern_id"]: p for p in _load()}
    missing = sorted(_CAPPED_AT_19 - set(by_id))
    assert not missing, f"capped patterns deleted from the catalogue: {missing}"
    served = sorted(pid for pid in _CAPPED_AT_19 if _serves(by_id[pid]))
    assert not served, f"patterns wrong at 20.0 are still served for 20.0: {served}"
    wrong_max = sorted(pid for pid in _CAPPED_AT_19 if by_id[pid].get("odoo_version_max") != "19.0")
    assert not wrong_max, f"capped patterns must end exactly at 19.0: {wrong_max}"


def test_20_idioms_exist_as_20_patterns():
    """The 20.0 idioms are present, open-ended, and start at 20.0 (never served below it)."""
    by_id = {p["pattern_id"]: p for p in _load()}
    missing = sorted(_NEW_AT_20 - set(by_id))
    assert not missing, f"20.0 patterns deleted from the catalogue: {missing}"
    bad = sorted(
        pid for pid in _NEW_AT_20
        if by_id[pid]["odoo_version_min"] != "20.0"
        or by_id[pid].get("odoo_version_max") is not None
    )
    assert not bad, f"20.0 patterns must be min 20.0 with no max: {bad}"
    served_below = sorted(pid for pid in _NEW_AT_20 if _serves(by_id[pid], 19.0))
    assert not served_below, f"20.0 patterns leak into 19.0: {served_below}"


def test_version_windows_are_not_inverted():
    """A max below the min would hide the pattern from every version (numeric compare)."""
    inverted = [
        (p["pattern_id"], p["odoo_version_min"], p["odoo_version_max"])
        for p in _load()
        if p.get("odoo_version_max") is not None
        and float(p["odoo_version_max"]) < float(p["odoo_version_min"])
    ]
    assert not inverted, f"patterns with max < min: {inverted}"


# ---------------------------------------------------------------------------
# Layer 2 - idiom oracle against the snippets served at 20.0 (no checkout)
# ---------------------------------------------------------------------------

def test_no_snippet_served_at_20_uses_an_idiom_removed_at_20():
    """suggest_pattern('20.0') never shows code that calls an API 20.0 does not have."""
    offenders = []
    for p in _load():
        if not _serves(p):
            continue
        for name, regex, _globs, _evidence in _REMOVED_AT_20:
            if re.search(regex, p["snippet_text"], re.M):
                offenders.append((p["pattern_id"], name))
    assert not offenders, (
        "patterns served at 20.0 whose snippet uses an idiom absent at 20.0 "
        f"(cap them at 19.0 or rewrite): {offenders}"
    )


# ---------------------------------------------------------------------------
# Layer 1 - live oracle against the real 20.0 checkout
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def root20() -> Path:
    root = checkout_root(_MAJOR)
    if root is None:
        pytest.skip("no Odoo 20.0 checkout (set ODOO20_SRC or OSM_ODOO_CHECKOUTS)")
    return root


def test_served_pattern_file_ref_exists_in_20_checkout(root20):
    """Every pattern served at 20.0 points at a file that exists at 20.0, at a line inside it."""
    broken = []
    for p in _load():
        if not _serves(p):
            continue
        target, line = _resolve_file(root20, p["file_ref"])
        if target is None:
            broken.append((p["pattern_id"], p["file_ref"], "missing file"))
            continue
        total = sum(1 for _ in target.open(encoding="utf-8", errors="replace"))
        if not 1 <= line <= total:
            broken.append((p["pattern_id"], p["file_ref"], f"file has {total} lines"))
    assert not broken, (
        f"{len(broken)} pattern(s) served at 20.0 with a file_ref absent from the 20.0 checkout "
        f"(point it at the 20.0 code or cap the pattern at 19.0): {broken}"
    )


_COMMENT_PREFIXES = ("#", "//", "/*", "*", "<!--")


def test_20_pattern_snippets_are_lines_of_their_file_ref(root20):
    """A pattern that starts at 20.0 quotes its file_ref file: each code line is in that file."""
    problems = []
    for p in _load():
        if p["odoo_version_min"] != "20.0":
            continue
        target, _line = _resolve_file(root20, p["file_ref"])
        if target is None:
            problems.append((p["pattern_id"], "file_ref file missing"))
            continue
        file_lines = {ln.strip() for ln in target.read_text(encoding="utf-8").splitlines()}
        for raw in p["snippet_text"].splitlines():
            code = raw.strip()
            if not code or code.startswith(_COMMENT_PREFIXES):
                continue
            if code not in file_lines:
                problems.append((p["pattern_id"], code))
    assert not problems, f"snippet lines that are not in the 20.0 file_ref file: {problems}"


def _checkout_matches(root: Path, regex: str, globs: tuple[str, ...]) -> list[str]:
    pattern = re.compile(regex, re.M)
    hits: list[str] = []
    seen: set[Path] = set()
    for glob in globs:
        for path in root.glob(glob):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
                hits.append(str(path.relative_to(root)))
                if len(hits) >= 3:
                    return hits
    return hits


@pytest.mark.parametrize(
    "name,regex,globs,evidence", _REMOVED_AT_20, ids=[r[0] for r in _REMOVED_AT_20]
)
def test_removed_idiom_is_absent_from_20_checkout(root20, name, regex, globs, evidence):
    """The oracle itself: every idiom the snippet check rejects really is absent at 20.0."""
    hits = _checkout_matches(root20, regex, globs)
    assert not hits, (
        f"{name!r} is present in the 20.0 checkout ({hits}); "
        f"drop it from _REMOVED_AT_20 ({evidence})"
    )
