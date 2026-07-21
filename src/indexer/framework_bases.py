# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/framework_bases.py — Odoo test-framework base-class menu (issue #362).
"""Version-aware menu of Odoo's own test-framework base classes.

WHY THIS FILE EXISTS
---------------------
Before issue #362, "which test base classes exist at Odoo version N" was answered
by TWO independent, drift-prone copies:

* ``_FRAMEWORK_BASES`` (formerly ``src/indexer/parser_test.py``, the graph-seed
  path) — a flat, version-blind dict that was correct the day it was written and
  then silently stopped matching source as new Odoo majors shipped.
  ``SavepointCase`` — merged into ``TransactionCase`` at v15/v16 and removed
  outright at v17 — kept being served as "available" at every version, including
  v19, because nothing in the repo could notice the drift.
* ``_static_framework_bases`` / ``_static_framework_bases_str`` (formerly
  ``src/mcp/tools/test_tools.py``, the empty-graph fallback path) — a SEPARATE,
  smaller, independently hand-written table gated only by a single inline
  ``major <= 15`` check, disagreeing with the first copy at nearly every version.

This module REPLACES BOTH — it is the single SSOT: a curated, version-dispatched
table (ADR-0052 shape: one feature-owned ``VersionRegistry``, per-era handlers,
one aggregate dispatcher — see ``_FRAMEWORK_BASE_REGISTRY`` / ``framework_bases()``
below) plus an independent AST/text-regex oracle (``parse_framework_bases()``)
that a companion test (``tests/test_framework_bases_parity.py``) uses as a drift
alarm: it diffs the curated table against a real parse of Odoo source, on every
CI run (committed fixture excerpts) and, when available, on a real checkout
(dev-box layer).

BRANCH-HEAD SEMANTICS (read before editing the era tables)
------------------------------------------------------------
Every version window declared in this module describes what a
``git clone -b X.0 <odoo-repo>`` tree contains **today** at that branch's HEAD —
never a GA-tag snapshot frozen at release time. Odoo backports fixes and even new
test-framework helpers onto already-released stable branches, so "available at
8.0" means "present in the 8.0 branch as it stands now", not "present in the
8.0.0 release tarball". Concrete evidence: ``SavepointCase`` was introduced by
commit ``11ba4689b1d9`` ("[IMP] running speed of some tests & new testcase type",
2015-06-23) and *backported onto the already-released 8.0 branch* — it did not
exist when Odoo 8.0 first shipped (2014). It is nonetheless a real, load-bearing
base class on that branch today: ``odoo8/addons/mail/tests/common.py:25`` defines
``class TestMail(common.SavepointCase):``. OSM therefore asserts ``SavepointCase``
is available at 8.0 (see ``_era_v8_v9`` below) — matching the branch a real 8.0
checkout gives you, not a hypothetical frozen-at-GA menu that would wrongly
declare it absent.

THE v8 __pycache__ TRAP (D3)
-------------------------------
``parse_framework_bases()`` must test for a READABLE ``common.py`` FILE at the
era-correct prefix, never merely directory presence. Verified on this machine:
``/home/tuan/git/odoo8/odoo/tests/`` EXISTS — but holds only a stray
``__pycache__`` subdirectory (a leftover of the openerp -> odoo runtime alias),
zero readable ``.py`` source. A resolver that only checks ``is_dir()`` on that
path would silently "succeed" by parsing zero classes; ``is_file()`` on the exact
``common.py`` path returns the correct ``False`` instead, so this module returns
``None`` and the caller falls back to the curated table, exactly as the caller
expects for "no source root available".

THE v8/v9 PYTHON-2 SYNTAX HAZARD — AND WHY THE FALLBACK IS TEXT-REGEX
--------------------------------------------------------------------------
The real (un-curated) v8 and v9 ``openerp/tests/common.py`` files on this machine
do not parse under Python 3's ``ast`` module at all: both contain a genuine
Python-2-only ``except select.error, e:`` clause (v8 line 297, v9 line 324) inside
``HttpCase.phantom_poll`` — a PhantomJS output-polling helper with no bearing on
any framework base class's own shape, but fatal to a whole-file ``ast.parse``
anyway because the file must parse as one unit. This is the SAME, already-
established hazard ``parser_python.parse_file`` documents and solves for the
model parser (``parser_python.py:978-1013``, #285, ADR-0032 graceful
degradation), and that ``parser_python_era1.py`` itself names ``except E, e:`` as
its own motivating example — not a novel trap this module discovered.

Following that established convention means the *fallback*, not the AST-first
*primary path*, must be text-regex: ``ast.parse`` is tried first for every era
(matching ``parser_python.parse_file``'s real dispatch shape, not the looser
CLAUDE.md summary of it), and only on ``SyntaxError`` does this module fall back
to a text-regex class-header scan (issue #362 WI-1 adversarial review, section
1.6). The one reusable primitive from ``parser_python_era1.py`` is its
``_RE_CLASS_HEAD`` pattern — a ``class NAME(BASES):`` header matcher anchored at
column 0 (MULTILINE, so a nested class is never mistaken for a top-level one) —
the rest of that module is shaped for ORM model/field extraction (``_name``/
``_inherit``/``_columns``, a method regex that requires ``self`` so a
``@classmethod`` like ``setUpClass`` is invisible, and it drops any class without
``_name``/``_inherit`` — every framework base would be silently discarded), so it
is not reusable as a whole; only the class-header primitive is reused here
(``_RE_CLASS_HEAD`` below, same pattern).

The text-regex scanner (``_scan_classes_text``) NEVER mutates the source text —
unlike an earlier line-rewrite recovery this module used before the WI-1 review
(deleted; see git history for issue #362 if the prior approach is of interest),
which handled exactly one Python-2 shape and gave up completely, silently, on
every other: verified empirically, 9 of 10 injected Python-2 shapes (a ``print``
statement, ``raise E, msg``, backtick repr, ``<>``, ``exec``, an octal literal,
a ``ur''`` string, tuple-unpacking parameters, a multi-line variant of the one
handled shape) collapsed to a silent ``None``, and the rewrite predicate could in
principle touch an unrelated, already-VALID line it merely happened to be pointed
at. A pure text scan has neither failure mode: it reads facts (class name, base
list, ``setUpClass`` presence, the deprecation marker) directly out of the raw
text via regex, so it is robust to *every* Python-2 construct — a regex never
cares whether the surrounding file would compile — and it degrades per-class,
never all-or-nothing: a genuinely unparseable class header simply does not
appear in the result, the same graceful behaviour the curated-table fallback
already relies on when ``parse_framework_bases`` returns ``None`` entirely (no
readable ``common.py`` at all).

PYTHON-VERSION DEPENDENCE
----------------------------
On CPython >= 3.14, PEP 758 made the unparenthesized ``except A, B:`` shape (a
2-tuple of exception TYPES, not the Python-2 ``except <expr>, <name>:`` BINDING
form) legal to parse — ``ast.parse`` no longer raises ``SyntaxError`` on the real
v8/v9 files on that interpreter, so this module's text-regex fallback simply
never triggers there (the AST path already succeeds) and silently reads
``except select.error, e:`` as a 2-type exception tuple instead — a semantic
misreading with no bearing on this module's OWN facts (the misparsed line is
deep inside ``HttpCase.phantom_poll``'s body, not a class header), but worth
knowing if this file is ever debugged on a >=3.14 interpreter and the fallback
appears "unreachable": it decays gracefully as CPython's own tolerance widens,
it never breaks.

ADR-0052 SHAPE
----------------
This feature owns ONE ``VersionRegistry`` (``_FRAMEWORK_BASE_REGISTRY``), fans out
to seven per-era handlers (``_era_v8_v9`` .. ``_era_v17_plus``), and exposes ONE
aggregate dispatcher (``framework_bases()``). Callers never see the registry or
the era handlers directly. Adding v20 is a one-line append to the registry plus
one new ``_era_v20`` handler for the NAME/status/file_path/has_setUpClass menu —
see "V20 AUTO-EXTEND — DELIBERATELY ABSENT" below for the one thing that is NOT a
one-line append (``removed_at()`` owns its own tiny registry, independent of
``_FRAMEWORK_BASE_REGISTRY``, and needs its own entry too — see that function's
docstring for why).

V20 AUTO-EXTEND — DELIBERATELY ABSENT (issue #362 WI-1 review, HIGH-2)
----------------------------------------------------------------------------
An earlier version of this module, when given ``odoo_source_root``, APPENDED any
parsed class name with no curated match to the returned menu ("v20 auto-extend" —
letting a real checkout's new class show up before the curated table was updated
for it). That directly contradicted this module's own prune-universe invariant
(api-contract.md "Writer contract": ``live_names`` — the name set
``framework_bases()`` returns — must NEVER become a function of
``odoo_source_root``, or one profile's prune could delete a class node another
profile, running without a source root, still needs). The WI-1 review proved the
contradiction is live, not just theoretical: a synthetic v19 checkout containing
an extra real class made ``{f.name for f in framework_bases(v, source_root=X)}``
diverge from ``{f.name for f in framework_bases(v)}``, and the extra class would
have been written to the graph as a node the WI-4 prune Cypher
(``WHERE NOT th.name IN $live_names AND th.name IN $known_universe``) can NEVER
delete, because it is not a member of ``KNOWN_FRAMEWORK_BASE_NAMES`` either — a
permanent graph-hygiene hole.

Auto-extend is therefore GONE. ``framework_bases(v, source_root)`` only ever
ENRICHES (file_path/line/has_setUpClass, and promotes status to 'deprecated')
entries the curated table for ``v`` ALREADY has; a parsed class with no curated
match is silently ignored by the enrichment step (never written, never
returned). THE DESIGNED CONSEQUENCE: when a future Odoo v20 ships a class this
table does not know about yet, ``framework_bases('20.0')`` keeps returning the
v17+ era menu, and ``tests/test_framework_bases_parity.py``'s dev-box layer goes
RED against a real v20 checkout the moment one exists on this machine — that red
IS the alarm doing its job, the signal for a human to append one era line (see
"ADR-0052 SHAPE" above). A silent auto-extension would have suppressed exactly
that alarm — the same "curated table quietly stops matching source" failure mode
issue #362 exists to kill, just relocated one level up the stack.

EXPLICIT PER-CLASS test_type (not name-substring inference)
------------------------------------------------------------------
``FrameworkBaseFacts.test_type`` is one of the six literal values
api-contract.md's Dataclass section fixes (``'transaction'|'savepoint'|
'single_transaction'|'http'|'form'|'unittest'`` — this closed set is also
hard-coded by ``tests/test_mcp_test_tools.py``'s ``_CLASS_ROW_RE``, a read-side
contract this module does not own and must not silently outgrow). Every
``_curated_fact()`` call below states its class's ``test_type`` EXPLICITLY,
rather than deriving it by matching a substring of the class NAME (an earlier
version of this module did exactly that via a now-deleted
``_test_type_for_name`` helper, and the fallthrough silently mis-filed both
``BaseCase`` and ``TreeCase`` under ``'unittest'`` simply because neither name
contains "Transaction"/"Http"/"Form"/"Savepoint" — a false-by-omission
classification for ``BaseCase`` specifically: real ``openerp/tests/common.py`` /
``odoo/tests/common.py`` ``BaseCase`` is abstract (expects
``self.registry``/``self.cr``/``self.uid`` to already be set by a subclass) but
is genuinely Odoo-ORM-aware — it defines ``ref()``/``browse_ref()``/
``cursor()``, unlike bare stdlib ``unittest.TestCase``).

``BaseCase`` keeps ``test_type='unittest'`` (none of the six values is a perfect
fit for a class that is abstract by design — its own transaction/http/form claim
would be equally false on a bare, subclass-less instance — and changing the
VALUE would require growing the closed six-value set the read-side test file
already pins), but ``_render_setup_summary()`` below gives ``BaseCase`` its OWN,
name-specific, ORM-aware description instead of reusing the generic "carries no
transaction semantics" line verbatim, so the connotation "unittest == stdlib, no
ORM" a reader might reasonably draw from ``TestCase`` sharing the same bucket is
never applied to ``BaseCase``. ``TreeCase`` keeps the generic description: real
``odoo/tests/common.py`` (v11-v14) shows it derives directly from
``unittest.TestCase`` and adds only an lxml ``assertTreesEqual`` comparison
helper — no ``cursor``/``ref``/``browse_ref``, no ORM awareness at all (those are
added by ``BaseCase``, which itself subclasses ``TreeCase``) — so "no Odoo ORM"
is factually true for ``TreeCase``, unlike for ``BaseCase``. Verified directly
against ``/home/tuan/git/odoo8`` (``BaseCase``) and ``/home/tuan/git/odoo11`` /
``/home/tuan/git/odoo14`` (``TreeCase``) in this session.

COMPOSITION RULES (``framework_bases()`` internals — see api-contract.md)
-----------------------------------------------------------------------------
1. Resolve the era handler for ``odoo_version`` (default: the newest known era,
   so an out-of-range or unparseable version fails open to the modern menu).
2. Call it to get the curated entries for that version.
3. If ``odoo_source_root`` is given and ``parse_framework_bases`` returns
   non-``None``: for every parsed name that matches a curated entry, overwrite
   ``file_path``/``line``/``has_setUpClass`` from the parse, and PROMOTE
   ``status`` to ``'deprecated'`` when the parse says so — a curated
   ``'deprecated'`` is NEVER downgraded back to ``'available'`` by a silent parse
   miss. A parsed name with NO curated match is ignored (see "V20 AUTO-EXTEND —
   DELIBERATELY ABSENT" above).
4. ``setup_summary`` is composed by ONE renderer (``_render_setup_summary``) for
   EVERY entry, on EVERY path (curated-only or parse-enriched), deriving the
   deprecation clause from the final ``status`` — never baked into a per-era
   handler.
5. Sort by ``name`` ascending (matches the eventual Cypher ``ORDER BY``).

Imports are intentionally minimal (``ast``, ``logging``, ``re``,
``collections.abc.Callable`` — ruff's UP035-mandated modern spelling of the
contract's "typing" allowance — ``dataclasses``, ``pathlib``,
``.version_registry``, ``.parser_util``, ``..constants``) — this module sits in
the parser layer of the one-way pipeline (``scanner -> registry -> resolver ->
parser -> ...``) and must never import the writer, ``src.mcp``, or a Neo4j
driver (enforced by ``tests/test_pipeline_import_discipline.py``).
"""
from __future__ import annotations

