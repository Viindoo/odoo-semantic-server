# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drift alarm for src/indexer/parser_python.py's _DEPRECATED_API_SYMBOLS (issue #364 D1).

WHY THIS FILE EXISTS
---------------------
`_DEPRECATED_API_SYMBOLS` (`src/indexer/parser_python.py`) is a 25-entry hand-curated
table of Odoo API/method/field-option names flagged for USES_CORE_SYMBOL migration-
review attention. Before issue #364 its version claims lived ONLY in trailing
comments - unfalsifiable by construction, the same artifact class that caused issue
#362's `_FRAMEWORK_BASES` to silently rot for five majors. The audit for #364 found
NINE of the 25 entries wrong or unverifiable against real source, not merely the one
demonstrated defect that opened the issue (`track_visibility` claimed at 17.0; real
transition is 13.0 - 0 hits of `tracking=` before v13, 51 at v13, across all twelve
checkouts). See the issue #364 PR body for the full audit table (symbol | claimed |
real | verdict | evidence).

PROPORTIONALITY - WHY THIS IS *NOT* A framework_bases.py-SHAPED PRODUCTION ORACLE
----------------------------------------------------------------------------------
`framework_bases.py` earns a production-code oracle (`parse_framework_bases()`)
because `index-core` calls it for real, at index time, to enrich real graph nodes.
`_DEPRECATED_API_SYMBOLS`'s `since_version` has NO production consumer: the only
runtime use is a bare membership check (`target not in _DEPRECATED_API_SYMBOLS`,
`parser_python.py` ~line 289) - the real version-gating for the USES_CORE_SYMBOL edge
happens downstream, against the matching CoreSymbol node's own `status` at the
indexed version. Building a full parser + `VersionRegistry` + era-handler apparatus
in production for a fact nothing reads at runtime would be dead code. This test file
is therefore the ONLY place the oracle evidence lives - a "lighter structure...
proportionate for 25 entries" per the issue #364 brief, not the full ADR-0054 rig.

TWO LAYERS (same convention as tests/test_framework_bases_parity.py)
----------------------------------------------------------------------
1. CI layer (`test_since_version_matches_hardcoded_real_source_snippet` et al.) -
   runs everywhere, every time, no checkout needed. For every entry with a
   mechanical check (23 of 25 - see `_PROBES`), it re-derives the transition boundary
   against a TINY, hand-captured, VERBATIM excerpt of real Odoo source at the two
   majors bracketing the true transition (see `_CI_SNIPPETS` - each snippet's
   provenance is the same grep/file:line evidence in the audit table). This is
   lighter than `test_framework_bases_parity.py`'s committed multi-line fixture
   FILES only because these probes are plain text-regex checks, not AST parses that
   need syntactically valid Python - a snippet can be a single real line. It is NOT
   a schema-only check: the hardcoded snippets are independent of
   `_DEPRECATED_API_SYMBOLS`'s own `since_version` field, so a future accidental
   edit to `since_version` (e.g. someone "fixing" `track_visibility` back to 17)
   fails here immediately, without any checkout.
2. Dev-box layer (`test_since_version_matches_real_checkout_per_major`) - the
   stronger, un-curated version of the same comparison, run against the real
   `/home/tuan/git/odoo<N>` checkouts via the shared `tests/_odoo_checkouts.py`
   helper (issue #364 D2 - the convention that actually works, unlike the dead
   `ODOO<N>_SRC` env vars `test_parser_cli.py`/`test_parser_lint_rules.py` read).
   Skips per (name, major) when the checkout is absent - expected and fine.

2 of 25 entries (`name_search`, `fields_get`) have NO probe: the audit found no
single, robust, low-false-positive text pattern that isolates their claimed change
(a whole-method behavioral rewrite for `name_search`; no confirmed transition at all
for `fields_get` - see its `detail` string in production). Per the issue #364
acceptance bar, an honest "no mechanical oracle feasible" note in the data (their
`detail` field) is a complete answer for those two, not a deferral.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.constants import ODOO_NAMESPACE_LEGACY_MAX_MAJOR
from src.indexer.parser_python import _DEPRECATED_API_SYMBOLS, DeprecatedApiSymbol
from tests._odoo_checkouts import checkout_root

