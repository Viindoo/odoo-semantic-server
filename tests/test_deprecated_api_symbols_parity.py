# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drift alarm for src/indexer/parser_python.py's _DEPRECATED_API_SYMBOLS (issue #364 D1).

WHY THIS FILE EXISTS
---------------------
`_DEPRECATED_API_SYMBOLS` (`src/indexer/parser_python.py`) is a hand-curated
table of Odoo API/method/field-option names flagged for USES_CORE_SYMBOL migration-
review attention. Before issue #364 its version claims lived ONLY in trailing
comments - unfalsifiable by construction, the same artifact class that caused issue
#362's `_FRAMEWORK_BASES` to silently rot for five majors. The audit for #364 found
NINE of the then 25 entries wrong or unverifiable against real source, not merely the
one demonstrated defect that opened the issue (`track_visibility` claimed at 17.0;
real transition is 13.0 - 0 hits of `tracking=` before v13, 51 at v13, across all
twelve checkouts then surveyed). See the issue #364 PR body for the full audit table
(symbol | claimed | real | verdict | evidence).

A name keeps ONE entry. Its first transition is `since_version`/`change`; a later
transition of the same name (removed at 20.0 after a deprecation at 18.0, a
deprecated API reborn with a new signature at 20.0, ...) is `later`, probed by
`_LATER_PROBES` with the same two layers, so a fact about 20.0 never overwrites the
fact about 19.0 and neither lives only in prose.

PROPORTIONALITY - WHY THIS IS *NOT* A framework_bases.py-SHAPED PRODUCTION ORACLE
----------------------------------------------------------------------------------
`framework_bases.py` earns a production-code oracle (`parse_framework_bases()`)
because `index-core` calls it for real, at index time, to enrich real graph nodes.
`_DEPRECATED_API_SYMBOLS`'s `since_version` and `later` have NO production consumer:
the only runtime use is a bare membership check (`target not in
_DEPRECATED_API_SYMBOLS`, in `_extract_core_symbol_refs`) - the real version-gating
for the USES_CORE_SYMBOL edge happens downstream, against the matching CoreSymbol
node's own `status` at the indexed version. Building a full parser +
`VersionRegistry` + era-handler apparatus in production for a fact nothing reads at
runtime would be dead code. This test file is therefore the ONLY place the oracle
evidence lives - a "lighter structure... proportionate for a table this size" per the
issue #364 brief, not the full ADR-0054 rig.

TWO LAYERS (same convention as tests/test_framework_bases_parity.py)
----------------------------------------------------------------------
1. CI layer (`test_since_version_matches_hardcoded_real_source_snippet` et al.) -
   runs everywhere, every time, no checkout needed. For every entry with a
   mechanical check (all but `_UNPROBED`), it re-derives the transition boundary
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
   `odoo<N>` checkouts via the shared `tests/_odoo_checkouts.py` helper (issue #364
   D2), over every major up to `src.constants.SURVEYED_MAX_MAJOR`. Skips per
   (name, major) when the checkout is absent - expected and fine.

