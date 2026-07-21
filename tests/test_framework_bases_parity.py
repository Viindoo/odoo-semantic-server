# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drift alarm for src/indexer/framework_bases.py (issue #362).

WHY THIS FILE EXISTS
---------------------
`framework_bases.py`'s per-era menu (which classes exist, their file_path,
has_setUpClass, deprecation status) is a CURATED table, hand-verified once against
real Odoo source at review time (api-contract.md "Authoritative menus per era"). A
curated table's failure mode is never that it is wrong on day one - review catches
that. Its failure mode is DRIFT: it silently stops matching source as new Odoo
majors ship. That is the literal root cause of issue #362 - `_FRAMEWORK_BASES`
(the flat, version-blind dict this feature replaces) was correct when written and
was still being served as the answer five majors and six years later, with nothing
in the repo able to notice.

This file is the mechanical detector for that failure mode. It compares the
curated table (`framework_bases()`, no source root - the curated/default path) to
an INDEPENDENT oracle - a real AST parse of Odoo source (`parse_framework_bases()`)
- and asserts they agree on the facts a parse can verify. ADR-0054 SS3 describes the
curated table as "a cached projection of source with a drift alarm, not an
independent belief" - that claim is only true for as long as this file can
actually go RED on a real disagreement.

IF THIS FILE IS EVER MADE TO SKIP EVERYWHERE, THE CURATED TABLE LOSES ITS ENTIRE
JUSTIFICATION. Do not "fix" a failing layer here by turning it into a skip; fix
the curated table, the parser, or the fixture - whichever is actually wrong.