import ast
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from ..constants import ODOO_NAMESPACE_LEGACY_MAX_MAJOR
from .parser_util import parse_external_source
from .version_registry import VersionRegistry

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dataclasses (api-contract.md "Dataclass" / "Public functions")
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameworkBaseFacts:
    """One framework test-base class, as known at one Odoo version.

    ``line`` is parse-derived ONLY (never set by the curated era tables).
    ``replacement`` is reserved for a REMOVED-at-this-era class named in the
    removal line (see ``removed_at()``) — every entry ``framework_bases()``
    actually returns describes a class that EXISTS at that version, so
    ``replacement`` is always ``None`` on values this module emits; the
    replacement mapping itself lives in ``removed_at()``.
    """

    name: str
    test_type: str  # 'transaction'|'savepoint'|'single_transaction'|'http'|'form'|'unittest'
    commit_allowed: bool  # ALWAYS False for every framework base (PP3 contract)
    status: str  # 'available' | 'deprecated'
    setup_summary: list[str]
    file_path: str | None
    line: int | None
    has_setUpClass: bool
    replacement: str | None


@dataclass(frozen=True)
class ParsedFacts:
    """One top-level test-base ClassDef, as found by the AST oracle."""

    name: str
    file_path: str
    line: int
    bases: list[str]
    has_setUpClass: bool
    is_deprecated: bool  # body has __init_subclass__ calling warnings.warn(..., DeprecationWarning)