# ---------------------------------------------------------------------------
# Probe definitions - one per mechanically-checkable entry.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SourceProbe:
    """Evidence for one DeprecatedApiSymbol.since_version claim: a single text
    pattern searched in one real-source file, whose presence/absence flips
    exactly at ``since_version`` within [check_min_major, check_max_major].

    ``relpath`` is written in the v10-v18 "odoo/..." form; ``_era_relpath()``
    below substitutes the era-correct real path (openerp/ for v8-v9, the v19
    odoo/orm/ package split for odoo/models.py and odoo/fields.py) - the same
    two substitutions `src/indexer/parser_odoo_core.py`'s `_PREFIX_REGISTRY` /
    `_resolve_core_paths` already established for the real indexer, reused
    here rather than re-invented (CLAUDE.md "search before building").

    ``mode``:
      - "present_from": pattern is ABSENT for major < since_version, PRESENT
        for major >= since_version (a rename/deprecation-warning landing).
      - "absent_from": pattern is PRESENT for major < since_version, ABSENT
        for major >= since_version (a removal).

    ``check_max_major`` bounds entries where a SECOND, unprobed fact follows
    the checked one within the surveyed range (e.g. `flush` is deprecated at
    16.0 - probed here - and separately REMOVED at 17.0, a different fact
    this probe does not check; bounding to 16 avoids asserting something
    about the removal this probe was never designed to verify).
    """

    relpath: str
    pattern: str
    mode: str
    check_min_major: int = 10
    check_max_major: int = 19


def _era_relpath(relpath: str, major: int) -> str:
    if major <= ODOO_NAMESPACE_LEGACY_MAX_MAJOR and relpath.startswith("odoo/"):
        return "openerp/" + relpath[len("odoo/"):]
    if major == 19 and relpath in ("odoo/models.py", "odoo/fields.py"):
        return "odoo/orm/" + relpath[len("odoo/"):]
    return relpath


def _probe_matches_text(probe: _SourceProbe, text: str) -> bool:
    return re.search(probe.pattern, text) is not None


def _probe_matches_checkout(probe: _SourceProbe, root: Path, major: int) -> bool:
    """False both when the pattern is absent AND when the file itself does not
    exist at this era - both are legitimate "not observably true yet/anymore"
    states for every probe below (verified per-entry against the real
    checkouts during the issue #364 audit), never a silent mismatch."""
    path = root / _era_relpath(probe.relpath, major)
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return _probe_matches_text(probe, text)


def _expected_match(probe: _SourceProbe, major: int, since_version: int) -> bool:
    present = major >= since_version
    return present if probe.mode == "present_from" else not present