TWO LAYERS
----------
1. CI layer (`test_curated_table_matches_parsed_fixture_per_version` et al.) - runs
   everywhere, every time, including GitHub-hosted CI with zero Odoo checkouts on
   disk (see phase5-review.md W5 - the exact gap this layer exists to close: a
   checkout-only parity test never runs anywhere but a developer's laptop). It
   diffs the curated table against the committed, AST-faithful excerpts under
   `tests/fixtures/odoo_tests_headers/` (`v<major>_common.py` [+ `_form.py` for
   v17+] - see that directory's README.md for provenance and extraction method).
   This layer must NEVER skip: a missing fixture is a hard failure here, not a
   silent pass, because a layer that can go green with nothing to compare is not
   an alarm, it is theater.
2. Dev-box layer (`test_curated_table_matches_real_checkout_per_version`) - the
   stronger, un-curated version of the same comparison, run against a real
   `/home/tuan/git/odoo<N>` checkout when one is present on this machine (parent
   dir overridable via `OSM_ODOO_CHECKOUTS` for portability, ETHOS #11). Marked
   `@pytest.mark.odoo_source`; skips per version when the checkout is absent - that
   is expected and fine on CI, which is exactly why the CI layer above exists and
   is NOT allowed to skip.

FIXTURE-TO-SOURCE-ROOT ADAPTER (read this before touching WI-4/WI-6)
----------------------------------------------------------------------
`parse_framework_bases(odoo_source_root, odoo_version)` takes a checkout ROOT and
internally resolves `<root>/<era-prefix>/tests/common.py` (+ `form.py` from v17+) -
the exact contract real callers (`index_core`) use. The committed fixtures under
`odoo_tests_headers/` are FLAT files (`v17_common.py`, not `v17/odoo/tests/
common.py`), so `_materialize_source_root()` below copies each flat fixture into a
`tmp_path`-backed synthetic checkout root at the exact era-prefixed path the parser
resolves, then calls the real, unmodified public `parse_framework_bases` contract
against it. This is deliberate: it exercises the real era-prefix-resolution logic
end to end (not just the AST walk), and it requires ZERO test-only calling
convention in `framework_bases.py` - the same principle ADR-0054 SS5 applies to the
`99.0` sentinel ("no test sentinel is special-cased in production code"). WI-4/WI-6
must build `parse_framework_bases` to the source-root contract in api-contract.md;
do not add a "hand me a bare file" path to accommodate this test file.

STDLIB TestCase CARVE-OUT
--------------------------
`TestCase` is python's stdlib `unittest.TestCase`. api-contract.md's "file_path per
era" table gives it `file_path=None` at every version - it is never bound as a name
inside `odoo/tests/common.py` at any surveyed version (phase4-solution.md §2.4), so
it never appears in any fixture file and is never present in
`parse_framework_bases()`'s returned dict. The parity comparison below therefore
excludes curated entries with `file_path is None` BEFORE diffing against the parse.
This exclusion is explicit (`_curated_comparable_set`), and is itself guarded by
`test_the_only_curated_entry_without_a_file_path_is_testcase` so a future curated-
table edit cannot silently grow a second, unnoticed carve-out. The comparison is
never weakened to a subset check - an explicit, asserted carve-out is not the same
as loosening the alarm.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "odoo_tests_headers"

# Every major this feature surveys (api-contract.md "Authoritative menus per era",
# E1 through E7 span 8..19 inclusive).
SURVEYED_MAJORS = list(range(8, 20))

# Real dev-box checkouts live at <parent>/odoo<major>. Default parent matches the
# convention this project's own design docs use (phase4-solution.md §8, T7 row:
# "/home/tuan/git/odoo<N> (or $OSM_ODOO_CHECKOUTS)"). The env override means this
# file makes no hardcoded assumption on a machine without that exact layout - see
# ETHOS #11 (portable): absence is handled by a per-version skip, never a failure.
_CHECKOUTS_PARENT = Path(os.environ.get("OSM_ODOO_CHECKOUTS", "/home/tuan/git"))


def _import_framework_bases():
    """Deferred import of the not-yet-built SSOT module (issue #362, WI-4/WI-6).

    Imported inside each test function - never at module level - so
    `pytest --collect-only` on this file succeeds cleanly today even though the
    module does not exist; the ImportError surfaces only when a test runs, which
    is the RED proof this commit is meant to record (a module-level import here
    would instead produce a collection error and hide every test in this file).
    """
    from src.indexer import framework_bases
    return framework_bases


def _era_prefix(major: int) -> str:
    """Mirror the era-prefix rule api-contract.md states for parse_framework_bases:
    'openerp/' for majors <= ODOO_NAMESPACE_LEGACY_MAX_MAJOR (9), 'odoo/'
    otherwise. Imported from src.constants - the same constant framework_bases.py
    itself is contracted to use - so this test helper mirrors the public constant
    rather than re-guessing the boundary.
    """
    from src.constants import ODOO_NAMESPACE_LEGACY_MAX_MAJOR
    return "openerp" if major <= ODOO_NAMESPACE_LEGACY_MAX_MAJOR else "odoo"


def _materialize_source_root(tmp_path: Path, major: int) -> Path | None:
    """Copy the flat committed fixture(s) for one major into a tmp_path-backed
    synthetic checkout root, at the exact era-prefixed path
    parse_framework_bases resolves (<root>/<prefix>/tests/common.py [+ form.py
    from v17]). Returns None when no fixture is committed for this major - the CI
    layer must treat that as a hard failure, never a skip (see module docstring).
    """
    common_src = FIXTURE_DIR / f"v{major}_common.py"
    if not common_src.is_file():
        return None
    prefix = _era_prefix(major)
    tests_dir = tmp_path / prefix / "tests"
    tests_dir.mkdir(parents=True)
    shutil.copyfile(common_src, tests_dir / "common.py")
    form_src = FIXTURE_DIR / f"v{major}_form.py"
    if form_src.is_file():
        shutil.copyfile(form_src, tests_dir / "form.py")
    return tmp_path


def _checkout_root(major: int) -> Path:
    """Resolve one real dev-box checkout root. See _CHECKOUTS_PARENT for the
    OSM_ODOO_CHECKOUTS override; absence is handled by a per-version pytest.skip
    in the dev-box layer, never a hard failure (unlike the CI layer)."""
    return _CHECKOUTS_PARENT / f"odoo{major}"


def _curated_comparable_set(fb, version: str) -> set[tuple[str, str, str, bool]]:
    """The curated side of the parity comparison, with the stdlib TestCase
    carve-out applied (see module docstring)."""
    return {
        (f.name, f.status, f.file_path, f.has_setUpClass)
        for f in fb.framework_bases(version)
        if f.file_path is not None
    }


def _parsed_comparable_set(parsed: dict) -> set[tuple[str, str, str, bool]]:
    """The parsed (oracle) side of the parity comparison - same tuple shape as
    _curated_comparable_set so the two sets are directly diffable."""
    return {
        (p.name, "deprecated" if p.is_deprecated else "available", p.file_path, p.has_setUpClass)
        for p in parsed.values()
    }


# ---------------------------------------------------------------------------
# Layer 1 - CI (must NEVER skip; see module docstring).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("major", SURVEYED_MAJORS)
def test_curated_table_matches_parsed_fixture_per_version(major, tmp_path):
    """T7 CI layer - business rule: for every surveyed major, the curated era
    table's {name, status, file_path, has_setUpClass} facts (excluding the stdlib
    TestCase carve-out) equal what an independent AST parse of the committed
    source excerpt derives. A mismatch means either the curated table drifted from
    source (issue #362's exact failure mode) or a fixture/table edit introduced a
    typo - either way it must be visible here, never skipped.
    """
    fb = _import_framework_bases()
    version = f"{major}.0"
    root = _materialize_source_root(tmp_path, major)
    assert root is not None, (
        f"no committed fixture v{major}_common.py under {FIXTURE_DIR} - the CI "
        "layer must fail loudly on a missing fixture, never silently skip "
        "(see module docstring)"
    )
    parsed = fb.parse_framework_bases(root, version)
    assert parsed is not None, f"parse_framework_bases returned None for the v{major} fixture"
    assert _curated_comparable_set(fb, version) == _parsed_comparable_set(parsed)


def test_ci_layer_covers_every_surveyed_major_with_no_silent_gaps():
    """T7 CI layer guard - business rule: this file's parametrization must
    actually exercise the full [8, 19] surveyed range. A discovery bug that
    silently narrows SURVEYED_MAJORS, or a directory move that orphans the
    fixtures, must fail here rather than quietly reducing the alarm's coverage.
    """
    assert SURVEYED_MAJORS == list(range(8, 20))
    fixture_majors = {
        int(p.stem.split("_")[0][1:])
        for p in FIXTURE_DIR.glob("v*_common.py")
    }
    assert fixture_majors == set(SURVEYED_MAJORS), (
        f"expected a v<major>_common.py fixture for every major in {SURVEYED_MAJORS}, "
        f"found fixtures for {sorted(fixture_majors)}"
    )


def test_the_only_curated_entry_without_a_file_path_is_testcase():
    """T7 carve-out guard - business rule: TestCase (stdlib) is the ONLY curated
    entry the parity comparison is allowed to exclude via `file_path is None`. If
    a future era-table edit adds a second null-file_path entry, this must fail so
    `_curated_comparable_set`'s exclusion cannot silently swallow a real drift.
    """
    fb = _import_framework_bases()
    for major in SURVEYED_MAJORS:
        version = f"{major}.0"
        null_path_names = {f.name for f in fb.framework_bases(version) if f.file_path is None}
        assert null_path_names == {"TestCase"}, (
            f"{version}: expected only TestCase to have file_path=None, got {null_path_names}"
        )


# ---------------------------------------------------------------------------
# Layer 2 - dev-box (real checkouts; skips per version when absent, never fails).
# ---------------------------------------------------------------------------

@pytest.mark.odoo_source
@pytest.mark.parametrize("major", SURVEYED_MAJORS)
def test_curated_table_matches_real_checkout_per_version(major):
    """T7 dev-box layer - business rule: the same parity comparison as the CI
    layer, but against the actual, un-curated Odoo source tree rather than a
    committed excerpt - the stronger of the two checks, since it also validates
    that the fixtures themselves stayed faithful to real source. Skips (never
    fails) when the checkout is not present on this machine.
    """
    fb = _import_framework_bases()
    version = f"{major}.0"
    root = _checkout_root(major)
    if not root.is_dir():
        pytest.skip(
            f"Odoo {major} checkout not found at {root} (set OSM_ODOO_CHECKOUTS to override)"
        )
    parsed = fb.parse_framework_bases(root, version)
    assert parsed is not None, f"parse_framework_bases returned None for real checkout {root}"
    assert _curated_comparable_set(fb, version) == _parsed_comparable_set(parsed)