# The prune universe: every name this feature has EVER emitted at any era.
# Also the "known name" half of parse_framework_bases's inclusion rule (it is
# what admits Form/O2MForm, which derive from `object` and would otherwise
# never chain-reach TestCase).
KNOWN_FRAMEWORK_BASE_NAMES: frozenset[str] = frozenset({
    "BaseCase",
    "TransactionCase",
    "SingleTransactionCase",
    "SavepointCase",
    "HttpCase",
    "HttpCaseCommon",
    "HttpSavepointCase",
    "TreeCase",
    "Form",
    "O2MForm",
    "TestCase",
})

# SavepointCase / HttpSavepointCase's replacement, at the era each is removed
# (v17+). Single SSOT reused by both removed_at() and the setup_summary
# renderer's deprecation clause, so the two never drift apart.
_DEPRECATION_REPLACEMENTS: dict[str, str] = {
    "SavepointCase": "TransactionCase",
    "HttpSavepointCase": "HttpCase",
}


def _curated_fact(
    name: str, test_type: str, status: str, file_path: str | None, has_setUpClass: bool,
) -> FrameworkBaseFacts:
    """Build one curated (pre-parse-enrichment, pre-setup_summary) entry.

    ``test_type`` is ALWAYS given explicitly by the caller (see module docstring
    "EXPLICIT PER-CLASS test_type") — this function performs no name-substring
    inference of its own, so a future era edit cannot silently mis-file a class
    the way the deleted ``_test_type_for_name`` fallthrough once did.
    """
    return FrameworkBaseFacts(
        name=name,
        test_type=test_type,
        commit_allowed=False,
        status=status,
        setup_summary=[],
        file_path=file_path,
        line=None,
        has_setUpClass=has_setUpClass,
        replacement=None,
    )


# ---------------------------------------------------------------------------
# Per-era curated handlers (ADR-0052 shape). Each is fully self-contained
# (no inter-era delegation) so an edit to one era can never silently perturb
# another. Facts transcribed verbatim from api-contract.md's "Authoritative
# menus per era" / "file_path per era" / "has_setUpClass" tables, independently
# re-verified against both the committed CI fixtures and the real checkouts on
# this machine (see module docstring). Every ``_curated_fact`` call states its
# ``test_type`` explicitly (module docstring "EXPLICIT PER-CLASS test_type").
# ---------------------------------------------------------------------------


