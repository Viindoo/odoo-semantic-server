# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_tools_symbols_content_parity.py
"""Content-parity tests for tools_symbols curated data (issue #364, phase2-C audit).

WHY THIS FILE EXISTS
---------------------
`tests/test_parser_tools_symbols.py` proves loader contract + JSON-schema
validity + a handful of hand-picked lifecycle boundaries (SQL absent-then-
present, image_resize_image removed v13, format_datetime introduced v13). It
never compares curated content against real `odoo.tools.*`/`openerp.tools.*`
source - a curated JSON could ship a stale `signature` string or a `status`
that was never true and that file would stay green. This file closes that gap.

TWO PARTS
----------
Part A (this file's primary deliverable, RUNS TODAY) pins the two CONFIRMED
data errors the phase2-C audit individually verified against real Odoo source,
so a later fix is provable (RED now, GREEN once `spec_data/tools_symbols_*.json`
is corrected - not touched by this file):

  1. `odoo.tools.pycompat` is marked `status: "stable"` at v8.0/v9.0/v10.0, but
     the real module (`openerp/tools/pycompat.py` / `odoo/tools/pycompat.py`)
     does not exist on disk until v11.0 (upstream history dates its
     introduction to the v11 dev cycle - phase2-C audit section 3 item 1).
     Same failure shape as issue #362: a version's own file asserts a false
     claim about that version's real source.
  2. `odoo.tools.image_process`'s `qualified_name` was invalid specifically at
     v19.0: the flat `odoo.tools.image_process` re-export was dropped from
     `odoo/tools/__init__.py` at v19 (only `odoo.tools.image.image_process`
     resolves there now) - phase2-C audit section 3 item 2b. FIXED (issue #364
     follow-up): the flat entry is now OMITTED from tools_symbols_19.0.json
     (schema's own "'removed' = absent, omit it" convention, same one already
     applied to pycompat in that file) rather than kept `deprecated` with a
     `replacement_qname` - `deprecated` means still-importable-but-discouraged
     per the schema, and the flat name is not importable at all at v19. Only
     `odoo.tools.image.image_process` (status `stable`) represents it there
     now. The signature-accuracy checks below (item 2a, curated `signature`
     string vs real params) follow that move for v19 - see
     `_image_process_qname_for`.

These checks read real source directly (file existence, an AST-first /
text-regex-on-SyntaxError parameter extraction, and a plain substring check on
`__init__.py`) - they do NOT depend on Part B's not-yet-built oracle. A single
fact does not need a general-purpose symbol walker to be checked one file at a
time.

Part C checks every curated entry of every surveyed major against the source of
that major: parameters and defaults, return-type names, aliases, constants,
instances, class definitions and deprecation status (see the section header
before `_spec_of_args`).

Part B is the demanded, not-yet-built production oracle contract
(`parse_tools_symbols`) - see that section's docstring for the full API this
file codes against and why it fails with a clean ImportError today (same
"write the test against the API you expect" pattern issue #362 used for
`parse_framework_bases`, commit c1f618b).

Discovery for every real-checkout test in this file goes through
`tests/_odoo_checkouts.py` (issue #364 D2 SSOT) - no second discovery
mechanism is invented here.

EXPECT RED (do not weaken these to reach green - see the top-level task brief):
  - every test in Part B (ImportError: parse_tools_symbols does not exist yet)

NO LONGER RED - fixed by prior curated-data corrections (pycompat at v8-v10,
image_process signature/expand/padding at v18-v19) plus this file's own
issue #364 follow-up (image_process qualified_name modeling at v19):
  - test_pycompat_claimed_stable_before_the_module_exists_in_real_source[8,9,10]
  - test_image_process_signature_is_frozen_copy_paste... (all 7 versions)
  - test_image_process_v18_v19_curated_signature_missing_expand_and_padding
    (both versions)
  - test_image_process_qualified_name_no_longer_resolves_at_v19
Part A's job (real-source parity) is otherwise still incomplete for OTHER
tools_symbols_*.json versions (v13-v17 image_process signature accuracy is
out of this file's control - those JSON files belong to other work), so new
real defects there would still legitimately show up red here; none currently
do.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from src.constants import ODOO_NAMESPACE_LEGACY_MAX_MAJOR
from src.indexer.parser_tools_symbols import _load_static_tools_symbols
from src.indexer.parser_util import parse_external_source
from tests._odoo_checkouts import SURVEYED_MAJORS, checkout_root

_SPEC_DATA_DIR = Path(__file__).parent.parent / "src" / "indexer" / "spec_data"

_RE_IMAGE_PROCESS_DEF = re.compile(r"^def\s+image_process\s*\(([^)]*)\)\s*:", re.MULTILINE)
_RE_CURATED_FIRST_PARAM = re.compile(r"image_process\(\s*([A-Za-z_]\w*)")


def _era_prefix(major: int) -> str:
    """'openerp' for v8-v9, 'odoo' for v10+ - mirrors the boundary
    `framework_bases.py`'s own `_ERA_PREFIX_REGISTRY` and
    `parser_odoo_core.py`'s `_PREFIX_REGISTRY` already use (SSOT: reuse the
    published `ODOO_NAMESPACE_LEGACY_MAX_MAJOR` constant, never a second
    hardcoded "9" - phase2-C audit section 4 flags exactly this duplication
    risk for a future oracle).
    """
    return "openerp" if major <= ODOO_NAMESPACE_LEGACY_MAX_MAJOR else "odoo"


def _tools_dir(root: Path, major: int) -> Path:
    return root / _era_prefix(major) / "tools"


def _curated_entry(version: str, qualified_name: str):
    symbols = _load_static_tools_symbols(version, static_data_dir=_SPEC_DATA_DIR)
    return next((s for s in symbols if s.qualified_name == qualified_name), None)


def _majors(min_major: int, max_major: int | None = None) -> list[int]:
    """Surveyed majors in [min_major, max_major] (SSOT: SURVEYED_MAJORS)."""
    return [
        m for m in SURVEYED_MAJORS
        if m >= min_major and (max_major is None or m <= max_major)
    ]


def _versions(min_major: int, max_major: int | None = None) -> list[str]:
    return [f"{m}.0" for m in _majors(min_major, max_major)]


def _image_process_qname_for(version: str) -> str:
    """Which curated qualified_name carries image_process's signature truth
    at `version`.

    Every version before 19.0 keys off the flat `odoo.tools.image_process`
    re-export. v19 and later intentionally OMIT that flat entry (issue #364
    Problem 2 - the re-export was dropped from odoo/tools/__init__.py at v19,
    so the flat name no longer resolves there; see
    test_image_process_qualified_name_no_longer_resolves_from_v19 for the full
    ground-truth argument). Their signature truth therefore lives under the
    replacement path, odoo.tools.image.image_process, instead - tests that
    inspect the curated signature must follow that move rather than looking
    up a qualified_name the data no longer carries.
    """
    major = int(version.split(".")[0])
    return "odoo.tools.image.image_process" if major >= 19 else "odoo.tools.image_process"


def _real_image_process_params(root: Path) -> list[str] | None:
    """Real image_process() parameter NAMES (order-preserved) from
    <root>/odoo/tools/image.py. AST-first, text-regex fallback ONLY on
    SyntaxError - the established two-tier convention
    (src/indexer/framework_bases.py's `_scan_source` /
    parser_python.py:993-1013) reused here rather than inventing a third
    mechanism, even though image.py is v13+-only and the fallback is not
    expected to trigger for these versions in practice.
    """
    image_py = root / "odoo" / "tools" / "image.py"
    if not image_py.is_file():
        return None
    source = image_py.read_text(encoding="utf-8", errors="ignore")
    try:
        tree = ast.parse(source, filename=str(image_py))
    except SyntaxError:
        m = _RE_IMAGE_PROCESS_DEF.search(source)
        if not m:
            return None
        return [p.strip().split("=")[0].strip() for p in m.group(1).split(",") if p.strip()]
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "image_process":
            return [a.arg for a in node.args.args]
    return None


# ---------------------------------------------------------------------------
# Part A - pin the two CONFIRMED data errors (phase2-C audit section 3,
# "Individually investigated - real, demonstrated errors"). Dev-box only;
# skips per version when the checkout is absent, never fails on absence.
# ---------------------------------------------------------------------------


@pytest.mark.odoo_source
@pytest.mark.parametrize("major", [8, 9, 10])
def test_pycompat_claimed_stable_before_the_module_exists_in_real_source(major):
    """Business rule: `odoo.tools.pycompat` is marked status='stable' in
    tools_symbols_{8,9,10}.0.json, but the real module does not exist as a
    file on disk at any of those versions - upstream history dates its
    introduction to the v11.0 dev cycle (phase2-C audit section 3 item 1).
    Same failure shape as issue #362: a version's own file asserts a false
    claim about that version's real source. EXPECT RED for v8/v9/v10.
    """
    root = checkout_root(major)
    if root is None:
        pytest.skip(f"Odoo {major} checkout not found (set OSM_ODOO_CHECKOUTS)")
    pycompat_file = _tools_dir(root, major) / "pycompat.py"
    assert not pycompat_file.is_file(), (
        f"ground-truth check itself is broken: expected pycompat.py to be "
        f"absent at v{major} ({pycompat_file})"
    )

    version = f"{major}.0"
    entry = _curated_entry(version, "odoo.tools.pycompat")
    assert entry is None or entry.status != "stable", (
        f"tools_symbols_{version}.json claims odoo.tools.pycompat "
        f"status={entry.status if entry else None!r}, but the real module "
        f"does not exist on disk at v{major} ({pycompat_file} absent)"
    )


@pytest.mark.odoo_source
def test_pycompat_correctly_present_from_v11():
    """Control: v11.0 IS the real introduction point (file presence AND
    upstream git history both confirm it) - if this ever goes red, something
    else changed (the checkout or the curated entry), not "more of the same
    v8-v10 defect" above.
    """
    root = checkout_root(11)
    if root is None:
        pytest.skip("Odoo 11 checkout not found (set OSM_ODOO_CHECKOUTS)")
    pycompat_file = _tools_dir(root, 11) / "pycompat.py"
    assert pycompat_file.is_file(), (
        "ground-truth check itself is broken: v11 should define pycompat.py"
    )

    entry = _curated_entry("11.0", "odoo.tools.pycompat")
    assert entry is not None and entry.status == "stable"


@pytest.mark.odoo_source
@pytest.mark.parametrize("version", _versions(13))
def test_image_process_signature_is_frozen_copy_paste_never_matching_real_params(version):
    """Business rule: image_process's curated `signature` string must match
    the real parameter list at each version - historically it was a single
    copy-pasted string across all versions (phase2-C audit section 3 item
    2a: real first positional parameter is 'base64_source' at v13-v15, then
    'source' from v16 on). v19 and later look up the entry under
    odoo.tools.image.image_process rather than the flat odoo.tools.image_process
    - see `_image_process_qname_for` (issue #364 Problem 2: the flat qname is
    intentionally omitted at v19).
    """
    major = int(version.split(".")[0])
    root = checkout_root(major)
    if root is None:
        pytest.skip(f"Odoo {major} checkout not found (set OSM_ODOO_CHECKOUTS)")
    real_params = _real_image_process_params(root)
    assert real_params, (
        f"ground-truth check itself is broken: could not find "
        f"image_process() in v{major} odoo/tools/image.py"
    )

    qname = _image_process_qname_for(version)
    entry = _curated_entry(version, qname)
    assert entry is not None, f"{qname} missing from tools_symbols_{version}.json"
    curated_sig = entry.signature or ""
    m = _RE_CURATED_FIRST_PARAM.search(curated_sig)
    curated_first_param = m.group(1) if m else None
    # Exact match, NOT substring containment: real 'source' is a substring of
    # curated 'b64source' (it literally ends in "source"), so a naive `in`
    # check would silently pass at v16+ despite the params being different
    # names - exactly the kind of false-green a frozen copy-paste string can
    # produce. Compare the two first-parameter NAMES for equality instead.
    assert curated_first_param == real_params[0], (
        f"v{major}: curated image_process signature {curated_sig!r} has "
        f"first parameter {curated_first_param!r}, but real source's first "
        f"parameter is {real_params[0]!r} (real params: {real_params})"
    )


@pytest.mark.odoo_source
@pytest.mark.parametrize("version", _versions(18))
def test_image_process_v18_plus_curated_signature_carries_expand_and_padding(version):
    """Business rule: real image_process() gained `expand` and `padding`
    parameters at v18 - the curated signature must carry them from v18 on.
    v19 and later look up the entry under odoo.tools.image.image_process rather than
    the flat odoo.tools.image_process - see `_image_process_qname_for`
    (issue #364 Problem 2: the flat qname is intentionally omitted at v19).
    """
    major = int(version.split(".")[0])
    root = checkout_root(major)
    if root is None:
        pytest.skip(f"Odoo {major} checkout not found (set OSM_ODOO_CHECKOUTS)")
    real_params = _real_image_process_params(root)
    assert real_params and "expand" in real_params and "padding" in real_params, (
        f"ground-truth check itself is broken: v{major} image_process() "
        f"should define expand/padding, got {real_params}"
    )

    qname = _image_process_qname_for(version)
    entry = _curated_entry(version, qname)
    assert entry is not None, f"{qname} missing from tools_symbols_{version}.json"
    curated_sig = entry.signature or ""
    assert "expand" in curated_sig and "padding" in curated_sig, (
        f"v{major}: curated image_process signature is missing the real "
        f"expand/padding parameters added at v18: {curated_sig!r}"
    )


@pytest.mark.odoo_source
@pytest.mark.parametrize("major", _majors(19))
def test_image_process_qualified_name_no_longer_resolves_from_v19(major):
    """Business rule: odoo.tools.image_process was re-exported via `from
    .image import image_process` in odoo/tools/__init__.py through v18; that
    re-export was dropped at v19 and stays dropped (no 'image' reference
    anywhere in odoo/tools/__init__.py from v19 on) - the curated data must
    reflect that the flat qualified_name no longer resolves as a real import
    path there.

    NOTE (issue #364 follow-up): the original version of this assertion
    (`assert real_still_reexports`) was inverted relative to this docstring
    and its own failure message - it demanded the real v19 checkout still
    re-export image_process, which is a fact about upstream source no
    curated-data edit could ever change, and which is also simply false
    (confirmed against odoo/tools/__init__.py of the v19 checkout). Fixed
    here to assert the real rule the docstring always described: the
    re-export IS gone at v19, and the curated data must model that by
    OMITTING the flat qualified_name (schema's own "'removed' = absent - do
    not include removed symbols, omit them instead" rule; same convention
    already applied to pycompat in tools_symbols_19.0.json) rather than
    keeping it 'deprecated' with a replacement_qname - 'deprecated' means
    still-importable-but-discouraged per the schema, and the flat name is not
    importable at all at v19. The replacement path
    (odoo.tools.image.image_process, the one v19's own test_image.py imports)
    must be present and 'stable' instead.
    """
    root = checkout_root(major)
    if root is None:
        pytest.skip(f"Odoo {major} checkout not found (set OSM_ODOO_CHECKOUTS)")
    version = f"{major}.0"
    init_py = root / "odoo" / "tools" / "__init__.py"
    assert init_py.is_file()
    real_still_reexports = "image_process" in init_py.read_text(encoding="utf-8", errors="ignore")
    assert not real_still_reexports, (
        f"ground-truth check itself is broken (or v{major} has changed upstream): "
        f"expected v{major}'s odoo/tools/__init__.py to no longer mention "
        "image_process at all - if this fires, re-derive the curated-data "
        "decision below from source, do not just flip this assertion back"
    )

    flat_entry = _curated_entry(version, "odoo.tools.image_process")
    assert flat_entry is None, (
        f"tools_symbols_{version}.json still carries a flat odoo.tools.image_process "
        f"entry (status={flat_entry.status if flat_entry else None!r}), but "
        f"v{major}'s odoo/tools/__init__.py no longer re-exports it - per the "
        "schema's 'removed = absent, omit it' convention (already applied to "
        "pycompat in this same file), this qualified_name must be omitted "
        "entirely, not kept as 'deprecated'"
    )

    replacement_entry = _curated_entry(version, "odoo.tools.image.image_process")
    assert replacement_entry is not None and replacement_entry.status == "stable", (
        f"tools_symbols_{version}.json must carry a 'stable' "
        "odoo.tools.image.image_process entry - the real resolving path "
        "v19's own test_image.py imports ('from odoo.tools import image as tools')"
    )


@pytest.mark.odoo_source
def test_image_process_qualified_name_correctly_resolves_at_v18():
    """Control: v18's odoo/tools/__init__.py DOES still re-export
    image_process, so the flat qname is valid there - a regression here
    would mean the checkout or __init__.py itself changed, not "more of the
    same v19 defect" above.
    """
    root = checkout_root(18)
    if root is None:
        pytest.skip("Odoo 18 checkout not found (set OSM_ODOO_CHECKOUTS)")
    init_py = root / "odoo" / "tools" / "__init__.py"
    assert "image_process" in init_py.read_text(encoding="utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# Part B - the demanded, not-yet-built production oracle contract (issue
# #364). `src/indexer/parser_tools_symbols.py` has no `parse_tools_symbols`
# today - this section defines the EXPECTED API by importing it, deferred to
# inside each test body (never at module level) so `pytest --collect-only`
# stays clean; the ImportError below is a RUNTIME failure - proof the
# contract is demanded but unmet - never a collection error. Same pattern
# `test_framework_bases_parity.py` uses for `parse_framework_bases`
# (`_import_framework_bases`), which is how issue #362's WI-4/WI-6 was driven
# (commit c1f618b).
#
# EXPECTED CONTRACT (mirrors parse_framework_bases's shape, ADR-0054):
#   parse_tools_symbols(odoo_source_root: str | Path, odoo_version: str)
#       -> dict[str, ParsedToolSymbol] | None
#   ParsedToolSymbol: name, qualified_name, signature, file_path, line
#   None only when no readable <era-prefix>/tools/__init__.py exists at that
#   root+version (mirrors parse_framework_bases's "no readable common.py"
#   contract for the missing-source case).
#
# A CORRECT implementation MUST (phase2-C audit section 2, "two concrete
# under-recovery modes" - both individually confirmed against real source):
#   1. Descend one level into module-scope `if/else` bodies for def/class/
#      assignment discovery. `html_escape` is bound this way (not a bare
#      top-level def) at v8, v9, v10, v11, v13, v15, v16, v17 - a
#      tree.body-only walk under-recovers it at exactly those versions.
#   2. Track `ImportFrom` bindings inside star-imported submodules,
#      TRANSITIVELY. `ustr` is never itself a def/class/assignment in
#      misc.py - it arrives via `from odoo.loglevels import ustr` (or
#      `openerp.loglevels` pre-v10) inside misc.py, which is itself
#      star-re-exported by odoo/tools/__init__.py's `from .misc import *`.
# A naive implementation that skips either will make the tests below go red
# specifically on html_escape/ustr - that IS the visibility issue #364 asks
# for (see the audit for the exact source lines), not a test bug.
# ---------------------------------------------------------------------------


def _import_tools_symbols_oracle():
    """Deferred import of the not-yet-built oracle - see Part B docstring."""
    from src.indexer.parser_tools_symbols import parse_tools_symbols
    return parse_tools_symbols


def _curated_qnames(version: str) -> set[str]:
    symbols = _load_static_tools_symbols(version, static_data_dir=_SPEC_DATA_DIR)
    return {s.qualified_name for s in symbols}


@pytest.mark.odoo_source
@pytest.mark.parametrize("major", SURVEYED_MAJORS)
def test_curated_tools_symbols_match_live_ast_oracle_per_version(major):
    """T-364 dev-box parity layer (mirrors test_framework_bases_parity.py's
    dev-box layer shape) - business rule: every curated odoo.tools.* symbol
    at version N must be recoverable by an independent AST walk of the real
    <era>/tools/ package at that version. The phase2-C audit's throwaway
    probe already proved this is feasible across all 12 versions
    (`probe_tools_symbols.py`); this test demands the production version of
    that probe as a first-class, wired-in oracle - the same drift-alarm
    contract issue #362/#363 established for framework_bases.py, extended to
    this family. Currently fails with ImportError for every version -
    parse_tools_symbols does not exist yet. That IS the deliverable (module
    docstring): a later commit builds the oracle to this contract and this
    test starts passing, PROVIDED it handles the two under-recovery modes
    named in the Part B docstring (html_escape / ustr) - a naive
    implementation would newly fail on exactly those two symbols instead.
    """
    root = checkout_root(major)
    if root is None:
        pytest.skip(f"Odoo {major} checkout not found (set OSM_ODOO_CHECKOUTS)")
    parse_tools_symbols = _import_tools_symbols_oracle()

    version = f"{major}.0"
    parsed = parse_tools_symbols(root, version)
    assert parsed is not None, f"parse_tools_symbols returned None for real checkout {root}"

    curated = _curated_qnames(version)
    recovered = {f"odoo.tools.{name}" for name in parsed}
    missing_from_oracle = curated - recovered
    assert not missing_from_oracle, (
        f"v{major}: curated tools_symbols_{version}.json names not "
        f"recoverable by the live AST oracle: {sorted(missing_from_oracle)}"
    )


@pytest.mark.odoo_source
@pytest.mark.parametrize("major", [8, 9, 10, 11, 13, 15, 16, 17])
def test_oracle_recovers_html_escape_bound_inside_module_scope_if_else(major):
    """Under-recovery mode 1 (Part B docstring): html_escape is defined at
    module scope INSIDE an if/else at these versions (real source, confirmed
    byte-identical shape at v8 and v13 - phase2-C audit section 2 item 1),
    not as a bare top-level def. A tree.body-only walker misses it; the
    production oracle must descend one level into module-scope If bodies.
    Currently ImportError (oracle not built yet) - this test exists so a
    future naive implementation fails HERE, specifically, rather than only
    inside the aggregate per-version diff above.
    """
    root = checkout_root(major)
    if root is None:
        pytest.skip(f"Odoo {major} checkout not found (set OSM_ODOO_CHECKOUTS)")
    parse_tools_symbols = _import_tools_symbols_oracle()
    parsed = parse_tools_symbols(root, f"{major}.0")
    assert parsed is not None
    assert "html_escape" in parsed, (
        f"v{major}: oracle failed to recover html_escape, which real source "
        "binds inside a module-scope if/else (not a bare top-level def) - "
        "see phase2-C audit section 2 item 1"
    )


# Last major whose odoo/loglevels.py still defines `ustr` (and therefore whose
# odoo.tools namespace carries it); v20 dropped it.
_USTR_LAST_MAJOR = 19


@pytest.mark.odoo_source
@pytest.mark.parametrize("major", _majors(0, _USTR_LAST_MAJOR))
def test_oracle_recovers_ustr_bound_via_transitive_import_reexport(major):
    """Under-recovery mode 2 (Part B docstring): ustr is never itself
    def'd/assigned in misc.py - it arrives via `from odoo.loglevels import
    ustr` then misc.py's inclusion in `from .misc import *` (confirmed
    present at every version v8-v19 - phase2-C audit section 2 item 2). A
    walker that only inspects FunctionDef/ClassDef/Assign misses it entirely
    unless it tracks ImportFrom bindings transitively. Currently ImportError
    (oracle not built yet) - see test_oracle_recovers_html_escape... above
    for why this is a separate, individually-nameable test rather than only
    a symptom buried in the aggregate diff.
    """
    root = checkout_root(major)
    if root is None:
        pytest.skip(f"Odoo {major} checkout not found (set OSM_ODOO_CHECKOUTS)")
    parse_tools_symbols = _import_tools_symbols_oracle()
    parsed = parse_tools_symbols(root, f"{major}.0")
    assert parsed is not None
    assert "ustr" in parsed, (
        f"v{major}: oracle failed to recover ustr, which real source binds "
        "via a transitive ImportFrom re-export, not a direct def/class/"
        "assignment - see phase2-C audit section 2 item 2"
    )


@pytest.mark.odoo_source
@pytest.mark.parametrize("major", SURVEYED_MAJORS)
def test_oracle_recovers_exception_to_unicode_via_transitive_import_reexport(major):
    """The same under-recovery mode 2 mechanism as ustr above, witnessed by a
    name that exists at EVERY surveyed version: exception_to_unicode is bound
    in misc.py only through `from odoo.loglevels import exception_to_unicode`
    and reaches odoo.tools through `from .misc import *`.
    """
    root = checkout_root(major)
    if root is None:
        pytest.skip(f"Odoo {major} checkout not found (set OSM_ODOO_CHECKOUTS)")
    parse_tools_symbols = _import_tools_symbols_oracle()
    parsed = parse_tools_symbols(root, f"{major}.0")
    assert parsed is not None
    assert "exception_to_unicode" in parsed, (
        f"v{major}: oracle failed to recover exception_to_unicode, which real "
        "source binds via a transitive ImportFrom re-export"
    )


@pytest.mark.odoo_source
@pytest.mark.parametrize("major", _majors(_USTR_LAST_MAJOR + 1))
def test_ustr_is_gone_from_the_namespace_from_v20(major):
    """Business rule: ustr no longer resolves off odoo.tools once odoo/loglevels.py
    stops defining it, and a name that does not resolve is omitted from the
    curated data (tools_symbol.schema.json status doc) - never kept 'stable'.
    The source fact is checked on the file itself, independent of the oracle.
    """
    root = checkout_root(major)
    if root is None:
        pytest.skip(f"Odoo {major} checkout not found (set OSM_ODOO_CHECKOUTS)")
    loglevels = (root / "odoo" / "loglevels.py").read_text(encoding="utf-8", errors="ignore")
    assert "def ustr" not in loglevels, (
        f"ground-truth check itself is broken: v{major} odoo/loglevels.py defines ustr"
    )
    parse_tools_symbols = _import_tools_symbols_oracle()
    parsed = parse_tools_symbols(root, f"{major}.0")
    assert parsed is not None
    assert "ustr" not in parsed
    assert "odoo.tools.ustr" not in _curated_qnames(f"{major}.0"), (
        f"tools_symbols_{major}.0.json lists odoo.tools.ustr, which does not resolve at v{major}"
    )


@pytest.mark.odoo_source
@pytest.mark.parametrize("major", _majors(20))
def test_oracle_exposes_safe_eval_package_surface_from_v20(major):
    """From v20 safe_eval is a package whose __init__.py re-exports its
    submodules. The oracle must reach those names (`odoo.tools.safe_eval.
    safe_eval`) through the package, not only through `.py` files.
    """
    root = checkout_root(major)
    if root is None:
        pytest.skip(f"Odoo {major} checkout not found (set OSM_ODOO_CHECKOUTS)")
    assert (root / "odoo" / "tools" / "safe_eval" / "__init__.py").is_file(), (
        f"ground-truth check itself is broken: v{major} has no safe_eval package"
    )
    parse_tools_symbols = _import_tools_symbols_oracle()
    parsed = parse_tools_symbols(root, f"{major}.0")
    assert parsed is not None
    assert parsed["safe_eval"].signature == "module"
    assert parsed["safe_eval.safe_eval"].file_path == "odoo/tools/safe_eval/evaluation.py"
    assert parsed["safe_eval.expr_eval"].file_path == "odoo/tools/safe_eval/expression.py"


@pytest.mark.odoo_source
@pytest.mark.parametrize("major", _majors(20))
def test_ormcache_is_a_deprecated_alias_served_by_module_getattr_from_v20(major):
    """From v20 odoo.tools.ormcache is not bound by an import: the module-level
    __getattr__ of odoo/tools/__init__.py serves it with a DeprecationWarning.
    The oracle must recover it, and the curated entry must be 'deprecated'
    with odoo.api.ormcache as replacement.
    """
    root = checkout_root(major)
    if root is None:
        pytest.skip(f"Odoo {major} checkout not found (set OSM_ODOO_CHECKOUTS)")
    init_text = (root / "odoo" / "tools" / "__init__.py").read_text(encoding="utf-8")
    assert "def __getattr__" in init_text and "DeprecationWarning" in init_text, (
        f"ground-truth check itself is broken: v{major} odoo/tools/__init__.py "
        "no longer serves a deprecated name through __getattr__"
    )
    parse_tools_symbols = _import_tools_symbols_oracle()
    parsed = parse_tools_symbols(root, f"{major}.0")
    assert parsed is not None
    assert parsed["ormcache"].file_path == "odoo/tools/__init__.py"

    entry = _curated_entry(f"{major}.0", "odoo.tools.ormcache")
    assert entry is not None and entry.status == "deprecated"
    assert entry.replacement_qname == "odoo.api.ormcache"


# ---------------------------------------------------------------------------
# Part C - every curated entry of every surveyed major is checked against the
# real source of that major: parameters, defaults, constants, aliases, class
# existence, return-type names and deprecation status. The oracle's own
# signature is the parameter ground truth wherever it carries one; a name it
# binds through an import or a class (no signature) is read at its definition
# site by `_locate`.
# ---------------------------------------------------------------------------

_ParamSpec = list[tuple[str, str | None]]


def _spec_of_args(args: ast.arguments) -> _ParamSpec:
    """(name, default source) per parameter, in call order; `*`/`**` prefixed
    for variadics. Annotations are not part of the spec.
    """
    positional = [*args.posonlyargs, *args.args]
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    spec: _ParamSpec = [
        (a.arg, ast.unparse(d) if d is not None else None)
        for a, d in zip(positional, defaults, strict=True)
    ]
    if args.vararg:
        spec.append((f"*{args.vararg.arg}", None))
    spec.extend(
        (a.arg, ast.unparse(d) if d is not None else None)
        for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True)
    )
    if args.kwarg:
        spec.append((f"**{args.kwarg.arg}", None))
    return spec


def _signature_spec(signature: str) -> _ParamSpec:
    """Parameter spec of a `name(params) [-> ret]` signature string."""
    head = signature[: signature.rindex(")") + 1]
    return _spec_of_args(ast.parse(f"def {head}: pass").body[0].args)  # type: ignore[attr-defined]


def _return_hint(signature: str) -> str | None:
    tail = signature[signature.rindex(")") + 1:].strip()
    return tail.removeprefix("->").strip() if tail.startswith("->") else None


_RE_FUNCTION_SIGNATURE = re.compile(r"^\w+\(.*\)(?:\s*->\s*.+)?$")
_RE_ALIAS_SIGNATURE = re.compile(r"^(\w+) = ([\w.]+)$")
_RE_CONSTANT_SIGNATURE = re.compile(r"^str = (.+)$")
_RE_INSTANCE_SIGNATURE = re.compile(r"^odoo\.tools\.(\w+)\.(\w+)$")


def _signature_kind(signature: str | None) -> str:
    """function | class | module | alias | constant | instance | other."""
    sig = signature or ""
    if sig == "module":
        return "module"
    if sig.startswith("class "):
        return "class"
    if _RE_ALIAS_SIGNATURE.match(sig):
        return "alias"
    if _RE_CONSTANT_SIGNATURE.match(sig):
        return "constant"
    if _RE_INSTANCE_SIGNATURE.match(sig):
        return "instance"
    return "function" if _RE_FUNCTION_SIGNATURE.match(sig) else "other"


class _Site:
    """Where a name is defined: the file (relative to the checkout root), its
    text and, when the file parses, the defining AST node.
    """

    def __init__(self, rel: str, text: str, tree: ast.Module | None, node: ast.AST | None):
        self.rel, self.text, self.tree, self.node = rel, text, tree, node


def _module_file(root: Path, module: str, importer_rel: str, level: int) -> str | None:
    """Relative path of the file `from <module> import ...` reads, or None
    when it is not part of the checkout (stdlib, third-party).
    """
    if level:
        base = list(Path(importer_rel).parent.parts)
        base = base[: len(base) - (level - 1)]
        parts = base + (module.split(".") if module else [])
    else:
        parts = module.split(".")
        if parts[0] not in ("odoo", "openerp"):
            parts = [*Path(importer_rel).parent.parts, *parts]  # py2 implicit relative
    stem = Path(*parts)
    for candidate in (stem.with_suffix(".py"), stem / "__init__.py"):
        if (root / candidate).is_file():
            return candidate.as_posix()
    return None


def _top_level_statements(tree: ast.Module) -> list[ast.stmt]:
    out: list[ast.stmt] = []
    for stmt in tree.body:
        out.append(stmt)
        if isinstance(stmt, ast.If):
            out.extend([*stmt.body, *stmt.orelse])
    return out


_RE_FROM_IMPORT_LINE = re.compile(r"^from\s+([.\w]+)\s+import\s+(.+)$", re.MULTILINE)


def _locate(root: Path, rel: str, name: str, depth: int = 0) -> _Site | None:
    """Definition site of `name`, starting at file `rel` and following
    `from X import name` re-exports into the checkout (AST when the file
    parses, a line-regex over `from ... import` when it is Python 2 syntax).
    """
    path = root / rel
    if depth > 6 or not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="ignore")
    try:
        tree: ast.Module | None = parse_external_source(text, filename=rel)  # type: ignore[assignment]
    except SyntaxError:
        tree = None
    if tree is not None:
        for stmt in reversed(_top_level_statements(tree)):
            if isinstance(stmt, (ast.FunctionDef, ast.ClassDef)) and stmt.name == name:
                return _Site(rel, text, tree, stmt)
            if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in stmt.targets
            ):
                return _Site(rel, text, tree, stmt)
        for stmt in _top_level_statements(tree):
            if isinstance(stmt, ast.ImportFrom) and any(
                (a.asname or a.name) == name for a in stmt.names
            ):
                source = _module_file(root, stmt.module or "", rel, stmt.level)
                original = next(a.name for a in stmt.names if (a.asname or a.name) == name)
                found = _locate(root, source, original, depth + 1) if source else None
                if found:
                    return found
        for stmt in _top_level_statements(tree):
            if isinstance(stmt, ast.ImportFrom) and any(a.name == "*" for a in stmt.names):
                source = _module_file(root, stmt.module or "", rel, stmt.level)
                found = _locate(root, source, name, depth + 1) if source else None
                if found:
                    return found
        return None
    for module, imported in _RE_FROM_IMPORT_LINE.findall(text):
        if name in re.findall(r"\w+", imported):
            dots = len(module) - len(module.lstrip("."))
            source = _module_file(root, module.lstrip("."), rel, dots)
            found = _locate(root, source, name, depth + 1) if source else None
            if found:
                return found
    defined = rf"^[ \t]*(?:def|class)\s+{re.escape(name)}\b|^{re.escape(name)}\s*="
    if re.search(defined, text, re.MULTILINE):
        return _Site(rel, text, None, None)
    return None


def _real_spec(root: Path, parsed, name: str, leaf: str) -> _ParamSpec | None:
    """Parameter spec the source gives for `name`: the oracle's own signature,
    else the definition site (function arguments, or a class's `__init__`
    without `self`).
    """
    real = parsed[name]
    if real.signature and real.signature != "module":
        return _signature_spec(real.signature)
    site = _locate(root, real.file_path, leaf)
    node = site.node if site else None
    if isinstance(node, ast.FunctionDef):
        return _spec_of_args(node.args)
    if isinstance(node, ast.ClassDef):
        init = next(
            (s for s in node.body if isinstance(s, ast.FunctionDef) and s.name == "__init__"),
            None,
        )
        return _spec_of_args(init.args)[1:] if init else None
    return None


def _parsed_and_entries(major: int):
    root = checkout_root(major)
    if root is None:
        pytest.skip(f"Odoo {major} checkout not found (set OSM_ODOO_CHECKOUTS)")
    version = f"{major}.0"
    parsed = _import_tools_symbols_oracle()(root, version)
    assert parsed is not None
    entries = _load_static_tools_symbols(version, static_data_dir=_SPEC_DATA_DIR)
    return root, parsed, entries


def _leaf(entry) -> tuple[str, str]:
    name = entry.qualified_name.removeprefix("odoo.tools.")
    return name, name.rsplit(".", 1)[-1]


@pytest.mark.parametrize("major", SURVEYED_MAJORS)
def test_every_curated_signature_has_a_form_the_source_checks_can_read(major):
    """Business rule: no curated signature is free text. Each one is a
    function signature, `class X`, `module`, an `X = Y` alias, a `str = <value>`
    constant or a `odoo.tools.<module>.<class>` instance - the forms the source
    checks of this file read back - so a new entry cannot slip past them.
    """
    entries = _load_static_tools_symbols(f"{major}.0", static_data_dir=_SPEC_DATA_DIR)
    assert entries, f"no curated entries for v{major}"
    free_text = [e.qualified_name for e in entries if _signature_kind(e.signature) == "other"]
    assert not free_text, f"signatures the source checks cannot read: {free_text}"
    for entry in entries:
        if _signature_kind(entry.signature) == "function":
            assert _signature_spec(entry.signature), entry.qualified_name


@pytest.mark.odoo_source
@pytest.mark.parametrize("major", SURVEYED_MAJORS)
def test_curated_function_signatures_carry_the_real_parameters_and_defaults(major):
    """Business rule: a curated `name(params)` signature lists exactly the
    parameters the live source takes - names, order, kind and default values -
    at every surveyed major. AI clients copy these signatures into call sites,
    so a missing parameter or a stale default reaches generated code directly.
    Annotations and the `-> ret` hint are not part of the parameter spec (the
    return hint is checked by its own test).
    """
    root, parsed, entries = _parsed_and_entries(major)
    checked, unresolved, mismatches = 0, [], []
    for entry in entries:
        if _signature_kind(entry.signature) != "function":
            continue
        name, leaf = _leaf(entry)
        if name not in parsed:
            continue  # absence is reported by the recoverability test
        real = _real_spec(root, parsed, name, leaf)
        if real is None:
            unresolved.append(entry.qualified_name)
            continue
        checked += 1
        curated = _signature_spec(entry.signature)
        if curated != real:
            mismatches.append((entry.qualified_name, curated, real))
    assert not unresolved, f"v{major}: no source signature found for {unresolved}"
    assert checked >= 8, f"parameter check ran on only {checked} signatures - oracle drift?"
    assert not mismatches, (
        f"v{major}: curated signature parameters differ from source: {mismatches}"
    )


def _annotation_name(node: ast.expr | None) -> str | None:
    """Last dotted segment of a plain-name return annotation (`str`,
    `markupsafe.Markup` -> `Markup`); None for unions, `Literal[...]`,
    subscripts and unannotated functions.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


@pytest.mark.odoo_source
@pytest.mark.parametrize("major", SURVEYED_MAJORS)
def test_curated_return_hints_name_the_type_the_source_annotates(major):
    """Business rule: where the live function annotates its return with a
    plain type name, the curated `-> hint` names that same type. Sources before
    v18 carry no annotations and later ones use unions and `Literal[...]`
    that a short hint cannot restate, so those returns are not checked
    mechanically; the older hints are limited to what the function body shows.
    """
    root, parsed, entries = _parsed_and_entries(major)
    checked, mismatches = 0, []
    for entry in entries:
        if _signature_kind(entry.signature) != "function":
            continue
        name, leaf = _leaf(entry)
        real = parsed.get(name)
        site = _locate(root, real.file_path, leaf) if real else None
        node = site.node if site else None
        annotated = _annotation_name(node.returns) if isinstance(node, ast.FunctionDef) else None
        if annotated is None:
            continue
        checked += 1
        hint = _return_hint(entry.signature)
        if hint is None or hint.rsplit(".", 1)[-1] != annotated:
            mismatches.append((entry.qualified_name, hint, annotated))
    assert not mismatches, (
        f"v{major}: curated return hint differs from source annotation: {mismatches}"
    )
    if major >= 18:
        assert checked >= 3, f"return check ran on only {checked} functions - oracle drift?"


@pytest.mark.odoo_source
@pytest.mark.parametrize("major", SURVEYED_MAJORS)
def test_curated_aliases_constants_and_instances_match_their_source_binding(major):
    """Business rule: a curated `X = Y` alias, `str = '<value>'` constant or
    `odoo.tools.<module>.<class>` instance signature restates the real
    module-level binding, and an alias's replacement is the aliased object
    itself. html_escape is the alias case: from v15 it is the statement
    `html_escape = markupsafe.escape`, which has no signature of its own, so
    the curated entry carries that statement and the test reads it back.
    """
    root, parsed, entries = _parsed_and_entries(major)
    checked, problems = 0, []
    for entry in entries:
        kind = _signature_kind(entry.signature)
        if kind not in ("alias", "constant", "instance"):
            continue
        name, leaf = _leaf(entry)
        real = parsed.get(name)
        if real is None:
            continue
        checked += 1
        if kind == "instance":
            module, cls = _RE_INSTANCE_SIGNATURE.match(entry.signature).groups()
            text = (root / real.file_path).read_text(encoding="utf-8", errors="ignore")
            if not (
                re.search(rf"^class {cls}\b", text, re.MULTILINE)
                and re.search(rf"^{leaf}\s*=\s*{cls}\(", text, re.MULTILINE)
            ):
                problems.append(
                    (entry.qualified_name, f"no `{leaf} = {cls}(...)` in {real.file_path}")
                )
            continue
        site = _locate(root, real.file_path, leaf)
        node = site.node if site else None
        if not isinstance(node, ast.Assign):
            problems.append((entry.qualified_name, f"no module-level assignment of {leaf}"))
            continue
        if kind == "alias":
            lhs, rhs = _RE_ALIAS_SIGNATURE.match(entry.signature).groups()
            if lhs != leaf or ast.unparse(node.value) != rhs:
                problems.append(
                    (entry.qualified_name, f"source binds {leaf} = {ast.unparse(node.value)}")
                )
            if entry.replacement_qname not in (None, rhs):
                problems.append((
                    entry.qualified_name,
                    f"replacement {entry.replacement_qname!r} is not the alias target {rhs!r}",
                ))
        else:
            curated = ast.literal_eval(_RE_CONSTANT_SIGNATURE.match(entry.signature).group(1))
            if _evaluate(node.value, site.tree) != curated:
                problems.append(
                    (entry.qualified_name, f"source value is {ast.unparse(node.value)}")
                )
    assert checked >= 3, f"alias/constant check ran on only {checked} entries - oracle drift?"
    assert not problems, f"v{major}: curated bindings differ from source: {problems}"


def _evaluate(node: ast.expr, tree: ast.Module):
    """Value of a constant expression built from literals, `%` formatting and
    names assigned earlier in the same module.
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Tuple):
        return tuple(_evaluate(e, tree) for e in node.elts)
    if isinstance(node, ast.Name):
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == node.id for t in stmt.targets
            ):
                return _evaluate(stmt.value, tree)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _evaluate(node.left, tree) % _evaluate(node.right, tree)
    raise AssertionError(f"constant not evaluable: {ast.unparse(node)}")


@pytest.mark.odoo_source
@pytest.mark.parametrize("major", SURVEYED_MAJORS)
def test_curated_class_entries_are_class_definitions_in_source(major):
    """Business rule: a curated `class X` entry names a real class definition
    at that major. A name the module-level `__getattr__` of odoo/tools/__init__.py
    serves (ormcache at v20) is defined elsewhere and is covered by
    test_ormcache_is_a_deprecated_alias_served_by_module_getattr_from_v20.
    """
    root, parsed, entries = _parsed_and_entries(major)
    problems = []
    for entry in entries:
        if _signature_kind(entry.signature) != "class":
            continue
        name, leaf = _leaf(entry)
        real = parsed.get(name)
        if real is None or real.file_path.endswith("tools/__init__.py"):
            continue
        site = _locate(root, real.file_path, leaf)
        if site is None or not isinstance(site.node, ast.ClassDef):
            problems.append(entry.qualified_name)
        elif entry.signature != f"class {site.node.name}":
            problems.append(entry.qualified_name)
    assert not problems, f"v{major}: curated class entries without a class definition: {problems}"


_RE_DEPRECATION = re.compile(r"DeprecationWarning|@deprecated\b")

def _marks_deprecation(node: ast.AST) -> bool:
    """Whether a function (anywhere in its body or decorators) or a class
    (decorators and `__init__`/`__new__` only - a deprecated method does not
    deprecate its class) raises a DeprecationWarning or carries `@deprecated`.
    """
    if isinstance(node, ast.ClassDef):
        parts = [*node.decorator_list] + [
            s for s in node.body
            if isinstance(s, ast.FunctionDef) and s.name in ("__init__", "__new__")
        ]
    else:
        parts = [node]
    return any(_RE_DEPRECATION.search(ast.unparse(part)) for part in parts)


@pytest.mark.odoo_source
@pytest.mark.parametrize("major", SURVEYED_MAJORS)
def test_curated_deprecated_status_agrees_with_source_markers(major):
    """Business rule: an entry is 'deprecated' exactly when the live source
    marks it so - a function or class that raises a DeprecationWarning, a module
    every public function of which does, or a name the module `__getattr__`
    serves with one - and never the other way round: a name whose source warns
    is not left 'stable' (ustr from v18, pycompat from v18). There are no
    exemptions: html_escape is the stable alias `html_escape = markupsafe.escape`
    at every major from v15 because nothing in source discourages it.
    """
    root, parsed, entries = _parsed_and_entries(major)
    problems, checked = [], 0
    for entry in entries:
        kind = _signature_kind(entry.signature)
        name, leaf = _leaf(entry)
        real = parsed.get(name)
        if real is None:
            continue
        if real.file_path.endswith("tools/__init__.py"):
            init_text = (root / real.file_path).read_text(encoding="utf-8")
            marked = bool(_RE_DEPRECATION.search(init_text))
        elif kind in ("function", "class"):
            site = _locate(root, real.file_path, leaf)
            marked = bool(site and site.node is not None and _marks_deprecation(site.node))
            if site is not None and site.node is None and kind == "function":
                body = re.search(
                    rf"^[ \t]*def {re.escape(leaf)}\(.*?(?=^\S)",
                    site.text,
                    re.MULTILINE | re.DOTALL,
                )
                marked = bool(body and _RE_DEPRECATION.search(body.group(0)))
        elif kind == "module":
            text = (root / real.file_path).read_text(encoding="utf-8", errors="ignore")
            try:
                tree = parse_external_source(text, filename=real.file_path)
            except SyntaxError:
                continue
            functions = [
                s for s in tree.body
                if isinstance(s, ast.FunctionDef) and not s.name.startswith("_")
            ]
            marked = bool(functions) and all(_marks_deprecation(f) for f in functions)
        else:
            marked = False
        checked += 1
        if entry.status == "deprecated":
            if not marked:
                problems.append((entry.qualified_name, "deprecated without a source marker"))
        elif marked:
            problems.append((
                entry.qualified_name,
                f"source marks it deprecated but status is {entry.status!r}",
            ))
    assert checked >= 8, f"deprecation check ran on only {checked} entries - oracle drift?"
    assert not problems, f"v{major}: {problems}"


@pytest.mark.odoo_source
@pytest.mark.parametrize("major", _majors(11, 19))
def test_pycompat_is_curated_wherever_the_module_resolves(major):
    """Business rule: `from odoo.tools import pycompat` resolves at every
    major from 11 to 19 (odoo/tools/pycompat.py exists, so the submodule
    fallback serves it even when odoo/tools/__init__.py stops importing it at
    v19), so the curated file carries the entry; v20 has no pycompat.py and
    omits it.
    """
    root, parsed, entries = _parsed_and_entries(major)
    assert (_tools_dir(root, major) / "pycompat.py").is_file(), (
        f"ground-truth check itself is broken: v{major} has no pycompat.py"
    )
    entry = next((e for e in entries if e.qualified_name == "odoo.tools.pycompat"), None)
    assert entry is not None, (
        f"tools_symbols_{major}.0.json omits odoo.tools.pycompat, which resolves at v{major}"
    )
