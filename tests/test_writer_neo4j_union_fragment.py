# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_writer_neo4j_union_fragment.py
"""Pure unit tests for _profile_union_set helper (NO pytest marker — no DB required).

Verifies:
1. Helper output identity for representative aliases.
2. Alias interpolation — each alias appears inside coalesce(<alias>.profile, []).
3. DRIFT GUARD — the literal fragment `WHERE NOT x IN $profiles] + $profiles`
   appears AT MOST ONCE in writer_neo4j.py source (only inside the helper body),
   proving all 16 call-sites were migrated and preventing future drift.
"""

import pathlib

import pytest

from src.indexer.writer_neo4j import _profile_union_set

# ---------------------------------------------------------------------------
# 1. Helper output identity
# ---------------------------------------------------------------------------

def test_helper_alias_m():
    assert _profile_union_set("m") == (
        "[x IN coalesce(m.profile, []) WHERE NOT x IN $profiles] + $profiles"
    )


def test_helper_alias_mod():
    assert _profile_union_set("mod") == (
        "[x IN coalesce(mod.profile, []) WHERE NOT x IN $profiles] + $profiles"
    )


def test_helper_alias_f():
    assert _profile_union_set("f") == (
        "[x IN coalesce(f.profile, []) WHERE NOT x IN $profiles] + $profiles"
    )


def test_helper_alias_ss():
    assert _profile_union_set("ss") == (
        "[x IN coalesce(ss.profile, []) WHERE NOT x IN $profiles] + $profiles"
    )


def test_helper_alias_lv():
    assert _profile_union_set("lv") == (
        "[x IN coalesce(lv.profile, []) WHERE NOT x IN $profiles] + $profiles"
    )


# ---------------------------------------------------------------------------
# 2. Parametrized alias interpolation — all 10 distinct aliases used in file
# ---------------------------------------------------------------------------

ALL_ALIASES = ["m", "mod", "f", "mth", "v", "t", "c", "j", "ss", "lv"]


@pytest.mark.parametrize("alias", ALL_ALIASES)
def test_alias_appears_in_coalesce(alias: str):
    """The alias must appear inside coalesce(<alias>.profile, []) in the fragment."""
    fragment = _profile_union_set(alias)
    assert f"coalesce({alias}.profile, [])" in fragment, (
        f"alias {alias!r} not found inside coalesce(...) in: {fragment!r}"
    )


@pytest.mark.parametrize("alias", ALL_ALIASES)
def test_fragment_structure(alias: str):
    """Fragment must follow the canonical union-set pattern."""
    fragment = _profile_union_set(alias)
    # Must start with '[x IN coalesce(...)' and end with '+ $profiles'
    assert fragment.startswith("[x IN coalesce("), (
        f"Fragment for {alias!r} does not start with '[x IN coalesce(': {fragment!r}"
    )
    assert fragment.endswith("] + $profiles"), (
        f"Fragment for {alias!r} does not end with '] + $profiles': {fragment!r}"
    )
    # The $profiles token is a Cypher param literal, NOT an empty string
    assert "$profiles" in fragment


# ---------------------------------------------------------------------------
# 3. DRIFT GUARD — verify all 16 call-sites were migrated
#    The raw fragment suffix must appear AT MOST ONCE in the source file
#    (only inside the helper's return statement body).
# ---------------------------------------------------------------------------

def test_no_residual_literal_fragments():
    """All 16 inline occurrences must have been replaced by _profile_union_set calls.

    Counts occurrences of the canonical raw suffix in NON-comment, NON-docstring
    lines of writer_neo4j.py source.  EXACTLY 1 is expected: the ``return``
    statement inside the helper itself.  Any count > 1 means a call-site was
    missed (drift); a count of 0 means the helper body itself was removed/renamed
    — both are failures, so the assertion is two-sided (== 1, not <= 1).

    The helper's docstring also contains the suffix (as documentation), so we
    strip lines that start with ``#`` or are triple-quoted docstring content
    (lines whose stripped form starts with the docstring marker ``[x IN coalesce``
    with the ``<alias>`` placeholder) before counting.

    B5 split note: the helper's SSOT ``return`` lives in ``writer_neo4j.py``; the
    node-grouped siblings (``writer_neo4j_orm/ui/spec.py``) must hold ZERO raw
    fragments (every site routes through the helper). The guard therefore scans
    the whole writer module family and expects EXACTLY 1 raw code-line total —
    the helper's own return — across all of them.
    """
    indexer_dir = pathlib.Path(__file__).parent.parent / "src" / "indexer"
    writer_files = sorted(indexer_dir.glob("writer_neo4j*.py"))
    assert writer_files, f"no writer_neo4j*.py files found under {indexer_dir}"

    raw_suffix = "WHERE NOT x IN $profiles] + $profiles"

    # Filter out lines that are documentation (contain '<alias>' placeholder)
    # so we only count code lines.
    code_lines = [
        f"{path.name}: {line}"
        for path in writer_files
        for line in path.read_text(encoding="utf-8").splitlines()
        if raw_suffix in line and "<alias>" not in line
    ]
    count = len(code_lines)
    assert count == 1, (
        f"Found {count} code-line occurrences of the raw union-fragment suffix "
        f"across the writer_neo4j module family (after excluding docstring "
        f"placeholder lines). Expected EXACTLY 1 (only in the helper's return "
        f"statement in writer_neo4j.py). "
        f"count==0 → the helper body was removed/renamed; count>1 → "
        f"{count - 1} call-site(s) were not migrated. "
        f"Offending lines:\n" + "\n".join(code_lines)
    )


def test_all_call_sites_use_helper():
    """Two-sided drift guard: exactly 16 ``_profile_union_set(...)`` call-sites.

    The literal-fragment guard above proves no raw fragment was *left behind*;
    this proves the migration was *complete and stayed complete* — that all 16
    union-set sites route through the helper. If a future edit drops a call-site
    (e.g. swaps one for a specialised inline variant) without reinstating the raw
    fragment, the count below catches it even though the literal guard would not.

    B5 split note: the write functions that hold these call-sites were extracted
    from ``writer_neo4j.py`` into the node-grouped siblings
    ``writer_neo4j_orm.py`` / ``writer_neo4j_ui.py`` / ``writer_neo4j_spec.py``
    (the ``_profile_union_set`` helper itself stays the SSOT in ``writer_neo4j``).
    The behavioural invariant is unchanged — exactly 16 union-set sites all route
    through the helper — so this guard now sums the count across the whole writer
    module family rather than a single file.
    """
    indexer_dir = pathlib.Path(__file__).parent.parent / "src" / "indexer"
    writer_files = sorted(indexer_dir.glob("writer_neo4j*.py"))
    assert writer_files, f"no writer_neo4j*.py files found under {indexer_dir}"

    per_file = {
        path.name: path.read_text(encoding="utf-8").count("{_profile_union_set(")
        for path in writer_files
    }
    call_sites = sum(per_file.values())
    assert call_sites == 16, (
        f"Expected exactly 16 '{{_profile_union_set(' call-sites across the "
        f"writer_neo4j module family, found {call_sites} (per-file: {per_file}). "
        f"A change in call-site count means a union-set site was added or removed — "
        f"re-verify the writer and update this expectation only if the change is "
        f"intentional."
    )