2 entries (`name_search`, `fields_get`) have NO probe: the audit found no
single, robust, low-false-positive text pattern that isolates their claimed change
(a whole-method behavioral rewrite for `name_search`; no confirmed transition at all
for `fields_get` - see its `detail` string in production). Per the issue #364
acceptance bar, an honest "no mechanical oracle feasible" note in the data (their
`detail` field) is a complete answer for those two, not a deferral.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.constants import (
    ODOO_NAMESPACE_LEGACY_MAX_MAJOR,
    SURVEYED_MAX_MAJOR,
    SURVEYED_MIN_MAJOR,
)
from src.indexer.parser_python import (
    _DEPRECATED_API_SYMBOLS,
    DeprecatedApiSymbol,
    _extract_core_symbol_refs,
)
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
    substitutions `src/indexer/parser_odoo_core.py`'s `_PREFIX_REGISTRY` /
    `_resolve_core_paths` already established for the real indexer, reused
    here rather than re-invented (CLAUDE.md "search before building").

    ``mode``:
      - "present_from": pattern is ABSENT for major < since_version, PRESENT
        for major >= since_version (a rename/deprecation-warning landing).
      - "absent_from": pattern is PRESENT for major < since_version, ABSENT
        for major >= since_version (a removal).

    ``check_max_major`` bounds entries where a SECOND fact follows the
    checked one within the surveyed range (e.g. `flush` is deprecated at
    16.0 - probed here - and separately REMOVED at 17.0, a ``later`` fact
    `_LATER_PROBES` checks); bounding to 16 keeps this probe from asserting
    something about the removal it was never designed to verify.
    ``test_first_probe_stops_before_the_later_transition`` enforces the
    bound for every entry that has a ``later``.
    """

    relpath: str
    pattern: str
    mode: str
    check_min_major: int = 10
    check_max_major: int = SURVEYED_MAX_MAJOR


# Files that moved: (first major at the new path, old path) -> new path.
_MOVED_AT = (
    # v19 split of odoo/models.py and odoo/fields.py into the odoo/orm/ package.
    (19, "odoo/models.py", "odoo/orm/models.py"),
    (19, "odoo/fields.py", "odoo/orm/fields.py"),
    # v20: query.py joins the odoo/orm/ package; safe_eval.py becomes a package
    # whose evaluation module holds safe_eval().
    (20, "odoo/tools/query.py", "odoo/orm/query.py"),
    (20, "odoo/tools/safe_eval.py", "odoo/tools/safe_eval/evaluation.py"),
)


def _era_relpath(relpath: str, major: int) -> str:
    if major <= ODOO_NAMESPACE_LEGACY_MAX_MAJOR and relpath.startswith("odoo/"):
        return "openerp/" + relpath[len("odoo/"):]
    for first_major, old, new in _MOVED_AT:
        if major >= first_major and relpath == old:
            return new
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
        check_max_major=19,
    ),
    "check_access_rule": _SourceProbe(
        "odoo/models.py", r"check_access_rule\(\) is deprecated", "present_from",
        check_max_major=19,
    ),
    "check_field_access_rights": _SourceProbe(
        "odoo/models.py", r"Deprecated since 19\.0, use `_check_field_access` on models",
        "present_from", check_max_major=19,
    ),
    "_filter_access_rules": _SourceProbe(
        "odoo/models.py", r"_filter_access_rules\(\) is deprecated", "present_from",
        check_max_major=19,
    ),
    "_filter_access_rules_python": _SourceProbe(
        "odoo/models.py", r"_filter_access_rules_python\(\) is deprecated", "present_from",
        check_max_major=19,
    ),
    "_check_recursion": _SourceProbe(
        "odoo/models.py", r"_has_cycle\(\) instead", "present_from", check_max_major=19,
    ),
    "_check_m2m_recursion": _SourceProbe(
        "odoo/models.py",
        r"(?:_has_cycle\(\) instead\"\)\s+def _check_m2m_recursion"
        r"|def _check_m2m_recursion\(self, field_name\):\s+warnings\.warn)",
        "present_from", check_max_major=19,
    ),
    "toggle_active": _SourceProbe(
        "odoo/models.py", r"Deprecated since 19\.0, use action_archive or action_unarchive",
        "present_from", check_max_major=19,
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
        "odoo/models.py", r"read_group is deprecated", "present_from", check_max_major=19,
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
    # --- first deprecated at 20.0; check_min_major=19 checks the prior major too ---
    "_check_access": _SourceProbe(
        "odoo/models.py",
        r"@deprecated\(\"Since 20\.0, use Model\._access_domain instead\"\)\s+def _check_access\(",
        "present_from", check_min_major=19,
    ),
    "_check_field_access": _SourceProbe(
        "odoo/models.py",
        r"@deprecated\(\"Since 20\.0, use check_field_access\(\)\"\)\s+def _check_field_access\(",
        "present_from", check_min_major=19,
    ),
    "_init_column": _SourceProbe(
        "odoo/models.py",
        r"@deprecated\(\"Since 20\.0, field initialization is defined in Field\.init_storage\"\)"
        r"\s+def _init_column\(",
        "present_from", check_min_major=19,
    ),
    "_table_has_rows": _SourceProbe(
        "odoo/models.py",
        r"@deprecated\(\"Since 20\.0, _table_has_rows is removed\"\)\s+def _table_has_rows\(",
        "present_from", check_min_major=19,
    ),
    "left_join": _SourceProbe(
        "odoo/tools/query.py",
        r"def left_join\([^\n]*\n(?:[^\n]*\n){0,10}?"
        r"[^\n]*Since 20\.0, use TableSQL\.join or add_join",
        "present_from", check_min_major=19,
    ),
    "fix_foreign_key": _SourceProbe(
        "odoo/tools/sql.py",
        r"@deprecated\(\"Removed after 20\.0\"\)\ndef fix_foreign_key\(",
        "present_from", check_min_major=19,
    ),
    "check_index_exist": _SourceProbe(
        "odoo/tools/sql.py",
        r"@deprecated\(\"Removed after 20\.0\"\)\ndef check_index_exist\(",
        "present_from", check_min_major=19,
    ),
    "reverse_order": _SourceProbe(
        "odoo/tools/sql.py",
        r"@deprecated\(\"Removed after 20\.0\"\)\ndef reverse_order\(",
        "present_from", check_min_major=19,
    ),
    "escape_psql": _SourceProbe(
        "odoo/tools/sql.py",
        r"@deprecated\(\"Since 20\.0, use escape_like_value\"\)\ndef escape_psql\(",
        "present_from", check_min_major=19,
    ),
}

# Second transition of a name, keyed like _PROBES. The boundary is the entry's
# own ``later[0][0]``; a probe is checked from ``check_min_major`` (the major
# before the transition) through SURVEYED_MAX_MAJOR.
_LATER_PROBES: dict[str, _SourceProbe] = {
    "check_access_rights": _SourceProbe(
        "odoo/models.py", r"def check_access_rights\(", "absent_from", check_min_major=19,
    ),
    "check_access_rule": _SourceProbe(
        "odoo/models.py", r"def check_access_rule\(", "absent_from", check_min_major=19,
    ),
    "check_field_access_rights": _SourceProbe(
        "odoo/models.py", r"def check_field_access_rights\(", "absent_from", check_min_major=19,
    ),
    "_filter_access_rules": _SourceProbe(
        "odoo/models.py", r"def _filter_access_rules\(", "absent_from", check_min_major=19,
    ),
    "_filter_access_rules_python": _SourceProbe(
        "odoo/models.py", r"def _filter_access_rules_python\(", "absent_from",
        check_min_major=19,
    ),
    "_check_recursion": _SourceProbe(
        "odoo/models.py", r"def _check_recursion\(", "absent_from", check_min_major=19,
    ),
    "_check_m2m_recursion": _SourceProbe(
        "odoo/models.py", r"def _check_m2m_recursion\(", "absent_from", check_min_major=19,
    ),
    "toggle_active": _SourceProbe(
        "odoo/models.py", r"def toggle_active\(", "absent_from", check_min_major=19,
    ),
    # 20.0 read_group: the live signature (groupby/aggregates/having), not the
    # @api.deprecated (domain, fields, groupby, ...) one.
    "read_group": _SourceProbe(
        "odoo/models.py",
        r"@api\.model\s+def read_group\(\s*self,\s*domain: DomainType,\s*"
        r"groupby: Sequence\[str\] = \(\),\s*aggregates",
        "present_from", check_min_major=19,
    ),
    # The file itself is gone at 20.0 (a missing file reads as "no match").
    "pycompat": _SourceProbe(
        "odoo/tools/pycompat.py", r"(?m)^import codecs$", "absent_from", check_min_major=19,
    ),
    "flush": _SourceProbe(
        "odoo/models.py", r"def flush\(self, fnames=None, records=None\)", "absent_from",
        check_min_major=16,
    ),
    "invalidate_cache": _SourceProbe(
        "odoo/models.py", r"def invalidate_cache\(self, fnames=None, ids=None\)",
        "absent_from", check_min_major=16,
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
    before: str | None  # real text at at - 1 (None when the claim is true at every major)
    after: str  # real text at ``at``
    at: int  # the major the ``after`` text was captured at: the boundary under test


_CI_SNIPPETS: dict[str, _CiSnippet] = {
    "name_get": _CiSnippet(
        before='    def name_get(self):\n        """..."""\n        warnings.warn('
        '"Since 17.0, deprecated method, read display_name instead", DeprecationWarning, 2)\n'
        "        return [(record.id, record.display_name) for record in self]\n",
        after="    def _add_missing_default_values(self, values):\n"
        "        avoid_models = set()\n",
        at=18,
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
        at=18,
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
        at=18,
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
        at=18,
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
        at=18,
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
        at=16,
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
        at=16,
    ),
    "safe_eval": _CiSnippet(
        before="def safe_eval(expr, globals_dict=None, locals_dict=None, mode=\"eval\",\n"
        "               nocopy=False, locals_builtins=False, filename=None):\n",
        after='def safe_eval(expr, /, context=None, *, mode="eval", filename=None):\n',
        at=19,
    ),
    "_search": _CiSnippet(
        before="    def _search(self, domain, offset=0, limit=None, order=None, "
        "access_rights_uid=None):\n",
        after="    def _search(self, domain, offset=0, limit=None, order=None) -> Query:\n",
        at=18,
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
        at=19,
    ),
    "default_get": _CiSnippet(
        before="    def default_get(self, fields_list):\n"
        '        """ default_get(fields_list) -> default_values\n',
        after="    def default_get(self, fields: Sequence[str]) -> ValuesType:\n"
        '        """ Return default values for the fields in ``fields``.\n',
        at=19,
    ),
    "group_operator": _CiSnippet(
        before="    group_operator = None               # operator for aggregating values\n",
        after="    aggregator = None                   # operator for aggregating values\n",
        at=18,
    ),
    "track_visibility": _CiSnippet(
        before="        tracking = getattr(field, 'track_visibility', None)\n",
        after="        tracking = getattr(field, 'tracking', None)\n",
        at=13,
    ),
    "float_compare": _CiSnippet(
        before=None,
        after="from .float_utils import *\n",
        at=8,
    ),
    "float_round": _CiSnippet(
        before=None,
        after="from .float_utils import *\n",
        at=8,
    ),
    "get_modules": _CiSnippet(
        before=None,
        after="    get_modules,\n    get_modules_with_version,\n",
        at=8,
    ),
    "html_escape": _CiSnippet(
        before="def html_escape(text):\n"
        '    """ Vendored from werkzeug.utils.escape which is deprecated in 2.0\n',
        after="html_escape = markupsafe.escape\n",
        at=15,
    ),
    "image_resize_image": _CiSnippet(
        before="def image_resize_image(base64_source, size=(1024, 1024), encoding='base64', "
        "filetype=None, avoid_if_small=False):\n",
        after="def image_process(b64source, size=(0, 0), verify_resolution=False, "
        "quality=0, crop=None, colorize=False, output_format=''):\n",
        at=13,
    ),
    "image_resize_image_big": _CiSnippet(
        before="def image_resize_image_big(base64_source, size=(1024, 1024), "
        "encoding='base64', filetype=None, avoid_if_small=True):\n",
        after="def image_process(b64source, size=(0, 0), verify_resolution=False, "
        "quality=0, crop=None, colorize=False, output_format=''):\n",
        at=13,
    ),
    "image_resize_image_medium": _CiSnippet(
        before="def image_resize_image_medium(base64_source, size=(128, 128), "
        "encoding='base64', filetype=None, avoid_if_small=False):\n",
        after="def image_process(b64source, size=(0, 0), verify_resolution=False, "
        "quality=0, crop=None, colorize=False, output_format=''):\n",
        at=13,
    ),
    "image_resize_image_small": _CiSnippet(
        before="def image_resize_image_small(base64_source, size=(64, 64), "
        "encoding='base64', filetype=None, avoid_if_small=False):\n",
        after="def image_process(b64source, size=(0, 0), verify_resolution=False, "
        "quality=0, crop=None, colorize=False, output_format=''):\n",
        at=13,
    ),
    "oldname": _CiSnippet(
        before='DEPRECATED_ATTRS = [("oldname", "use an upgrade script instead.")]\n',
        after="DEPRECATED_ATTRS: list[tuple[str, str]] = []\n",
        at=14,
    ),
    "pycompat": _CiSnippet(
        before="from . import pycompat\n",
        after="from . import assertion_report\n",
        at=19,
    ),
    "check_field_access_rights": _CiSnippet(
        before="    @api.model\n"
        "    def check_field_access_rights(self, operation, field_names):\n"
        '        """Check the user access rights on the given fields.\n',
        after="    @api.deprecated(\n"
        '        "Deprecated since 19.0, use `_check_field_access` on models."\n'
        '        " To get the list of allowed fields, use `fields_get`.",\n'
        "    )\n"
        "    def check_field_access_rights(self, operation: str, "
        "field_names: list[str] | None) -> list[str]:\n",
        at=19,
    ),
    "_filter_access_rules_python": _CiSnippet(
        before="    def _filter_access_rules_python(self, operation):\n"
        "        dom = self.env['ir.rule']._compute_domain(self._name, operation)\n",
        after="    def _filter_access_rules_python(self, operation):\n"
        "        warnings.warn(\n"
        '            "_filter_access_rules_python() is deprecated since 18.0; '
        'use _filtered_access() instead.",\n'
        "            DeprecationWarning, 1,\n"
        "        )\n",
        at=18,
    ),
    "_check_m2m_recursion": _CiSnippet(
        before="    def _check_m2m_recursion(self, field_name):\n"
        '        """\n'
        "        Verifies that there is no loop in a directed graph of records, by\n",
        after="    def _check_m2m_recursion(self, field_name):\n"
        '        warnings.warn("Since 18.0, one must use not _has_cycle() instead", '
        "DeprecationWarning, 2)\n"
        "        return not self._has_cycle(field_name)\n",
        at=18,
    ),
    "toggle_active": _CiSnippet(
        before="    def toggle_active(self):\n"
        '        "Inverses the value of :attr:`active` on the records in ``self``."\n',
        after='    @api.deprecated("Deprecated since 19.0, use action_archive or '
        'action_unarchive")\n'
        "    def toggle_active(self):\n",
        at=19,
    ),
    "_check_access": _CiSnippet(
        before="    def _check_access(self, operation: str) -> tuple[Self, Callable] | None:\n"
        '        """ Return ``None`` if the current user has permission to perform\n',
        after='    @deprecated("Since 20.0, use Model._access_domain instead")\n'
        "    def _check_access(self, operation: str) -> tuple[Self, Callable] | None:\n",
        at=20,
    ),
    "_check_field_access": _CiSnippet(
        before="    @api.model\n"
        "    def _check_field_access(self, field: Field, "
        "operation: typing.Literal['read', 'write']) -> None:\n",
        after="    @api.model\n"
        '    @deprecated("Since 20.0, use check_field_access()")\n'
        "    def _check_field_access(self, field: Field, "
        "operation: typing.Literal['read', 'write']) -> None:\n",
        at=20,
    ),
    "_init_column": _CiSnippet(
        before="    def _init_column(self, column_name):\n"
        '        """ Initialize the value of the given column for existing rows. """\n',
        after='    @deprecated("Since 20.0, field initialization is defined in '
        'Field.init_storage")\n'
        "    def _init_column(self, column_name):\n",
        at=20,
    ),
    "_table_has_rows": _CiSnippet(
        before="    @ormcache()\n    def _table_has_rows(self) -> bool:\n",
        after="    @api.ormcache()\n"
        '    @deprecated("Since 20.0, _table_has_rows is removed")\n'
        "    def _table_has_rows(self) -> bool:\n",
        at=20,
    ),
    "left_join": _CiSnippet(
        before="    def left_join(self, lhs_alias: str, lhs_column: str, rhs_table: str, "
        "rhs_column: str, link: str) -> str:\n"
        '        """ Add a LEFT JOIN to the current table (if necessary), and return the\n'
        "        alias corresponding to ``rhs_table``.\n"
        "\n"
        "        See the documentation of :meth:`join` for a better overview of the\n"
        "        arguments and what they do.\n"
        '        """\n'
        "        assert lhs_alias in self._tables or lhs_alias in self._joins, "
        '"Alias %r not in %s" % (lhs_alias, str(self))\n',
        after="    def left_join(self, lhs_alias: str, lhs_column: str, rhs_table: str | SQL, "
        "rhs_column: str, link: str) -> str:\n"
        '        """ Add a LEFT JOIN to the current table (if necessary), and return the\n'
        "        alias corresponding to ``rhs_table``.\n"
        "\n"
        "        See the documentation of :meth:`join` for a better overview of the\n"
        "        arguments and what they do.\n"
        '        """\n'
        '        warnings.warn("Since 20.0, use TableSQL.join or add_join", '
        "DeprecationWarning, stacklevel=2)\n",
        at=20,
    ),
    "fix_foreign_key": _CiSnippet(
        before="def fix_foreign_key(cr, tablename1, columnname1, tablename2, columnname2, "
        "ondelete):\n",
        after='@deprecated("Removed after 20.0")\n'
        "def fix_foreign_key(cr, tablename1, columnname1, tablename2, columnname2, "
        "ondelete):\n",
        at=20,
    ),
    "check_index_exist": _CiSnippet(
        before="def check_index_exist(cr, indexname):\n"
        '    assert index_exists(cr, indexname), f"{indexname} does not exist"\n',
        after='@deprecated("Removed after 20.0")\n'
        "def check_index_exist(cr, indexname):\n",
        at=20,
    ),
    "reverse_order": _CiSnippet(
        before='def reverse_order(order):\n    """ Reverse an ORDER BY clause """\n',
        after='@deprecated("Removed after 20.0")\n'
        "def reverse_order(order):\n",
        at=20,
    ),
    "escape_psql": _CiSnippet(
        before="def escape_psql(to_escape):\n"
        r"    return to_escape.replace('\\', r'\\').replace('%', r'\%')"
        r".replace('_', r'\_')" "\n",
        after='@deprecated("Since 20.0, use escape_like_value")\n'
        "def escape_psql(to_escape: str) -> str:\n",
        at=20,
    ),
}