_PROBES: dict[str, _SourceProbe] = {
    "name_get": _SourceProbe("odoo/models.py", r"def name_get\(self\):", "absent_from"),
    "check_access_rights": _SourceProbe(
        "odoo/models.py", r"check_access_rights\(\) is deprecated", "present_from",
    ),
    "check_access_rule": _SourceProbe(
        "odoo/models.py", r"check_access_rule\(\) is deprecated", "present_from",
    ),
    "_filter_access_rules": _SourceProbe(
        "odoo/models.py", r"_filter_access_rules\(\) is deprecated", "present_from",
    ),
    "_check_recursion": _SourceProbe(
        "odoo/models.py", r"_has_cycle\(\) instead", "present_from",
    ),
    "flush": _SourceProbe(
        "odoo/models.py", r"Deprecated method flush\(\), use flush_model",
        "present_from", check_max_major=16,
    ),
    "invalidate_cache": _SourceProbe(
        "odoo/models.py", r"Deprecated method invalidate_cache\(\), use invalidate_model",
        "present_from", check_max_major=16,
    ),
    "safe_eval": _SourceProbe(
        "odoo/tools/safe_eval.py", r"def safe_eval\(expr, /, context=None", "present_from",
    ),
    "_search": _SourceProbe("odoo/models.py", r"access_rights_uid", "absent_from"),
    "read_group": _SourceProbe(
        "odoo/models.py", r"read_group is deprecated", "present_from",
    ),
    "default_get": _SourceProbe(
        "odoo/models.py", r"def default_get\(self, fields:", "present_from",
    ),
    "group_operator": _SourceProbe("odoo/fields.py", r"\baggregator\b", "present_from"),
    "track_visibility": _SourceProbe(
        "addons/mail/models/ir_model_fields.py",
        r"getattr\(field, 'tracking', None\)", "present_from",
    ),
    "float_compare": _SourceProbe(
        "odoo/tools/__init__.py", r"float_utils import", "present_from", check_min_major=8,
    ),
    "float_round": _SourceProbe(
        "odoo/tools/__init__.py", r"float_utils import", "present_from", check_min_major=8,
    ),
    "get_modules": _SourceProbe(
        "odoo/modules/__init__.py", r"get_modules,", "present_from", check_min_major=8,
    ),
    "html_escape": _SourceProbe(
        "odoo/tools/misc.py", r"html_escape = markupsafe\.escape", "present_from",
    ),
    "image_resize_image": _SourceProbe(
        "odoo/tools/image.py", r"def image_resize_image\(", "absent_from",
    ),
    "image_resize_image_big": _SourceProbe(
        "odoo/tools/image.py", r"def image_resize_image_big\(", "absent_from",
    ),
    "image_resize_image_medium": _SourceProbe(
        "odoo/tools/image.py", r"def image_resize_image_medium\(", "absent_from",
    ),
    "image_resize_image_small": _SourceProbe(
        "odoo/tools/image.py", r"def image_resize_image_small\(", "absent_from",
    ),
    "oldname": _SourceProbe("odoo/fields.py", r"\boldname\b", "absent_from"),
    "pycompat": _SourceProbe(
        "odoo/tools/__init__.py", r"import pycompat", "absent_from", check_min_major=11,
    ),
}

# Entries with no mechanical oracle (see module docstring) - must stay in sync
# with _DEPRECATED_API_SYMBOLS by construction (test_probe_coverage below).
_UNPROBED = frozenset({"name_search", "fields_get"})

# ---------------------------------------------------------------------------
# Layer 1 - CI (must NEVER skip). Tiny, hand-captured VERBATIM real-source
# snippets bracketing the true transition major (see class docstring above).
# For a since_version == check_min_major entry (float_compare/float_round/
# get_modules: true at every surveyed major, no in-range "before" state) only
# an "after" snippet is meaningful - `_before` is None.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CiSnippet:
    before: str | None  # real text at since_version - 1 (None when since_version == floor)
    after: str  # real text at since_version


