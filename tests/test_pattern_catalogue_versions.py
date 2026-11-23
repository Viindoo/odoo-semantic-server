# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-version guards for the curated pattern catalogue (src/data/patterns.json).

Rule: ``suggest_pattern(odoo_version=N)`` never serves a pattern that is wrong at N, for every
surveyed major N (``SURVEYED_MAJORS``, 8..20). A pattern is served for a version when
``float(min) <= v`` and (``max`` is unset or ``v <= float(max)``) - see ``_version_covers`` in
src/mcp/tools/guidance.py. A pattern is wrong at N when its snippet uses an API or idiom N does not
have, its gotchas state something false at N, or its ``file_ref`` does not hold that code at N.

Curated-data bar (ADR-0055), three layers:

1. Live oracle against the real checkout of every major
   (``tests._odoo_checkouts.checkout_root(N)``, skipped per major when absent):
   - every pattern served at N has a ``file_ref`` whose file exists at N with the line inside it;
   - a quoted pattern (no ``illustrative`` marker) has every non-comment snippet line as a line of
     its file_ref file at N;
   - an illustrative pattern (``illustrative: {reason, anchor}``) has its anchor regex matching in
     its file_ref file at N;
   - at the newest version it is served for, the cited line sits on the code (a quoted line, or
     within ``_ANCHOR_REACH`` lines of an anchor match);
   - every ``core_symbol_names`` entry resolves to a symbol the core parser emits at N;
   - every API in ``_API_AVAILABILITY`` is present in the checkout exactly at the majors listed.
2. Idiom oracle (no checkout): no pattern served at N has a snippet that uses an API absent at N.
3. Anti-truncation: the hard-coded id sets below are independent of patterns.json, so capping one
   less pattern, or dropping a 20.0 pattern, makes a test fail.