# Second-transition snippets, keyed like _LATER_PROBES: `before` is real text at
# later[0][0] - 1, `after` real text at later[0][0].
_CHECK_ACCESS_20 = (
    "    @api.private  # use has_access\n"
    "    @typing.final\n"
    "    def check_access(self, operation: str) -> None:\n"
)
_FILTERED_ACCESS_20 = (
    "    @typing.final\n"
    "    def _filtered_access(self, operation: str) -> typing.Self:\n"
)
_HAS_CYCLE_20 = "    def _has_cycle(self, field_name=None) -> bool:\n"

_LATER_CI_SNIPPETS: dict[str, _CiSnippet] = {
    "check_access_rights": _CiSnippet(
        before="    @api.model\n"
        '    @api.deprecated("check_access_rights() is deprecated since 18.0; '
        'use check_access() instead.")\n'
        "    def check_access_rights(self, operation, raise_exception=True):\n",
        after=_CHECK_ACCESS_20,
        at=20,
    ),
    "check_access_rule": _CiSnippet(
        before='    @api.deprecated("check_access_rule() is deprecated since 18.0; '
        'use check_access() instead.")\n'
        "    def check_access_rule(self, operation):\n",
        after=_CHECK_ACCESS_20,
        at=20,
    ),
    "check_field_access_rights": _CiSnippet(
        before="    def check_field_access_rights(self, operation: str, "
        "field_names: list[str] | None) -> list[str]:\n",
        after="    @api.model\n"
        "    def check_field_access(self, field: Field, "
        "operation: typing.Literal['read', 'write']) -> None:\n",
        at=20,
    ),
    "_filter_access_rules": _CiSnippet(
        before='    @api.deprecated("_filter_access_rules() is deprecated since 18.0; '
        'use _filtered_access() instead.")\n'
        "    def _filter_access_rules(self, operation):\n",
        after=_FILTERED_ACCESS_20,
        at=20,
    ),
    "_filter_access_rules_python": _CiSnippet(
        before='    @api.deprecated("_filter_access_rules_python() is deprecated since '
        '18.0; use _filtered_access() instead.")\n'
        "    def _filter_access_rules_python(self, operation):\n",
        after=_FILTERED_ACCESS_20,
        at=20,
    ),
    "_check_recursion": _CiSnippet(
        before='    @api.deprecated("Deprecated since 18.0, use _has_cycle() instead")\n'
        "    def _check_recursion(self, parent=None):\n",
        after=_HAS_CYCLE_20,
        at=20,
    ),
    "_check_m2m_recursion": _CiSnippet(
        before='    @api.deprecated("Deprecated since 18.0, use _has_cycle() instead")\n'
        "    def _check_m2m_recursion(self, field_name):\n",
        after=_HAS_CYCLE_20,
        at=20,
    ),
    "toggle_active": _CiSnippet(
        before='    @api.deprecated("Deprecated since 19.0, use action_archive or '
        'action_unarchive")\n'
        "    def toggle_active(self):\n",
        after="    def action_archive(self):\n"
        '        """Set :attr:`active` to ``False`` on a recordset for active records.\n',
        at=20,
    ),
    "read_group": _CiSnippet(
        before="    @api.model\n"
        "    @api.readonly\n"
        '    @api.deprecated("Since 19.0, read_group is deprecated. Please use '
        '_read_group in the backend code or formatted_read_group for a complete '
        'formatted result")\n'
        "    def read_group(self, domain, fields, groupby, offset=0, limit=None, "
        "orderby=False, lazy=True):\n",
        after="    @typing.final\n"
        "    @api.model\n"
        "    def read_group(\n"
        "        self,\n"
        "        domain: DomainType,\n"
        "        groupby: Sequence[str] = (),\n"
        "        aggregates: Sequence[str] = (),\n"
        "        having: DomainType = (),\n",
        at=20,
    ),
    # odoo/tools/pycompat.py exists at 19.0 and is gone at 20.0: the "after"
    # text is the empty content of a missing file.
    "pycompat": _CiSnippet(
        before="# -*- coding: utf-8 -*-\n"
        "#pylint: disable=deprecated-module\n"
        "import csv\n"
        "import codecs\n"
        "import io\n",
        after="",
        at=20,
    ),
    "flush": _CiSnippet(
        before="    def flush(self, fnames=None, records=None):\n"
        "        warnings.warn(\n",
        after="    @api.private\n"
        "    def flush_model(self, fnames=None):\n",
        at=17,
    ),
    "invalidate_cache": _CiSnippet(
        before="    def invalidate_cache(self, fnames=None, ids=None):\n"
        "        warnings.warn(\n",
        after="    @api.private\n"
        "    def invalidate_model(self, fnames=None, flush=True):\n",
        at=17,
    ),
}