def _era_v8_v9() -> list[FrameworkBaseFacts]:
    fp = "openerp/tests/common.py"
    return [
        _curated_fact("BaseCase", "unittest", "available", fp, False),
        _curated_fact("HttpCase", "http", "available", fp, False),
        _curated_fact("SavepointCase", "savepoint", "available", fp, False),
        _curated_fact("SingleTransactionCase", "single_transaction", "available", fp, True),
        _curated_fact("TestCase", "unittest", "available", None, False),
        _curated_fact("TransactionCase", "transaction", "available", fp, False),
    ]


def _era_v10() -> list[FrameworkBaseFacts]:
    fp = "odoo/tests/common.py"
    return [
        _curated_fact("BaseCase", "unittest", "available", fp, False),
        _curated_fact("HttpCase", "http", "available", fp, False),
        _curated_fact("SavepointCase", "savepoint", "available", fp, False),
        _curated_fact("SingleTransactionCase", "single_transaction", "available", fp, True),
        _curated_fact("TestCase", "unittest", "available", None, False),
        _curated_fact("TransactionCase", "transaction", "available", fp, False),
    ]


def _era_v11() -> list[FrameworkBaseFacts]:
    fp = "odoo/tests/common.py"
    return [
        _curated_fact("BaseCase", "unittest", "available", fp, False),
        _curated_fact("HttpCase", "http", "available", fp, False),
        _curated_fact("SavepointCase", "savepoint", "available", fp, False),
        _curated_fact("SingleTransactionCase", "single_transaction", "available", fp, True),
        _curated_fact("TestCase", "unittest", "available", None, False),
        _curated_fact("TransactionCase", "transaction", "available", fp, False),
        _curated_fact("TreeCase", "unittest", "available", fp, False),
    ]


def _era_v12_v13() -> list[FrameworkBaseFacts]:
    fp = "odoo/tests/common.py"
    return [
        _curated_fact("BaseCase", "unittest", "available", fp, False),
        _curated_fact("Form", "form", "available", fp, False),
        _curated_fact("HttpCase", "http", "available", fp, False),
        _curated_fact("O2MForm", "form", "available", fp, False),
        _curated_fact("SavepointCase", "savepoint", "available", fp, False),
        _curated_fact("SingleTransactionCase", "single_transaction", "available", fp, True),
        _curated_fact("TestCase", "unittest", "available", None, False),
        _curated_fact("TransactionCase", "transaction", "available", fp, False),
        _curated_fact("TreeCase", "unittest", "available", fp, False),
    ]


def _era_v14() -> list[FrameworkBaseFacts]:
    fp = "odoo/tests/common.py"
    return [
        _curated_fact("BaseCase", "unittest", "available", fp, False),
        _curated_fact("Form", "form", "available", fp, False),
        _curated_fact("HttpCase", "http", "available", fp, False),
        _curated_fact("HttpCaseCommon", "http", "available", fp, False),
        _curated_fact("HttpSavepointCase", "http", "available", fp, False),
        _curated_fact("O2MForm", "form", "available", fp, False),
        _curated_fact("SavepointCase", "savepoint", "available", fp, False),
        _curated_fact("SingleTransactionCase", "single_transaction", "available", fp, True),
        _curated_fact("TestCase", "unittest", "available", None, False),
        _curated_fact("TransactionCase", "transaction", "available", fp, False),
        _curated_fact("TreeCase", "unittest", "available", fp, False),
    ]


def _era_v15_v16() -> list[FrameworkBaseFacts]:
    fp = "odoo/tests/common.py"
    return [
        _curated_fact("BaseCase", "unittest", "available", fp, False),
        _curated_fact("Form", "form", "available", fp, False),
        _curated_fact("HttpCase", "http", "available", fp, True),
        _curated_fact("HttpSavepointCase", "http", "deprecated", fp, False),
        _curated_fact("O2MForm", "form", "available", fp, False),
        _curated_fact("SavepointCase", "savepoint", "deprecated", fp, False),
        _curated_fact("SingleTransactionCase", "single_transaction", "available", fp, True),
        _curated_fact("TestCase", "unittest", "available", None, False),
        _curated_fact("TransactionCase", "transaction", "available", fp, True),
    ]


def _era_v17_plus() -> list[FrameworkBaseFacts]:
    common_fp = "odoo/tests/common.py"
    form_fp = "odoo/tests/form.py"
    return [
        _curated_fact("BaseCase", "unittest", "available", common_fp, True),
        _curated_fact("Form", "form", "available", form_fp, False),
        _curated_fact("HttpCase", "http", "available", common_fp, True),
        _curated_fact("O2MForm", "form", "available", form_fp, False),
        _curated_fact("SingleTransactionCase", "single_transaction", "available", common_fp, True),
        _curated_fact("TestCase", "unittest", "available", None, False),
        _curated_fact("TransactionCase", "transaction", "available", common_fp, True),
    ]


# Feature-owned registry (ADR-0052): one boundary set, per-era handlers, first
# match wins. Adding v20: append one entry + write _era_v20.
_EraHandler = Callable[[], list[FrameworkBaseFacts]]
_FRAMEWORK_BASE_REGISTRY: VersionRegistry[_EraHandler] = VersionRegistry([
    (8, 9, _era_v8_v9),
    (10, 10, _era_v10),
    (11, 11, _era_v11),
    (12, 13, _era_v12_v13),
    (14, 14, _era_v14),
    (15, 16, _era_v15_v16),
    (17, None, _era_v17_plus),
])


def _resolve_major(odoo_version: str) -> int | None:
    """Parse the leading major from *odoo_version* (e.g. "17.0" -> 17).

    Mirrors VersionRegistry.resolve_version's own parsing exactly so
    is_out_of_catalogue() and the dispatch table can never disagree on what
    counts as "unparseable". Returns None (never raises) on failure.
    """
    try:
        return int(str(odoo_version).split(".")[0])
    except (ValueError, IndexError, AttributeError):
        return None


def is_out_of_catalogue(odoo_version: str) -> bool:
    """True when the resolved major is outside the surveyed [8, 19] range.

    Also true for an unparseable/empty version — there is no real major to be
    "in catalogue" with. Drives the provenance line on the menu output (WI-3).
    """
    major = _resolve_major(odoo_version)
    return major is None or major < 8 or major > 19