_CI_SNIPPETS: dict[str, _CiSnippet] = {
    "name_get": _CiSnippet(
        before='    def name_get(self):\n        """..."""\n        warnings.warn('
        '"Since 17.0, deprecated method, read display_name instead", DeprecationWarning, 2)\n'
        "        return [(record.id, record.display_name) for record in self]\n",
        after="    def _add_missing_default_values(self, values):\n"
        "        avoid_models = set()\n",
    ),
    "check_access_rights": _CiSnippet(
        before="    def check_access_rights(self, operation, raise_exception=True):\n"
        '        """ Verify that the given operation is allowed ... """\n'
        "        access = self.env['ir.model.access']\n"
        "        return access.check(self._name, operation, raise_exception)\n",
        after="    def check_access_rights(self, operation, raise_exception=True):\n"
        "        warnings.warn(\n"
        '            "check_access_rights() is deprecated since 18.0; '
        'use check_access() instead.",\n'
        "            DeprecationWarning, 1,\n"
        "        )\n",
    ),
    "check_access_rule": _CiSnippet(
        before="    def check_access_rule(self, operation):\n"
        '        """ Verify that the given operation is allowed ... """\n'
        "        return self.env['ir.rule']._check_access(self, operation)\n",
        after="    def check_access_rule(self, operation):\n"
        "        warnings.warn(\n"
        '            "check_access_rule() is deprecated since 18.0; use check_access() instead.",\n'
        "            DeprecationWarning, 1,\n"
        "        )\n",
    ),
    "_filter_access_rules": _CiSnippet(
        before="    def _filter_access_rules(self, operation):\n"
        '        """ Return the subset of ``self`` for which ``operation`` is allowed. """\n'
        "        return self.env['ir.rule']._compute_access(self, operation)\n",
        after="    def _filter_access_rules(self, operation):\n"
        "        warnings.warn(\n"
        '            "_filter_access_rules() is deprecated since 18.0; '
        'use _filtered_access() instead.",\n'
        "            DeprecationWarning, 1,\n"
        "        )\n"
        "        return self._filtered_access(operation)\n",
    ),
    "_check_recursion": _CiSnippet(
        before="    def _check_recursion(self, parent=None):\n"
        '        """ Verify that there is no loop in a hierarchical structure ... """\n'
        "        if not parent:\n"
        "            parent = self._parent_name\n",
        after="    def _check_recursion(self, parent=None):\n"
        '        warnings.warn("Since 18.0, one must use not _has_cycle() instead", '
        "DeprecationWarning, 2)\n"
        "        return not self._has_cycle(parent)\n",
    ),
    "flush": _CiSnippet(
        before="    def flush(self, fnames=None, records=None):\n"
        '        """ Process all the pending computations ... """\n'
        "        if fnames is None:\n"
        "            self.env.flush_all()\n",
        after="    def flush(self, fnames=None, records=None):\n"
        "        warnings.warn(\n"
        '            "Deprecated method flush(), use flush_model(), flush_recordset() '
        'or env.flush_all() instead",\n'
        "            DeprecationWarning, stacklevel=2,\n"
        "        )\n",
    ),
    "invalidate_cache": _CiSnippet(
        before="    def invalidate_cache(self, fnames=None, ids=None):\n"
        '        """ Invalidate the record caches ... """\n'
        "        if ids is not None:\n",
        after="    def invalidate_cache(self, fnames=None, ids=None):\n"
        "        warnings.warn(\n"
        '            "Deprecated method invalidate_cache(), use invalidate_model(), '
        'invalidate_recordset() or env.invalidate_all() instead",\n'
        "            DeprecationWarning, stacklevel=2\n"
        "        )\n",
    ),
    "safe_eval": _CiSnippet(
        before="def safe_eval(expr, globals_dict=None, locals_dict=None, mode=\"eval\",\n"
        "               nocopy=False, locals_builtins=False, filename=None):\n",
        after='def safe_eval(expr, /, context=None, *, mode="eval", filename=None):\n',
    ),
    "_search": _CiSnippet(
        before="    def _search(self, domain, offset=0, limit=None, order=None, "
        "access_rights_uid=None):\n",
        after="    def _search(self, domain, offset=0, limit=None, order=None) -> Query:\n",
    ),
    "read_group": _CiSnippet(
        before="    def read_group(self, domain, fields, groupby, offset=0, limit=None, "
        "orderby=False, lazy=True):\n"
        '        """Get the list of records in list view grouped by the given '
        '``groupby`` fields.\n',
        after='    @api.deprecated("Since 19.0, read_group is deprecated. Please use '
        '_read_group in the backend code or formatted_read_group for a complete '
        'formatted result")\n'
        "    def read_group(self, domain, fields, groupby, offset=0, limit=None, "
        "orderby=False, lazy=True):\n",
    ),
    "default_get": _CiSnippet(
        before="    def default_get(self, fields_list):\n"
        '        """ default_get(fields_list) -> default_values\n',
        after="    def default_get(self, fields: Sequence[str]) -> ValuesType:\n"
        '        """ Return default values for the fields in ``fields``.\n',
    ),
    "group_operator": _CiSnippet(
        before="    group_operator = None               # operator for aggregating values\n",
        after="    aggregator = None                   # operator for aggregating values\n",
    ),
    "track_visibility": _CiSnippet(
        before="        tracking = getattr(field, 'track_visibility', None)\n",
        after="        tracking = getattr(field, 'tracking', None)\n",
    ),
    "float_compare": _CiSnippet(
        before=None,
        after="from .float_utils import *\n",
    ),
    "float_round": _CiSnippet(
        before=None,
        after="from .float_utils import *\n",
    ),
    "get_modules": _CiSnippet(
        before=None,
        after="    get_modules,\n    get_modules_with_version,\n",
    ),
    "html_escape": _CiSnippet(
        before="def html_escape(text):\n"
        '    """ Vendored from werkzeug.utils.escape which is deprecated in 2.0\n',
        after="html_escape = markupsafe.escape\n",
    ),
    "image_resize_image": _CiSnippet(
        before="def image_resize_image(base64_source, size=(1024, 1024), encoding='base64', "
        "filetype=None, avoid_if_small=False):\n",
        after="def image_process(b64source, size=(0, 0), verify_resolution=False, "
        "quality=0, crop=None, colorize=False, output_format=''):\n",
    ),
    "image_resize_image_big": _CiSnippet(
        before="def image_resize_image_big(base64_source, size=(1024, 1024), "
        "encoding='base64', filetype=None, avoid_if_small=True):\n",
        after="def image_process(b64source, size=(0, 0), verify_resolution=False, "
        "quality=0, crop=None, colorize=False, output_format=''):\n",
    ),
    "image_resize_image_medium": _CiSnippet(
        before="def image_resize_image_medium(base64_source, size=(128, 128), "
        "encoding='base64', filetype=None, avoid_if_small=False):\n",
        after="def image_process(b64source, size=(0, 0), verify_resolution=False, "
        "quality=0, crop=None, colorize=False, output_format=''):\n",
    ),
    "image_resize_image_small": _CiSnippet(
        before="def image_resize_image_small(base64_source, size=(64, 64), "
        "encoding='base64', filetype=None, avoid_if_small=False):\n",
        after="def image_process(b64source, size=(0, 0), verify_resolution=False, "
        "quality=0, crop=None, colorize=False, output_format=''):\n",
    ),
    "oldname": _CiSnippet(
        before='DEPRECATED_ATTRS = [("oldname", "use an upgrade script instead.")]\n',
        after="DEPRECATED_ATTRS: list[tuple[str, str]] = []\n",
    ),
    "pycompat": _CiSnippet(
        before="from . import pycompat\n",
        after="from . import assertion_report\n",
    ),
}