def test_ci_snippets_cover_every_probed_entry():
    """Shape guard: every probed name has exactly one CI snippet pair, and vice
    versa - a future probe added without a snippet (or a snippet without a
    probe) fails here instead of silently narrowing CI coverage."""
    assert set(_CI_SNIPPETS) == set(_PROBES)
    assert set(_LATER_CI_SNIPPETS) == set(_LATER_PROBES)


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

    assert snippet.at == entry.since_version, (
        f"{name}: the real source captured at v{snippet.at} says the transition "
        f"is at v{snippet.at}, but since_version={entry.since_version}"
    )
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


@pytest.mark.parametrize("name", sorted(_LATER_PROBES))
def test_later_transition_matches_hardcoded_real_source_snippet(name):
    """CI layer - business rule: the second transition of a name (``later``,
    e.g. removed at 20.0 after a deprecation at 18.0) is pinned against real
    text on both sides of ITS boundary, independently of the first transition
    the entry's ``since_version`` records."""
    entry = _DEPRECATED_API_SYMBOLS[name]
    probe = _LATER_PROBES[name]
    snippet = _LATER_CI_SNIPPETS[name]
    boundary = entry.later[0][0]

    assert snippet.at == boundary, (
        f"{name}: the real source captured at v{snippet.at} says the follow-up "
        f"transition is at v{snippet.at}, but later={entry.later}"
    )
    assert _probe_matches_text(probe, snippet.after) == (probe.mode == "present_from"), (
        f"{name}: the 'after' (v{boundary}) snippet does not exhibit the expected "
        f"probe outcome for mode={probe.mode!r}"
    )
    assert _probe_matches_text(probe, snippet.before) == (probe.mode == "absent_from"), (
        f"{name}: the 'before' (v{boundary - 1}) snippet does not exhibit the "
        f"expected probe outcome for mode={probe.mode!r}"
    )