# removed_at()'s OWN tiny registry (ADR-0052: every feature-specific boundary
# gets its own registry, never a scattered `if major >= N`, and never piggy-
# backed on ANOTHER registry's object identity). issue #362 WI-1 review HIGH-1:
# the previous implementation tested `era is _era_v17_plus` - the object
# identity of _FRAMEWORK_BASE_REGISTRY's era-menu handler - so appending a
# hypothetical (20, None, _era_v20) entry to THAT registry silently made
# removed_at("20.0") and removed_at("99.0") both wrongly start returning []
# (the identity check stopped matching), telling a v20/sentinel caller nothing
# was ever removed - the exact "SavepointCase still looks available" defect
# issue #362 exists to kill, just relocated to a new era boundary. This
# registry is independent: appending a v20 era to _FRAMEWORK_BASE_REGISTRY
# cannot change removed_at()'s answer at all, by construction.
_REMOVED_REGISTRY: VersionRegistry[tuple[tuple[str, str], ...]] = VersionRegistry([
    (17, None, tuple(_DEPRECATION_REPLACEMENTS.items())),
])


def removed_at(odoo_version: str) -> list[tuple[str, str]]:
    """[(removed_class, replacement)] a user of an OLDER series would look for.

    v17+ (including the out-of-catalogue sentinel 99.0) -> SavepointCase and
    HttpSavepointCase, both merged into their non-savepoint counterpart.
    v8-v16 -> [] (both classes still exist there, merely deprecated at 15/16).
    An unparseable/empty version fails open to the newest known era EXPLICITLY
    here (not via VersionRegistry's own default, which would answer [] for an
    unparseable major just as it would for a genuine v8-v16 major - the two
    cases must not collapse: "genuinely v8-v16" means nothing was removed yet,
    "cannot be resolved at all" means fail open to the modern, most-cautious
    answer, matching every other function in this module).
    """
    major = _resolve_major(odoo_version)
    if major is None:
        return list(_DEPRECATION_REPLACEMENTS.items())
    return list(_REMOVED_REGISTRY.resolve(major, default=()))


def _render_setup_summary(
    name: str, test_type: str, status: str, has_setUpClass: bool,
) -> list[str]:
    """The ONE setup_summary renderer (composition rule 4). Deliberately the
    single place that turns {name, test_type, status, has_setUpClass} into
    human-facing guidance — every FrameworkBaseFacts, curated or
    parse-enriched, goes through this exact function.

    Returns a list of length 1 (a single, fully-composed sentence-string),
    never several short fragments. Read-side rendering (test_tools.py, a
    different work item's owned file) joins ``setup_summary`` fragments with
    ``", ".join(setup_summary[:3])`` — that join was written when every
    fragment was a short, comma-joinable phrase ("savepoint-per-method"); once
    fragments became full sentences ending in a period, the same join produced
    a malformed "....own add., Defines its own setUpClass" (period directly
    followed by a comma). Composing exactly ONE sentence here — instead of
    fixing the join on the other side of the module boundary this file must
    not cross — means ``", ".join([one_string])`` is just ``one_string``: no
    comma is ever introduced, and the join's silent ``[:3]`` truncation (a
    fourth fragment would vanish with no trace) can no longer bite, because
    there is never a fourth fragment to begin with.
    """
    parts: list[str] = [_test_type_clause(name, test_type)]
    if has_setUpClass:
        parts.append(
            f"Defines its own setUpClass - always call super().setUpClass() "
            f"first when overriding it on a {name} subclass."
        )
    if status == "deprecated":
        replacement = _DEPRECATION_REPLACEMENTS.get(name)
        if replacement:
            parts.append(
                f"DEPRECATED: {name} has been merged into {replacement} - use "
                f"{replacement} directly in new code."
            )
        else:
            parts.append(f"DEPRECATED: {name} should not be used in new code.")
    return [" ".join(parts)]


def _test_type_clause(name: str, test_type: str) -> str:
    """The test_type-driven semantic clause, name-specialized where the shared
    per-test_type text would otherwise conflate two behaviourally different
    classes (module docstring "EXPLICIT PER-CLASS test_type" / issue #362
    WI-1 follow-up).
    """
    if test_type == "transaction":
        return (
            "Each test method runs inside its own savepoint; changes "
            "auto-rollback after the method (no manual cleanup needed)."
        )
    if test_type == "savepoint":
        return (
            "All test methods in the class share one setUpClass-built "
            "savepoint; per-method changes still auto-rollback after each test."
        )
    if test_type == "single_transaction":
        return (
            "Every test method in the class runs inside ONE shared "
            "transaction with no per-method rollback - methods can see each "
            "other's writes."
        )
    if test_type == "http":
        if name == "HttpCaseCommon":
            return (
                "HTTP test-infrastructure mixin (Chrome headless browser "
                "control, xmlrpc endpoints, request session) - not itself a "
                "concrete transactional test case; Odoo combines it with "
                "TransactionCase (see HttpCase) or SavepointCase (see "
                "HttpSavepointCase) rather than subclassing it directly."
            )
        if name == "HttpSavepointCase":
            return (
                "HTTP-enabled savepoint variant: all test methods in the "
                "class share one setUpClass-built savepoint (like "
                "SavepointCase), plus HTTP test support (Chrome headless "
                "browser, url_open) layered on top."
            )
        return (
            "Runs a real HTTP server for the test and wraps each test method "
            "in its own savepoint (auto-rollback), like TransactionCase."
        )
    if test_type == "form":
        return (
            "Server-side Form helper - simulates onchange/view logic without "
            "a browser; not itself a test case base class."
        )
    # 'unittest' - BaseCase is a deliberate exception within this bucket: it
    # is genuinely Odoo-ORM-aware (defines ref()/browse_ref()/cursor()), unlike
    # TestCase/TreeCase, which carry none of that. Reusing the generic
    # "carries no transaction semantics" line for BaseCase would be true about
    # transactions but silent about ORM-awareness, inviting exactly the
    # confident-but-wrong "no Odoo ORM" reading TestCase's own bucket implies.
    if name == "BaseCase":
        return (
            "Abstract Odoo test base, NOT stdlib-only - defines real "
            "Odoo-ORM-aware helpers (ref(), browse_ref(), cursor()) but "
            "expects self.registry/self.cr/self.uid to already be set by a "
            "concrete subclass (TransactionCase, SingleTransactionCase, or "
            "SavepointCase); carries no transaction semantics of its own."
        )
    return (
        "Framework base class; carries no transaction semantics of its "
        "own beyond what its subclasses add."
    )


