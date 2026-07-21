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


def _image_process_qname_for(version: str) -> str:
    """Which curated qualified_name carries image_process's signature truth
    at `version`.

    Every version except 19.0 keys off the flat `odoo.tools.image_process`
    re-export. v19 intentionally OMITS that flat entry (issue #364 Problem 2
    - the re-export was dropped from odoo/tools/__init__.py at v19, so the
    flat name no longer resolves there; see
    test_image_process_qualified_name_no_longer_resolves_at_v19 for the full
    ground-truth argument). v19's signature truth therefore lives under the
    replacement path, odoo.tools.image.image_process, instead - tests that
    inspect the curated signature must follow that move rather than looking
    up a qualified_name the data no longer carries.
    """
    return "odoo.tools.image.image_process" if version == "19.0" else "odoo.tools.image_process"


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
@pytest.mark.parametrize(
    "version", ["13.0", "14.0", "15.0", "16.0", "17.0", "18.0", "19.0"],
)
def test_image_process_signature_is_frozen_copy_paste_never_matching_real_params(version):
    """Business rule: image_process's curated `signature` string must match
    the real parameter list at each version - historically it was a single
    copy-pasted string across all versions (phase2-C audit section 3 item
    2a: real first positional parameter is 'base64_source' at v13-v15, then
    'source' from v16 on). v19 looks up the entry under
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
@pytest.mark.parametrize("version", ["18.0", "19.0"])
def test_image_process_v18_v19_curated_signature_missing_expand_and_padding(version):
    """Business rule: real image_process() gained `expand` and `padding`
    parameters at v18 - the curated signature must carry them from v18 on.
    v19 looks up the entry under odoo.tools.image.image_process rather than
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
def test_image_process_qualified_name_no_longer_resolves_at_v19():
    """Business rule: odoo.tools.image_process was re-exported via `from
    .image import image_process` in odoo/tools/__init__.py through v18; that
    re-export was dropped at v19 (no 'image' reference anywhere in v19's
    odoo/tools/__init__.py) - the curated data must reflect that the flat
    qualified_name no longer resolves as a real import path there.

    NOTE (issue #364 follow-up): the original version of this assertion
    (`assert real_still_reexports`) was inverted relative to this docstring
    and its own failure message - it demanded the real v19 checkout still
    re-export image_process, which is a fact about upstream source no
    curated-data edit could ever change, and which is also simply false
    (confirmed against /home/tuan/git/odoo19/odoo/tools/__init__.py). Fixed
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
    root = checkout_root(19)
    if root is None:
        pytest.skip("Odoo 19 checkout not found (set OSM_ODOO_CHECKOUTS)")
    init_py = root / "odoo" / "tools" / "__init__.py"
    assert init_py.is_file()
    real_still_reexports = "image_process" in init_py.read_text(encoding="utf-8", errors="ignore")
    assert not real_still_reexports, (
        "ground-truth check itself is broken (or v19 has changed upstream): "
        "expected v19's odoo/tools/__init__.py to no longer mention "
        "image_process at all - if this fires, re-derive the curated-data "
        "decision below from source, do not just flip this assertion back"
    )

    flat_entry = _curated_entry("19.0", "odoo.tools.image_process")
    assert flat_entry is None, (
        "tools_symbols_19.0.json still carries a flat odoo.tools.image_process "
        f"entry (status={flat_entry.status if flat_entry else None!r}), but "
        "v19's odoo/tools/__init__.py no longer re-exports it - per the "
        "schema's 'removed = absent, omit it' convention (already applied to "
        "pycompat in this same file), this qualified_name must be omitted "
        "entirely, not kept as 'deprecated'"
    )

    replacement_entry = _curated_entry("19.0", "odoo.tools.image.image_process")
    assert replacement_entry is not None and replacement_entry.status == "stable", (
        "tools_symbols_19.0.json must carry a 'stable' "
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


@pytest.mark.odoo_source
@pytest.mark.parametrize("major", SURVEYED_MAJORS)
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