def test_probe_coverage_matches_every_entry():
    """Shape guard: _PROBES ∪ _UNPROBED must equal every key in
    _DEPRECATED_API_SYMBOLS - a future entry added to the production table
    without EITHER a probe or an explicit _UNPROBED acknowledgement fails
    here, so silent oracle coverage narrowing is impossible. The same holds
    for ``later``: exactly the entries that declare one have a later probe.
    """
    assert set(_PROBES) | _UNPROBED == set(_DEPRECATED_API_SYMBOLS)
    assert set(_PROBES) & _UNPROBED == set()
    with_later = {name for name, e in _DEPRECATED_API_SYMBOLS.items() if e.later}
    assert set(_LATER_PROBES) == with_later


def test_every_entry_has_a_verified_since_version_in_surveyed_range():
    """Shape guard - business rule: since_version is always a real, in-range
    Odoo major (the surveyed range, src.constants), change is one of the closed
    set of categories, a follow-up transition is a single strictly later,
    in-range step, and the dict key always matches the entry's own name (never
    allows a copy-paste key/name mismatch to go unnoticed)."""
    valid_changes = {"removed", "deprecated", "renamed", "moved", "signature_changed", "review"}
    assert _DEPRECATED_API_SYMBOLS
    for key, entry in _DEPRECATED_API_SYMBOLS.items():
        assert isinstance(entry, DeprecatedApiSymbol)
        assert entry.name == key
        assert SURVEYED_MIN_MAJOR <= entry.since_version <= SURVEYED_MAX_MAJOR, (
            f"{key}: since_version={entry.since_version} out of range"
        )
        assert entry.change in valid_changes, f"{key}: unknown change kind {entry.change!r}"
        assert entry.detail, f"{key}: empty detail"
        assert len(entry.later) <= 1, f"{key}: _LATER_PROBES checks one follow-up step"
        for major, change in entry.later:
            assert entry.since_version < major <= SURVEYED_MAX_MAJOR, (
                f"{key}: later transition v{major} must follow since_version "
                f"{entry.since_version} inside the surveyed range"
            )
            assert change in valid_changes - {"review"}, f"{key}: unknown later {change!r}"