def framework_bases(
    odoo_version: str,
    odoo_source_root: str | Path | None = None,
) -> list[FrameworkBaseFacts]:
    """Menu for one version, sorted by name ASC (matches the Cypher ORDER BY).

    ``framework_bases(v)`` (no source root) is the curated, AUTHORITATIVE menu.
    Passing ``odoo_source_root`` additionally enriches file_path/line/
    has_setUpClass from a real AST parse and may promote status to
    'deprecated' - see the composition rules in the module docstring.
    """
    era = _FRAMEWORK_BASE_REGISTRY.resolve_version(odoo_version, default=_era_v17_plus)
    by_name: dict[str, FrameworkBaseFacts] = {fact.name: fact for fact in era()}

    if odoo_source_root is not None:
        parsed = parse_framework_bases(odoo_source_root, odoo_version)
        if parsed is None:
            # Not silent (issue #362 WI-1 review HIGH-3): the caller asked for
            # source enrichment and got the curated-only menu instead. DEBUG,
            # not WARNING - parse_framework_bases already logged the specific
            # reason (missing file at DEBUG, unreadable file at WARNING) when
            # it decided to return None; this is just the visible trail from
            # the enrichment call site so a reader tracing "why does this menu
            # have no file_path" does not have to guess which layer to blame.
            _logger.debug(
                "framework_bases(%r, %r): parse_framework_bases returned None - "
                "menu stays curated-only (no file_path/line/has_setUpClass "
                "enrichment, no status promotion).",
                odoo_version, str(odoo_source_root),
            )
        else:
            for pname, pf in parsed.items():
                cur = by_name.get(pname)
                if cur is None:
                    # issue #362 WI-1 review HIGH-2: v20 auto-extend was
                    # DELIBERATELY REMOVED (module docstring "V20 AUTO-EXTEND -
                    # DELIBERATELY ABSENT") - live_names must stay a pure
                    # function of odoo_version, never of odoo_source_root, or
                    # the WI-4 prune Cypher could leave a permanently
                    # un-prunable node. A real class the curated table does
                    # not know about yet is intentionally ignored here; the
                    # dev-box parity layer is the alarm for it, not this path.
                    _logger.debug(
                        "framework_bases(%r): parsed class %r has no curated "
                        "entry for this era - skipping enrichment (v20 "
                        "auto-extend was intentionally dropped, issue #362 "
                        "WI-1 review HIGH-2; see module docstring).",
                        odoo_version, pname,
                    )
                    continue
                # Promote-only: a curated 'deprecated' never reverts to
                # 'available' just because this particular parse missed
                # the __init_subclass__ marker.
                promote = cur.status == "deprecated" or pf.is_deprecated
                new_status = "deprecated" if promote else "available"
                by_name[pname] = replace(
                    cur,
                    file_path=pf.file_path,
                    line=pf.line,
                    has_setUpClass=pf.has_setUpClass,
                    status=new_status,
                )

    final = [
        replace(
            fact,
            setup_summary=_render_setup_summary(
                fact.name, fact.test_type, fact.status, fact.has_setUpClass,
            ),
        )
        for fact in by_name.values()
    ]
    final.sort(key=lambda fact: fact.name)
    return final


def framework_base(
    odoo_version: str,
    name: str,
    odoo_source_root: str | Path | None = None,
) -> FrameworkBaseFacts | None:
    """One class, or None when it does not exist at that version."""
    for fact in framework_bases(odoo_version, odoo_source_root):
        if fact.name == name:
            return fact
    return None


# ---------------------------------------------------------------------------
# AST/text-regex oracle (parse_framework_bases). Independent of the curated
# tables above - this is what tests/test_framework_bases_parity.py diffs the
# curated menu against.
# ---------------------------------------------------------------------------

# Era-prefix resolution is its own tiny one-boundary registry (ADR-0052: every
# feature-specific boundary gets its own registry, never a scattered
# `if major <= N`), reusing ODOO_NAMESPACE_LEGACY_MAX_MAJOR rather than a
# second hardcoded "9". Mirrors parser_odoo_core._PREFIX_REGISTRY's shape.
_ERA_PREFIX_REGISTRY: VersionRegistry[str] = VersionRegistry([
    (8, ODOO_NAMESPACE_LEGACY_MAX_MAJOR, "openerp"),
    (ODOO_NAMESPACE_LEGACY_MAX_MAJOR + 1, None, "odoo"),
])


def _era_prefix(odoo_version: str) -> str:
    return _ERA_PREFIX_REGISTRY.resolve_version(odoo_version, default="odoo")  # type: ignore[return-value]


@dataclass(frozen=True)
class _ScannedClass:
    """One top-level class definition, as found by EITHER scan path (AST or
    the text-regex SyntaxError fallback), normalized to the same shape so
    every downstream step (``_is_included`` / ``ParsedFacts`` construction) is
    origin-agnostic — it never needs to know whether the source that produced
    it was AST-parseable or required the text-regex fallback (module
    docstring "THE v8/v9 PYTHON-2 SYNTAX HAZARD").
    """

    name: str
    file_path: str
    line: int
    bases: list[str]  # resolved terminal base names; an unresolvable base (a
    # Call expression, a keyword arg like metaclass=...) is already dropped -
    # the same rule for both scan paths, see _terminal_base_name/_text_base_names.
    has_setUpClass: bool
    is_deprecated: bool


def _terminal_base_name(base: ast.expr) -> str | None:
    """Resolve a base-class expression to its rightmost dotted component.

    ``unittest.TestCase`` / ``unittest2.TestCase`` / ``case.TestCase`` (the
    three forms real Odoo source actually uses across v8-v19) all resolve to
    "TestCase" - the inclusion rule only cares about the terminal name, never
    the module prefix, so a future fourth spelling needs no code change.
    Returns None for an unresolvable base shape (e.g. v12/v13's
    ``MetaCase('DummyCase', (object,), {})`` mixin-trick Call expression) -
    such bases are simply skipped, never a crash. ``_text_base_names`` below
    is the text-mode equivalent of this same rule, for the fallback path.
    """
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    return None