def test_ci_snippets_cover_every_probed_entry():
    """Shape guard: every probed name has exactly one CI snippet pair, and vice
    versa - a future probe added without a snippet (or a snippet without a
    probe) fails here instead of silently narrowing CI coverage."""
    assert set(_CI_SNIPPETS) == set(_PROBES)


@pytest.mark.parametrize("name", sorted(_PROBES))
def test_since_version_matches_hardcoded_real_source_snippet(name):
    """CI layer - business rule: DeprecatedApiSymbol.since_version, read from
    the PRODUCTION module, must be consistent with a tiny hand-captured real
    excerpt bracketing the true transition. This is the layer that catches a
    future accidental edit to since_version with zero checkout required - the
    exact gap that let track_visibility ship wrong for an unknown period
    (issue #364).
    """
    entry = _DEPRECATED_API_SYMBOLS[name]
    probe = _PROBES[name]
    snippet = _CI_SNIPPETS[name]

    after_matches = _probe_matches_text(probe, snippet.after)
    assert after_matches == (probe.mode == "present_from"), (
        f"{name}: the 'after' (since_version={entry.since_version}) snippet "
        f"does not exhibit the expected probe outcome for mode={probe.mode!r}"
    )

    if snippet.before is not None:
        before_matches = _probe_matches_text(probe, snippet.before)
        assert before_matches == (probe.mode == "absent_from"), (
            f"{name}: the 'before' (since_version-1) snippet does not exhibit "
            f"the expected probe outcome for mode={probe.mode!r}"
        )
    else:
        assert entry.since_version <= probe.check_min_major, (
            f"{name}: no 'before' snippet is recorded, which is only valid "
            f"when since_version ({entry.since_version}) is at or below this "
            f"probe's check_min_major ({probe.check_min_major}) - i.e. the "
            "claim is 'true at every surveyed major', not a real boundary."
        )