@pytest.mark.parametrize("name", sorted(_LATER_PROBES))
def test_first_probe_stops_before_the_later_transition(name):
    """Shape guard - business rule: a first-transition probe that looks for text
    (``present_from``: a deprecation warning, a signature) is never asked about
    a major at or past the follow-up, where that text vanishes; a probe for an
    absence persists through the follow-up and needs no bound. The follow-up
    probe brackets its own boundary - so each real-source check asserts one
    fact."""
    boundary = _DEPRECATED_API_SYMBOLS[name].later[0][0]
    if name in _PROBES and _PROBES[name].mode == "present_from":
        assert _PROBES[name].check_max_major < boundary, (
            f"{name}: first-transition probe reaches v{_PROBES[name].check_max_major}, "
            f"past the later transition at v{boundary}"
        )
    later_probe = _LATER_PROBES[name]
    assert later_probe.check_min_major <= boundary - 1 <= later_probe.check_max_major


# Names that look like deprecated Odoo API but would flag correct code, with the
# reason. Each is a deprecated CoreSymbol short name at 20.0 (odoo/orm/*.py) that
# the call-name match cannot tell apart from legitimate calls.
_NOT_LISTED = {
    "clear": "Environment.clear() shares its short name with dict/set/list.clear()",
    "join": "Query.join() shares its short name with str.join()",
    "add_where": "Query.add_where() warns only for a str clause or params, not for SQL",
    "ormcache": "odoo.tools.ormcache is an alias of the supported odoo.api.ormcache",
}