def _text_base_names(bases_text: str) -> list[str]:
    """Text-mode equivalent of resolving ``ast.ClassDef.bases`` via
    ``_terminal_base_name``, for the text-regex fallback path (module
    docstring "THE v8/v9 PYTHON-2 SYNTAX HAZARD" / issue #362 WI-1 review
    section 1.6).

    Splits the raw base-list text (the ``(...)`` capture of a class header)
    on top-level commas only — a comma nested inside parens belongs to a Call
    expression (e.g. the v12-v14 ``MetaCase('DummyCase', (object,), {})``
    mixin trick) and must never be split on. A segment resolves to a base
    name only when it is a bare dotted name with no call and no keyword
    argument — exactly what ``_terminal_base_name`` resolves for
    ``ast.Name``/``ast.Attribute`` and skips (``None``) for everything else
    (``ast.Call``, a keyword arg) — then the segment's rightmost dotted
    component is taken, matching ``ast.Attribute``'s ``.attr`` resolution
    (e.g. ``unittest2.TestCase`` -> ``TestCase``).
    """
    segments: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in bases_text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            segments.append("".join(current))
            current = []
        else:
            current.append(ch)
    segments.append("".join(current))

    names: list[str] = []
    for segment in segments:
        seg = segment.strip()
        if not seg or "=" in seg or "(" in seg:
            continue  # keyword arg (e.g. metaclass=...) or Call expression
        if re.fullmatch(r"[\w.]+", seg):
            names.append(seg.rsplit(".", 1)[-1])
    return names


# Reused verbatim from parser_python_era1.py's `_RE_CLASS_HEAD` (module
# docstring "THE v8/v9 PYTHON-2 SYNTAX HAZARD" / issue #362 WI-1 review
# section 1.6) - column-0-anchored via `^` + MULTILINE, so an indented
# (nested) class is never mistaken for a top-level one, matching the AST
# path's `tree.body`-only discipline (CLAUDE.md).
_RE_CLASS_HEAD = re.compile(r"^class\s+(\w+)\s*\(([^)]*)\)\s*:", re.MULTILINE)
_RE_SETUPCLASS_DEF_TEXT = re.compile(r"^[ \t]+def\s+setUpClass\b", re.MULTILINE)


def _reaches_test_case(
    name: str, class_index: dict[str, _ScannedClass], seen: set[str] | None = None,
) -> bool:
    """True when *name*'s base chain, resolved only within the walked files,
    reaches TestCase (directly or transitively through a locally-defined
    base). Cycle-guarded via *seen* (a pathological/hand-crafted fixture could
    otherwise self-reference). Origin-agnostic: *class_index* values are
    ``_ScannedClass`` regardless of whether they came from the AST path or
    the text-regex fallback."""
    if seen is None:
        seen = set()
    if name in seen:
        return False
    seen.add(name)
    scanned = class_index.get(name)
    if scanned is None:
        return False
    for terminal in scanned.bases:
        if terminal == "TestCase":
            return True
        if terminal in class_index and _reaches_test_case(terminal, class_index, seen):
            return True
    return False


def _is_included(name: str, class_index: dict[str, _ScannedClass]) -> bool:
    """Inclusion rule (api-contract.md): a public top-level class is emitted
    when either its base chain reaches TestCase, or its name is already known
    (the clause that admits Form/O2MForm, which derive from `object`).
    Excludes utility classes like MetaCase(type), OdooSuite, _ErrorCatcher,
    TagsSelector, RecordCapturer, Like, Approx, RegistryRLock, Screencaster,
    WhitespaceInsensitive - none of those chain-reach TestCase and none are in
    KNOWN_FRAMEWORK_BASE_NAMES (verified against the real common.py/case.py
    source for v8-v19 on this machine).
    """
    if name in KNOWN_FRAMEWORK_BASE_NAMES:
        return True
    return _reaches_test_case(name, class_index)


def _has_setupclass(node: ast.ClassDef) -> bool:
    return any(
        isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "setUpClass"
        for item in node.body
    )