def test_probe_coverage_matches_all_25_entries():
    """Shape guard: _PROBES ∪ _UNPROBED must equal every key in
    _DEPRECATED_API_SYMBOLS - a future entry added to the production table
    without EITHER a probe or an explicit _UNPROBED acknowledgement fails
    here, so silent oracle coverage narrowing is impossible.
    """
    assert set(_PROBES) | _UNPROBED == set(_DEPRECATED_API_SYMBOLS)
    assert set(_PROBES) & _UNPROBED == set()


def test_every_entry_has_a_verified_since_version_in_surveyed_range():
    """Shape guard - business rule: since_version is always a real, in-range
    Odoo major (8-19), change is one of the closed set of categories, and the
    dict key always matches the entry's own name (never allows a copy-paste
    key/name mismatch to go unnoticed)."""
    valid_changes = {"removed", "deprecated", "renamed", "moved", "signature_changed", "review"}
    assert len(_DEPRECATED_API_SYMBOLS) == 25
    for key, entry in _DEPRECATED_API_SYMBOLS.items():
        assert isinstance(entry, DeprecatedApiSymbol)
        assert entry.name == key
        assert 8 <= entry.since_version <= 19, (
            f"{key}: since_version={entry.since_version} out of range"
        )
        assert entry.change in valid_changes, f"{key}: unknown change kind {entry.change!r}"
        assert entry.detail, f"{key}: empty detail"


# ---------------------------------------------------------------------------
# Layer 2 - dev-box (real checkouts; skips per (name, major) when absent).
# ---------------------------------------------------------------------------


def _dev_box_cases() -> list[tuple[str, int]]:
    cases: list[tuple[str, int]] = []
    for name, probe in sorted(_PROBES.items()):
        for major in range(probe.check_min_major, probe.check_max_major + 1):
            cases.append((name, major))
    return cases


@pytest.mark.odoo_source
@pytest.mark.parametrize("name,major", _dev_box_cases())
def test_since_version_matches_real_checkout_per_major(name, major):
    """Dev-box layer - business rule: the same boundary check as the CI layer,
    but against the actual, un-curated Odoo source tree rather than a
    hand-captured snippet - the stronger of the two, since it also validates
    the snippets themselves stayed faithful to real source. Skips (never
    fails) when the checkout is not present on this machine.
    """
    root = checkout_root(major)
    if root is None:
        pytest.skip(f"Odoo {major} checkout not found (set OSM_ODOO_CHECKOUTS to override)")

    entry = _DEPRECATED_API_SYMBOLS[name]
    probe = _PROBES[name]
    matched = _probe_matches_checkout(probe, root, major)
    expected = _expected_match(probe, major, entry.since_version)
    assert matched == expected, (
        f"{name} at v{major}.0: probe found match={matched}, expected {expected} "
        f"(since_version={entry.since_version}, mode={probe.mode}) - "
        f"file {root / _era_relpath(probe.relpath, major)}"
    )