@pytest.mark.parametrize("name", sorted(_NOT_LISTED))
def test_ambiguous_names_are_not_flagged_as_deprecated_usage(name):
    """Business rule - false-positive surface (ADR-0002 section 3): a call whose
    short name is also a legitimate, non-deprecated call is not recorded as
    core_symbol_refs, because the USES_CORE_SYMBOL edge matches by short name
    and would flag correct 20.0 code."""
    assert name not in _DEPRECATED_API_SYMBOLS, _NOT_LISTED[name]
    fn = ast.parse(f"def m(self):\n    cache.{name}()\n    {name}()\n").body[0]
    assert _extract_core_symbol_refs(fn) == []


@pytest.mark.parametrize(
    "call,expected",
    [
        ("self._check_access('read')", ["_check_access"]),
        ("self._check_field_access(field, 'read')", ["_check_field_access"]),
        ("super()._init_column(column_name)", ["_init_column"]),
        ("self._table_has_rows()", ["_table_has_rows"]),
        ("query.left_join('a', 'b', 'c', 'd', 'link')", ["left_join"]),
        ("sql.fix_foreign_key(cr, 'a', 'b', 'c', 'd', 'cascade')", ["fix_foreign_key"]),
        ("sql.check_index_exist(cr, 'idx')", ["check_index_exist"]),
        ("sql.reverse_order(order)", ["reverse_order"]),
        ("sql.escape_psql(value)", ["escape_psql"]),
        ("self.toggle_active()", ["toggle_active"]),
        ("self._check_m2m_recursion('x')", ["_check_m2m_recursion"]),
        ("self.check_field_access_rights('read', None)", ["check_field_access_rights"]),
        ("self._filter_access_rules_python('read')", ["_filter_access_rules_python"]),
        ("self.read_group(domain, ['f'], ['g'])", ["read_group"]),
        ("self.check_access('read')", []),
        ("self.invalidate_access_cache()", []),
    ],
)
def test_parser_records_the_call_names_of_the_curated_hazards(call, expected):
    """Business rule: a method that calls a name curated as a migration hazard
    gets it in core_symbol_refs (the source of the USES_CORE_SYMBOL edge), and
    the supported replacement API is never recorded."""
    fn = ast.parse(f"def m(self):\n    {call}\n").body[0]
    assert _extract_core_symbol_refs(fn) == expected