"""
from __future__ import annotations

import json
import os
import re
from functools import cache
from pathlib import Path
from typing import NamedTuple

import pytest

from tests._odoo_checkouts import SURVEYED_MAJORS, checkout_root

_PATTERNS_PATH = Path(__file__).resolve().parent.parent / "src" / "data" / "patterns.json"

# An anchor match must start within this many lines of the cited line (newest served version).
_ANCHOR_REACH = 40
# A quoted pattern's cited line is a quoted line, give or take this many lines.
_CITED_LINE_SLACK = 2
_COMMENT_PREFIXES = ("#", "//", "/*", "*", "<!--")

# Patterns whose snippet or gotchas are wrong at 20.0 (removed API, moved file, Owl 2 syntax) and
# so end at or before 19.0 (an earlier end means the idiom is wrong from a lower major, or the
# cited file is gone from it; a twin carries the idiom for the later window). Evidence for each is
# in the checkouts; see the idiom oracle for the mechanically checkable ones.
_ENDS_BEFORE_20 = {
    "sql-constraint-name-uniq-v19",
    "cron-deferred-task-v18",
    "security-ir-rule-portal-domain-v19",
    "view-inherit-xpath-v10-v16",
    "xml-tree-optional-column-v15-v18",
    "owl-useservice-reactivity-v19",
    "owl-wysiwyg-not-contenteditable-v18",
    "portal-sudo-public-user-access",
    "portal-compute-with-sudo",
    "ir-attachment-create-res-model-res-id",
    "domain-child-of-parent-of",
    "domain-filter-domain-search-view",
    "report-qweb-t-set-subtotal",
    "mail-thread-mixin-message-post",
    "website-published-mixin",
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

class _Api(NamedTuple):
    """An API/idiom and the majors whose checkout contains it (oracle for the snippet check).

    ``needle`` is a literal substring of every match (cheap prefilter), ``regex`` the idiom,
    ``suffixes`` the file types scanned. Test directories are skipped unless ``scan_tests``.
    """

    name: str
    needle: str
    regex: str
    suffixes: tuple[str, ...]
    present: frozenset[int]
    evidence: str
    scan_tests: bool = False


# Each regex matches a checkout exactly at the listed majors (test_api_availability_table_matches_
# checkout proves it), so a snippet that uses the idiom must only be served at those majors.
_API_AVAILABILITY: list[_Api] = [
    _Api(
        '@api.depends_context decorator',
        '@api.depends_context',
        r"@api\.depends_context\(",
        ('.py',),
        frozenset(range(13, 21)),
        'addons/stock/models/product.py:96 @ 13.0; absent before 13.0',
    ),
    _Api(
        '@api.model_create_multi decorator',
        '@api.model_create_multi',
        r"@api\.model_create_multi\b",
        ('.py',),
        frozenset(range(12, 21)),
        'addons/partner_autocomplete/models/res_partner.py:166 @ 12.0; absent before 12.0',
    ),
    _Api(
        '@api.multi decorator',
        '@api.multi',
        r"^\s*@api\.multi\b",
        ('.py',),
        frozenset(range(8, 13)),
        'addons/stock/wizard/stock_transfer_details.py:112 @ 8.0; absent from 13.0',
    ),
    _Api(
        '@api.one decorator',
        '@api.one',
        r"^\s*@api\.one\b",
        ('.py',),
        frozenset(range(8, 13)),
        'addons/stock/wizard/stock_transfer_details.py:75 @ 8.0; absent from 13.0',
    ),
    _Api(
        '@api.returns decorator',
        '@api.returns',
        r"@api\.returns\b",
        ('.py',),
        frozenset(range(8, 19)),
        'addons/account/account.py:1023 @ 8.0; absent from 19.0',
    ),
    _Api(
        'search_fetch call',
        'search_fetch(',
        r"\.search_fetch\(",
        ('.py',),
        frozenset(range(17, 21)),
        'addons/stock/models/stock_move.py:1903 @ 17.0; absent before 17.0',
    ),
    _Api(
        'invalidate_recordset call',
        'invalidate_recordset',
        r"\.invalidate_recordset\(",
        ('.py',),
        frozenset(range(16, 21)),
        'addons/stock/models/stock_orderpoint.py:411 @ 16.0; absent before 16.0',
    ),
    _Api(
        'flush_all call',
        'flush_all(',
        r"\.flush_all\(",
        ('.py',),
        frozenset(range(16, 21)),
        'addons/stock/models/stock_move.py:1528 @ 16.0; absent before 16.0',
    ),
    _Api(
        'with_company call',
        'with_company(',
        r"\.with_company\(",
        ('.py',),
        frozenset(range(14, 21)),
        'addons/stock/wizard/product_replenish.py:80 @ 14.0; absent before 14.0',
    ),
    _Api(
        '_check_company_auto attribute',
        '_check_company_auto',
        r"^\s*_check_company_auto\s*=",
        ('.py',),
        frozenset(range(13, 21)),
        'addons/stock/models/product.py:562 @ 13.0; absent before 13.0',
    ),
    _Api(
        'tracking=True field option',
        'tracking=True',
        r"\btracking=True\b",
        ('.py',),
        frozenset(range(13, 21)),
        'addons/stock/models/product.py:567 @ 13.0; absent before 13.0',
    ),
    _Api(
        'track_visibility field option',
        'track_visibility',
        r"\btrack_visibility=",
        ('.py',),
        frozenset(range(8, 14)),
        'addons/stock/stock.py:824 @ 8.0; absent from 14.0',
    ),
    _Api(
        'name_get definition',
        'def name_get',
        r"^\s*def name_get\(",
        ('.py',),
        frozenset(range(8, 18)),
        'addons/stock/stock.py:98 @ 8.0; absent from 18.0',
    ),
    _Api(
        'aggregator field option',
        'aggregator=',
        r"\baggregator=",
        ('.py',),
        frozenset(range(18, 21)),
        'addons/account/report/account_invoice_report.py:53 @ 18.0; absent before 18.0',
    ),
    _Api(
        'group_operator field option',
        'group_operator=',
        r"\bgroup_operator=",
        ('.py',),
        frozenset(range(8, 18)),
        'addons/account/report/account_invoice_report.py:80 @ 8.0; absent from 18.0',
    ),
    _Api(
        'ir.property model',
        'ir.property',
        '[\'\\"]ir\\.property[\'\\"]',
        ('.py',),
        frozenset(range(8, 18)),
        'addons/account/account.py:672 @ 8.0; absent from 18.0',
    ),
    _Api(
        'models.Constraint attribute',
        'models.Constraint(',
        r"=\s*models\.Constraint\(",
        ('.py',),
        frozenset(range(19, 21)),
        'addons/website_crm_iap_reveal/models/crm_reveal_rule.py:66 @ 19.0; absent before 19.0',
    ),
    _Api(
        '_sql_constraints assignment',
        '_sql_constraints',
        r"^\s*_sql_constraints\s*=",
        ('.py',),
        frozenset(range(8, 19)),
        'addons/account_payment/account_payment.py:331 @ 8.0; absent from 19.0',
    ),
    _Api(
        'SavepointCase class',
        'SavepointCase',
        r"^class SavepointCase\b",
        ('.py',),
        frozenset(range(8, 17)),
        'openerp/tests/common.py:191 @ 8.0; absent from 17.0',
        scan_tests=True,
    ),
    _Api(
        'SingleTransactionCase class',
        'SingleTransactionCase',
        r"^class SingleTransactionCase\b",
        ('.py',),
        frozenset(range(8, 20)),
        'openerp/tests/common.py:169 @ 8.0; absent from 20.0',
        scan_tests=True,
    ),
    _Api(
        'formatted_read_group definition',
        'def formatted_read_group',
        r"^\s*def formatted_read_group\(",
        ('.py',),
        frozenset(range(19, 21)),
        'addons/web/models/models.py:802 @ 19.0; absent before 19.0',
    ),
    _Api(
        'safe_eval single-context signature',
        'def safe_eval',
        r"^def safe_eval\(expr, /, context=None",
        ('.py',),
        frozenset(range(19, 21)),
        'odoo/tools/safe_eval.py:369 @ 19.0; absent before 19.0',
    ),
    _Api(
        't-out directive',
        't-out=',
        r"\bt-out=",
        ('.xml',),
        frozenset(range(15, 21)),
        'addons/http_routing/views/http_routing_template.xml:106 @ 15.0; absent before 15.0',
    ),
    _Api(
        '<tree> view root',
        '<tree',
        r"<tree[\s>]",
        ('.xml',),
        frozenset(range(8, 18)),
        'addons/account_analytic_analysis/account_analytic_analysis_view.xml:169 @ 8.0; '
        'absent from 18.0',
    ),
    _Api(
        'ir.rule record',
        'ir.rule',
        'model\\s*=\\s*["\']ir\\.rule["\']',
        ('.xml',),
        frozenset(range(8, 20)),
        'addons/account_payment/security/account_payment_security.xml:16 @ 8.0; absent from 20.0',
    ),
    _Api(
        'ir.model.access.csv header',
        'perm_read',
        r"model_id:id,group_id:id,perm_read",
        ('.csv',),
        frozenset(range(8, 20)),
        'addons/account_analytic_analysis/security/ir.model.access.csv:1 @ 8.0; absent from 20.0',
    ),
    _Api(
        'useState imported from owl',
        'useState',
        'import\\s*\\{[^}]*\\buseState\\b[^}]*\\}\\s*from\\s*["\\\']@odoo/owl["\\\']',
        ('.js',),
        frozenset(range(16, 20)),
        'addons/web/static/src/core/transition.js:4 @ 16.0; absent from 20.0; absent before 16.0',
    ),
    _Api(
        'owl.hooks namespace',
        'owl.hooks',
        r"\bowl\.hooks\b|\bQWeb\.registerComponent\b",
        ('.js',),
        frozenset(range(14, 16)),
        'addons/web/static/src/js/fields/abstract_field_owl.js:7 @ 14.0; '
        'absent from 16.0; absent before 14.0',
    ),
    _Api(
        'check_access_rights / check_access_rule call',
        'check_access_',
        r"(?<![_\w])check_access_(?:rights|rule)\(",
        ('.py',),
        frozenset(range(8, 20)),
        'addons/stock/wizard/stock_change_product_qty.py:69 @ 8.0; absent from 20.0',
    ),
]


def _load() -> list[dict]:
    return json.loads(_PATTERNS_PATH.read_text(encoding="utf-8"))


def _serves(p: dict, version: float) -> bool:
    """Same window rule as src/mcp/tools/guidance.py::_version_covers (numeric compare)."""
    if float(p["odoo_version_min"]) > version:
        return False
    vmax = p.get("odoo_version_max")
    return vmax is None or float(vmax) >= version


def _newest_served(p: dict) -> int:
    """Highest surveyed major the pattern is served for."""
    return max(m for m in SURVEYED_MAJORS if _serves(p, float(m)))


def _resolve_file(root: Path, file_ref: str) -> tuple[Path | None, int]:
    path, _, line = file_ref.rpartition(":")
    for candidate in (root / path, root / "odoo" / path):
        if candidate.is_file():
            return candidate, int(line)
    return None, int(line)


def _code_lines(snippet: str) -> list[str]:
    """Stripped non-comment, non-blank lines of a snippet."""
    return [
        ln.strip()
        for ln in snippet.splitlines()
        if ln.strip() and not ln.strip().startswith(_COMMENT_PREFIXES)
    ]


_NEGATIVE_COMMENT = re.compile(
    r"anti-pattern|\bbad\b|\bunsafe\b|\bwrong\b|\bremoved\b|and earlier|\bup to v\d+"
    r"|\bdeprecated\b",
    re.I,
)
_POSITIVE_COMMENT = re.compile(r"\bcorrect\b|\bgood\b|\bsafe\b|v\d+\+|and later", re.I)


def _recommended_code(snippet: str) -> str:
    """Snippet code without the counter-examples it shows on purpose.

    A comment line naming an anti-pattern, a removed or deprecated form, or an earlier series opens
    a counter-example block that a blank line or a comment naming the correct form closes.
    """
    kept: list[str] = []
    negative = False
    for raw in snippet.splitlines():
        line = raw.strip()
        if not line:
            negative = False
        elif line.startswith(_COMMENT_PREFIXES):
            if _NEGATIVE_COMMENT.search(line):
                negative = True
            elif _POSITIVE_COMMENT.search(line):
                negative = False
        elif not negative:
            kept.append(line)
    return "\n".join(kept)


@cache
def _file_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Layer 3 - anti-truncation and catalogue shape (no checkout)
# ---------------------------------------------------------------------------

def test_wrong_at_20_patterns_end_before_20():
    """Each pattern in the wrong-at-20.0 list has a max below 20.0 and is not served at 20.0."""
    by_id = {p["pattern_id"]: p for p in _load()}
    missing = sorted(_ENDS_BEFORE_20 - set(by_id))
    assert not missing, f"capped patterns deleted from the catalogue: {missing}"
    served = sorted(pid for pid in _ENDS_BEFORE_20 if _serves(by_id[pid], 20.0))
    assert not served, f"patterns wrong at 20.0 are still served for 20.0: {served}"
    unbounded = sorted(
        pid for pid in _ENDS_BEFORE_20
        if by_id[pid].get("odoo_version_max") is None
        or float(by_id[pid]["odoo_version_max"]) > 19.0
    )
    assert not unbounded, f"capped patterns must end at 19.0 or earlier: {unbounded}"


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


def test_every_surveyed_version_is_served_at_least_one_pattern():
    """No surveyed major gets an empty catalogue."""
    patterns = _load()
    empty = [m for m in SURVEYED_MAJORS if not any(_serves(p, float(m)) for p in patterns)]
    assert not empty, f"majors with no served pattern: {empty}"


def test_illustrative_markers_are_well_formed():
    """The marker carries a reason and a compilable anchor; nothing else uses the key."""
    problems = []
    for p in _load():
        marker = p.get("illustrative")
        if marker is None:
            continue
        try:
            re.compile(marker["anchor"], re.M)
        except re.error as exc:
            problems.append((p["pattern_id"], f"anchor does not compile: {exc}"))
        if not marker["reason"].strip():
            problems.append((p["pattern_id"], "empty reason"))
    assert not problems, f"malformed illustrative markers: {problems}"


# ---------------------------------------------------------------------------
# Layer 2 - idiom oracle against the snippets served at each version (no checkout)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("major", SURVEYED_MAJORS)
def test_no_snippet_uses_an_api_absent_at_its_version(major):
    """suggest_pattern(N) never shows code that calls an API N does not have."""
    offenders = []
    for p in _load():
        if not _serves(p, float(major)):
            continue
        code = _recommended_code(p["snippet_text"])
        for api in _API_AVAILABILITY:
            if major not in api.present and re.search(api.regex, code, re.M):
                offenders.append((p["pattern_id"], api.name))
    assert not offenders, (
        f"patterns served at {major}.0 whose snippet uses an API absent there "
        f"(narrow the window or rewrite the snippet): {offenders}"
    )


# ---------------------------------------------------------------------------
# Layer 1 - live oracle against the real checkout of every surveyed major
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", params=SURVEYED_MAJORS, ids=lambda m: f"v{m}")
def checkout(request) -> tuple[int, Path]:
    root = checkout_root(request.param)
    if root is None:
        pytest.skip(
            f"no Odoo {request.param}.0 checkout "
            f"(set ODOO{request.param}_SRC or OSM_ODOO_CHECKOUTS)"
        )
    return request.param, root


def test_served_pattern_file_ref_exists(checkout):
    """Every pattern served at N points at a file that exists at N, at a line inside it."""
    major, root = checkout
    broken = []
    for p in _load():
        if not _serves(p, float(major)):
            continue
        target, line = _resolve_file(root, p["file_ref"])
        if target is None:
            broken.append((p["pattern_id"], p["file_ref"], "missing file"))
            continue
        total = len(_file_text(target).splitlines())
        if not 1 <= line <= total:
            broken.append((p["pattern_id"], p["file_ref"], f"file has {total} lines"))
    assert not broken, (
        f"{len(broken)} pattern(s) served at {major}.0 with a file_ref that does not hold at "
        f"{major}.0 (point it at code that exists there or narrow the window): {broken}"
    )


def test_quoted_snippets_are_lines_of_their_file_ref(checkout):
    """A quoted pattern (no illustrative marker): each code line is a line of its file_ref file."""
    major, root = checkout
    problems = []
    for p in _load():
        if p.get("illustrative") or not _serves(p, float(major)):
            continue
        target, _line = _resolve_file(root, p["file_ref"])
        if target is None:
            problems.append((p["pattern_id"], "file_ref file missing"))
            continue
        file_lines = {ln.strip() for ln in _file_text(target).splitlines()}
        missing = [code for code in _code_lines(p["snippet_text"]) if code not in file_lines]
        if missing:
            problems.append((p["pattern_id"], missing[:3]))
    assert not problems, (
        f"quoted snippet lines that are not in the {major}.0 file_ref file "
        f"(mark the pattern illustrative with an anchor, or narrow the window): {problems}"
    )


def test_illustrative_anchor_matches_file_ref(checkout):
    """An illustrative pattern: its anchor matches in the file_ref file at N."""
    major, root = checkout
    problems = []
    for p in _load():
        marker = p.get("illustrative")
        if not marker or not _serves(p, float(major)):
            continue
        target, _line = _resolve_file(root, p["file_ref"])
        if target is None:
            problems.append((p["pattern_id"], "file_ref file missing"))
        elif not re.search(marker["anchor"], _file_text(target), re.M):
            problems.append((p["pattern_id"], marker["anchor"]))
    assert not problems, (
        f"illustrative anchors that do not match in the {major}.0 file_ref file "
        f"(fix the anchor or narrow the window): {problems}"
    )


def test_cited_line_sits_on_the_code(checkout):
    """At the newest version a pattern is served for, file_ref:line points at its code.

    Quoted: the cited line is one of the quoted lines. Illustrative: an anchor match starts within
    ``_ANCHOR_REACH`` lines of it. Checked once per pattern, at its newest served major.
    """
    major, root = checkout
    problems = []
    for p in _load():
        if _newest_served(p) != major:
            continue
        target, line = _resolve_file(root, p["file_ref"])
        if target is None:
            continue  # reported by test_served_pattern_file_ref_exists
        text = _file_text(target)
        lines = text.splitlines()
        if not 1 <= line <= len(lines):
            continue
        marker = p.get("illustrative")
        if marker:
            starts = [
                text.count("\n", 0, m.start()) + 1
                for m in re.finditer(marker["anchor"], text, re.M)
            ]
            if not any(abs(start - line) <= _ANCHOR_REACH for start in starts):
                problems.append(
                    (p["pattern_id"], p["file_ref"], f"anchor matches at lines {starts[:5]}")
                )
        else:
            quoted = set(_code_lines(p["snippet_text"]))
            window = lines[max(0, line - 1 - _CITED_LINE_SLACK): line + _CITED_LINE_SLACK]
            if not any(ln.strip() in quoted for ln in window):
                held = lines[line - 1].strip()[:50]
                problems.append((p["pattern_id"], p["file_ref"], f"line holds {held!r}"))
    assert not problems, (
        f"patterns whose cited line holds other code at {major}.0 "
        f"(their newest served version): {problems}"
    )


def test_core_symbol_names_are_core_symbols(checkout):
    """Each core_symbol_names entry resolves to a symbol the core parser emits at N.

    The writer links a pattern to a CoreSymbol whose qualified name equals the entry or ends with
    '.' + entry, so an addon class (not in the core parser's curated file list) never links.
    """
    from src.indexer.parser_odoo_core import parse_odoo_core

    major, root = checkout
    qualified = {s.qualified_name for s in parse_odoo_core(str(root), f"{major}.0")}
    problems = []
    for p in _load():
        if not _serves(p, float(major)):
            continue
        for name in p.get("core_symbol_names", []):
            if not any(q == name or q.endswith("." + name) for q in qualified):
                problems.append((p["pattern_id"], name))
    assert not problems, f"core_symbol_names that are not core symbols at {major}.0: {problems}"


@cache
def _api_hits(root: Path) -> frozenset[str]:
    """Names of the _API_AVAILABILITY idioms present in a checkout (one pass over its files)."""
    todo = {api.name: (api, re.compile(api.regex, re.M)) for api in _API_AVAILABILITY}
    hits: set[str] = set()
    for sub in ("addons", "odoo", "openerp"):
        base = root / sub
        if not base.is_dir():
            continue
        for dirpath, _dirs, files in os.walk(base):
            in_tests = "/tests" in dirpath + "/"
            for filename in files:
                suffix = os.path.splitext(filename)[1]
                candidates = [
                    (api, rx) for api, rx in todo.values()
                    if suffix in api.suffixes and (api.scan_tests or not in_tests)
                ]
                if not candidates:
                    continue
                text = Path(dirpath, filename).read_text(encoding="utf-8", errors="replace")
                for api, rx in candidates:
                    if api.needle in text and rx.search(text):
                        hits.add(api.name)
                        del todo[api.name]
            if not todo:
                return frozenset(hits)
    return frozenset(hits)


def test_api_availability_table_matches_checkout(checkout):
    """The idiom oracle itself: every listed API is in the checkout exactly at its listed majors."""
    major, root = checkout
    present = _api_hits(root)
    wrong = []
    for api in _API_AVAILABILITY:
        if (api.name in present) != (major in api.present):
            wrong.append((api.name, "present" if api.name in present else "absent", api.evidence))
    assert not wrong, f"_API_AVAILABILITY disagrees with the {major}.0 checkout: {wrong}"