def _calls_deprecation_warning(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    # ast.walk is safe here: the scope is a single, already-identified
    # function body, not top-level class discovery - CLAUDE.md's "iterate
    # tree.body, never ast.walk" rule is about not mistaking a nested class
    # for a module-level one, which is not a risk when walking inside one
    # known function looking for a Call node.
    for sub in ast.walk(func_node):
        if isinstance(sub, ast.Call):
            callee = sub.func
            is_warn_call = (
                (isinstance(callee, ast.Attribute) and callee.attr == "warn")
                or (isinstance(callee, ast.Name) and callee.id == "warn")
            )
            if not is_warn_call:
                continue
            for arg in list(sub.args) + [kw.value for kw in sub.keywords]:
                if isinstance(arg, ast.Name) and arg.id == "DeprecationWarning":
                    return True
    return False


def _is_deprecated(node: ast.ClassDef) -> bool:
    """The mechanical deprecation predicate: a class body defining
    __init_subclass__ whose body calls warnings.warn(..., DeprecationWarning).
    This is exactly what distinguishes the real v15/v16 SavepointCase
    (deprecated) from the v12-v14 SavepointCase (not deprecated - same name,
    different era, different body).
    """
    for item in node.body:
        is_init_subclass = (
            isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name == "__init_subclass__"
        )
        if is_init_subclass and _calls_deprecation_warning(item):
            return True
    return False


def _scan_classes_ast(tree: ast.Module, relpath: str) -> dict[str, _ScannedClass]:
    """AST class scan - the primary path, tried first for every era (module
    docstring "THE v8/v9 PYTHON-2 SYNTAX HAZARD"). Discovers only
    ``tree.body`` top-level ClassDefs, never via ``ast.walk`` (CLAUDE.md AST
    discipline: a nested class must never be mistaken for a top-level one).
    """
    found: dict[str, _ScannedClass] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name not in found:
            bases = [
                terminal
                for base in node.bases
                if (terminal := _terminal_base_name(base)) is not None
            ]
            found[node.name] = _ScannedClass(
                name=node.name,
                file_path=relpath,
                line=node.lineno,
                bases=bases,
                has_setUpClass=_has_setupclass(node),
                is_deprecated=_is_deprecated(node),
            )
    return found


def _scan_classes_text(source: str, relpath: str) -> dict[str, _ScannedClass]:
    """Text-regex class-header scan — the ``SyntaxError`` fallback (module
    docstring "THE v8/v9 PYTHON-2 SYNTAX HAZARD" / issue #362 WI-1 review
    section 1.6). NEVER mutates *source*; reads facts directly out of the raw
    text via regex. Always succeeds (returns a dict, possibly empty) — unlike
    ``ast.parse``, a regex scan has no all-or-nothing failure mode, so this
    degrades per-class: a genuinely unparseable class header simply does not
    appear in the result.
    """
    found: dict[str, _ScannedClass] = {}
    headers = list(_RE_CLASS_HEAD.finditer(source))
    for i, m in enumerate(headers):
        name = m.group(1)
        if name in found:
            continue
        body_start = m.end()
        body_end = headers[i + 1].start() if i + 1 < len(headers) else len(source)
        body = source[body_start:body_end]
        lineno = source.count("\n", 0, m.start()) + 1
        found[name] = _ScannedClass(
            name=name,
            file_path=relpath,
            line=lineno,
            bases=_text_base_names(m.group(2)),
            has_setUpClass=bool(_RE_SETUPCLASS_DEF_TEXT.search(body)),
            is_deprecated="__init_subclass__" in body and "DeprecationWarning" in body,
        )
    return found


def _scan_source(source: str, filename: str, relpath: str) -> dict[str, _ScannedClass]:
    """One file's class scan: AST first, text-regex fallback ONLY on
    ``SyntaxError`` (module docstring "THE v8/v9 PYTHON-2 SYNTAX HAZARD" /
    "PYTHON-VERSION DEPENDENCE") — matches ``parser_python.parse_file``'s real
    dispatch shape (``parser_python.py:993-1013``), not a version-keyed
    choice, so a syntactically-valid file at ANY era is never routed through
    the fallback.
    """
    try:
        tree = parse_external_source(source, filename=filename)
    except SyntaxError as exc:
        # By-design fallback for real v8/v9 source on this project's runtime
        # Python (3.12) - DEBUG, not WARNING, mirroring parser_python.py's own
        # "recovery-used" log level (issue #362 WI-1 review HIGH-3/C3a).
        _logger.debug(
            "parse_framework_bases: ast.parse failed for %s (%s) - falling "
            "back to text-regex class-header scan.",
            filename, exc,
        )
        classes = _scan_classes_text(source, relpath)
        if not classes:
            # NOT the expected "file recovered fine" outcome - a visible
            # signal that this file is more broken than a class-header regex
            # can recover from, never silent (issue #362 WI-1 review
            # HIGH-3/C3a: "an oracle stopped being an oracle" must be loud).
            _logger.warning(
                "parse_framework_bases: text-regex fallback found ZERO "
                "top-level classes in %s after a SyntaxError (%s).",
                filename, exc,
            )
        return classes
    return _scan_classes_ast(tree, relpath)


def parse_framework_bases(
    odoo_source_root: str | Path,
    odoo_version: str,
) -> dict[str, ParsedFacts] | None:
    """AST/text-regex oracle. ``None`` only when no readable ``common.py``
    FILE exists under the era prefix (the v8 __pycache__ trap - see module
    docstring) or when it exists but cannot be read/decoded. Once a readable
    ``common.py`` is found, this ALWAYS returns a dict (possibly with fewer
    entries than a fully AST-parseable file would yield) - the text-regex
    fallback degrades per-class, never all-or-nothing (module docstring "THE
    v8/v9 PYTHON-2 SYNTAX HAZARD").

    Walks ``<root>/<prefix>/tests/common.py`` always, plus
    ``<root>/<prefix>/tests/form.py`` whenever that file exists (this is a
    pure existence check, not a version-major comparison - form.py simply
    does not exist before v17 in real Odoo source, so this data-driven check
    reproduces the "v17+ also walks form.py" rule without a second
    ``if major >= 17`` anywhere in this module).
    """
    root = Path(odoo_source_root)
    prefix = _era_prefix(odoo_version)

    common_path = root / prefix / "tests" / "common.py"
    if not common_path.is_file():
        # Expected/designed "no source root available" case (the v8
        # __pycache__ trap is the canonical example) - DEBUG, not WARNING.
        _logger.debug(
            "parse_framework_bases: no readable common.py at %s - caller "
            "falls back to the curated table.", common_path,
        )
        return None
    try:
        common_source = common_path.read_text(encoding="utf-8", errors="ignore")
    except (OSError, UnicodeDecodeError) as exc:
        # A real failure, not the designed "no source" case: the file EXISTS
        # but could not be read (issue #362 WI-1 review MEDIUM-1/C3c - real
        # v12-v16 common.py contain non-ASCII bytes, and UnicodeDecodeError
        # is a ValueError, not an OSError, so both must be caught explicitly).
        _logger.warning(
            "parse_framework_bases: could not read %s (%s) - falling back "
            "to the curated table.", common_path, exc,
        )
        return None

    class_index: dict[str, _ScannedClass] = _scan_source(
        common_source, str(common_path), f"{prefix}/tests/common.py",
    )
    class_order: list[str] = list(class_index.keys())

    form_path = root / prefix / "tests" / "form.py"
    if form_path.is_file():
        try:
            form_source = form_path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeDecodeError) as exc:
            _logger.warning(
                "parse_framework_bases: found %s but could not read it (%s) "
                "- continuing with common.py only.", form_path, exc,
            )
            form_source = None
        if form_source is not None:
            for name, scanned in _scan_source(
                form_source, str(form_path), f"{prefix}/tests/form.py",
            ).items():
                if name not in class_index:
                    class_index[name] = scanned
                    class_order.append(name)

    result: dict[str, ParsedFacts] = {}
    for name in class_order:
        if not _is_included(name, class_index):
            continue
        scanned = class_index[name]
        result[name] = ParsedFacts(
            name=scanned.name,
            file_path=scanned.file_path,
            line=scanned.line,
            bases=scanned.bases,
            has_setUpClass=scanned.has_setUpClass,
            is_deprecated=scanned.is_deprecated,
        )
    return result