# ---------------------------------------------------------------------------
# Layer 2 - dev-box (real checkouts; skips per (name, major) when absent).
# ---------------------------------------------------------------------------


def _dev_box_cases() -> list[tuple[str, int]]:
    cases: list[tuple[str, int]] = []
    for name, probe in sorted(_PROBES.items()):
        for major in range(probe.check_min_major, probe.check_max_major + 1):
            cases.append((name, major))
    return cases


def _later_dev_box_cases() -> list[tuple[str, int]]:
    cases: list[tuple[str, int]] = []
    for name, probe in sorted(_LATER_PROBES.items()):
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


@pytest.mark.odoo_source
@pytest.mark.parametrize("name,major", _later_dev_box_cases())
def test_later_transition_matches_real_checkout_per_major(name, major):
    """Dev-box layer - business rule: a name's follow-up transition (``later``)
    flips exactly at its own boundary in the real source tree, whatever the
    first transition did before it."""
    root = checkout_root(major)
    if root is None:
        pytest.skip(f"Odoo {major} checkout not found (set OSM_ODOO_CHECKOUTS to override)")

    boundary = _DEPRECATED_API_SYMBOLS[name].later[0][0]
    probe = _LATER_PROBES[name]
    matched = _probe_matches_checkout(probe, root, major)
    expected = _expected_match(probe, major, boundary)
    assert matched == expected, (
        f"{name} at v{major}.0: later probe found match={matched}, expected {expected} "
        f"(later boundary v{boundary}, mode={probe.mode}) - "
        f"file {root / _era_relpath(probe.relpath, major)}"
    )
