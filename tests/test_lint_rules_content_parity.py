# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_lint_rules_content_parity.py
"""Content-parity tests for lint_rules curated data (issue #364, phase2-B audit).

WHY THIS FILE EXISTS
---------------------
`tests/test_spec_data_lint_rules_curated.py` proves schema validity and internal
self-consistency (unique rule_id, code_pattern compiles, no ReDoS shape, one
rule_id never carries two different patterns). It does NOT compare a single
`message` string against real Odoo source, real pylint-odoo, real eslint, or real
ruff - a curated JSON could ship a completely fabricated `message` for every rule
and that file would stay green (phase2-B audit section 4). This file closes that
gap for the part of the family where a source oracle actually applies.

THE SPLIT THAT MATTERS (phase2-B audit section 1.2/2)
-------------------------------------------------------
~84% of curated rules are EDITORIAL: timeless coding-convention opinions (SQL
injection risk, missing `super()` call, N+1 query) with no version-specific
truth-value - equally "good practice" at v8 or v19. A source-diff oracle is the
WRONG instrument for that bucket; there is nothing about a checkout that could
falsify "don't string-interpolate SQL". This file does NOT attempt to build one
for the editorial bucket.

~16% are FALSIFIABLE: the `message` asserts a specific claim about Odoo's own
source at a named version boundary ("removed in vN", "deprecated in vN",
"renamed to X in vN"). THAT is a real, checkable claim, and the audit found two
independently-confirmed defects in exactly the #362 failure shape (a version's
own file makes a false claim about that same version's real source):

  - W8202 (`name_get()`): v17.0's own file says "removed in v17" - real v17
    source (`odoo/models.py`) still DEFINES `name_get()` (a `.. deprecated::
    17.0` docstring + `warnings.warn(..., DeprecationWarning)` - deprecated,
    not removed). True removal is v18. v19.0's file says "removed in v17+",
    contradicting v18.0's own (correct) "removed in v18" entry.
  - W8167 (`track_visibility` -> `tracking`): claimed "renamed ... in v16[+]"
    at every version file it appears in (16.0-19.0); real usage-count evidence
    (grep across each version's addons tree) shows the rename already happened
    at the v12->v13 boundary - v12 is track_visibility-majority (122 files vs
    28), v13 is already tracking-majority (27 vs 77) and the gap only widens
    from there. The claimed boundary is off by THREE major versions.

THIS FILE'S TWO LAYERS
------------------------
1. Classification pin (`_BOUNDARY_CLAIM_RE` + `_KNOWN_FALSIFIABLE_RULE_IDS`) -
   CI-safe, no checkout needed, never skips. A mechanical regex sweep (the same
   method the audit used, section 1.2) over the three versions the live
   pylint-odoo/eslint/ruff extractor can actually reach today (v17.0-v19.0;
   `LINT_RULES_MIN_MAJOR` is NOT widened here - that is separate work, phase2-B
   section 4 "v14-v16" bullet) flags every rule whose message carries
   version-boundary language. The flagged set is pinned against a reviewed
   snapshot: if a FUTURE edit adds a new boundary claim without updating the
   snapshot, the mismatch fails loudly - an unmarked boundary claim is exactly
   what escapes review (this file's whole reason to exist). This layer does NOT
   check whether a classified-falsifiable rule's CONTENT is true - only that it
   was correctly IDENTIFIED as needing scrutiny. Content correctness is layer 2.
2. Content-vs-real-source checks (`test_w8202_*` / `test_w8167_*`) - dev-box
   only (`@pytest.mark.odoo_source`), skips per version when the checkout is
   absent. Diffs a specific falsifiable claim against the real Odoo checkout it
   claims to describe, the same shape `test_framework_bases_parity.py`'s
   dev-box layer uses for framework_bases.py. Discovery goes through
   `tests/_odoo_checkouts.py` (issue #364 D2 SSOT) - no second discovery
   mechanism.

EXPECT RED (do not weaken these to reach green - see the top-level task brief):
  - test_w8202_v17_own_file_falsely_claims_removal_at_v17
  - test_w8202_v19_contradicts_v18_true_removal_boundary
  - test_w8202_v16_recommends_switch_one_version_before_any_deprecation_signal
  - test_w8167_claimed_v16_boundary_contradicts_real_v12_v13_transition[16.0]
  - test_w8167_claimed_v16_boundary_contradicts_real_v12_v13_transition[17.0]
  - test_w8167_claimed_v16_boundary_contradicts_real_v12_v13_transition[18.0]
  - test_w8167_claimed_v16_boundary_contradicts_real_v12_v13_transition[19.0]

A later commit corrects `spec_data/lint_rules_*.json`; these tests turn green
when it does. This file does not touch `src/` or `spec_data/*.json` itself.
"""
from __future__ import annotations

import json
import re
import subprocess
import warnings
from pathlib import Path

import pytest

from src.constants import LINT_RULES_MIN_MAJOR
from tests._odoo_checkouts import checkout_root

_SPEC_DATA_DIR = Path(__file__).parent.parent / "src" / "indexer" / "spec_data"

# Versions the live pylint-odoo/eslint/ruff extractor can reach today
# (LINT_RULES_MIN_MAJOR gate; NOT widened here - out of scope, see module
# docstring). Currently ["17.0", "18.0", "19.0"].
_LIVE_EXTRACTOR_VERSIONS = [f"{m}.0" for m in range(LINT_RULES_MIN_MAJOR, 20)]


def _load_rules(version: str) -> list[dict]:
    path = _SPEC_DATA_DIR / f"lint_rules_{version}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["rules"]


def _rule(version: str, rule_id: str) -> dict:
    for r in _load_rules(version):
        if r["rule_id"] == rule_id:
            return r
    raise AssertionError(f"{rule_id} not found in lint_rules_{version}.json")


def _grep_file_count(directory: Path, pattern: str) -> int:
    """Count of files under *directory* containing >=1 line matching *pattern*
    (basic extended regex). Shells out to `grep -rlE` (read-only, mirrors the
    audit's own method, phase2-B section 3.2 W8167 row) for a reproducible
    ground-truth count; never mutates the checkout.
    """
    result = subprocess.run(
        ["grep", "-rlE", pattern, str(directory)],
        capture_output=True, text=True, check=False,
    )
    if not result.stdout:
        return 0
    return len(result.stdout.strip().splitlines())


def _name_get_defined(root: Path) -> bool:
    """True if any real ORM source file at checkout *root* still defines
    `def name_get`. v16-v18 -> odoo/models.py; v19+ -> the on-disk odoo/orm/ +
    odoo/models/ package split (ADR-0005 / CLAUDE.md "index-core paths") -
    checked directly (both candidate paths) rather than assumed, since the
    flat odoo/models.py file no longer exists there.
    """
    candidates = [
        root / "odoo" / "models.py",
        root / "odoo" / "orm" / "models.py",
        root / "odoo" / "models" / "__init__.py",
    ]
    for f in candidates:
        if f.is_file():
            src = f.read_text(encoding="utf-8", errors="ignore")
            if re.search(r"^\s*def\s+name_get\b", src, re.MULTILINE):
                return True
    return False


def _name_get_body(root: Path) -> str | None:
    """Extract the real `name_get` method body text (v16-v18's flat
    odoo/models.py only - the versions this file's v16 check needs), for a
    deprecation-marker check. None if not found.
    """
    models_py = root / "odoo" / "models.py"
    if not models_py.is_file():
        return None
    src = models_py.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"^\s*def\s+name_get\b.*?(?=^\s*def\s+\w|\Z)", src, re.MULTILINE | re.DOTALL)
    return m.group(0) if m else None


# ---------------------------------------------------------------------------
# Layer 1 - classification pin (CI-safe, no checkout needed, must never skip).
# ---------------------------------------------------------------------------

# Mechanical version-boundary claim detector (phase2-B audit section 1.2's own
# definition, reproduced here as regex): a message asserting a specific claim
# about Odoo's own source at a named version boundary. Intentionally narrow
# (a boundary WORD within 80 chars of an explicit v<N> token) so it does not
# also fire on unrelated prose that merely contains a rule_id or an unrelated
# digit - the audit's own sweep hit exactly this false-positive shape
# ("XPath position='replace'" via a bare "replace" match) and pruned it by
# hand; requiring the nearby v<N> token here prunes that class mechanically.
_BOUNDARY_CLAIM_RE = re.compile(
    r"\b(removed|deprecated|renamed|introduced|replaced|changed|used)\b"
    r"[^.]{0,80}\bv\d{1,2}\+?\b"
    r"|\buse\b[^.]{0,60}\binstead\b[^.]{0,40}\bv\d{1,2}\b",
    re.IGNORECASE,
)


def _mechanically_flagged_rule_ids(version: str) -> set[str]:
    """Every rule_id in *version*'s file whose message matches the boundary
    regex - the mechanical half of "falsifiable or editorial"."""
    return {
        r["rule_id"] for r in _load_rules(version)
        if _BOUNDARY_CLAIM_RE.search(r.get("message") or "")
    }


# The reviewed snapshot ("marked falsifiable") for the three live-extractor
# versions, computed by running the regex above over the real
# lint_rules_{17,18,19}.0.json files and manually confirming each hit is a
# genuine version-boundary claim (none pruned as false positives this pass).
# A rule_id appearing in the mechanical sweep but ABSENT from this snapshot is
# exactly the "unmarked boundary claim" this layer exists to catch - the pin
# equality test below fails loudly instead of letting it pass silently.
_KNOWN_FALSIFIABLE_RULE_IDS: dict[str, frozenset[str]] = {
    "17.0": frozenset({
        "W8107", "W8108", "W8166", "W8167", "W8168", "W8169", "W8202",
    }),
    "18.0": frozenset({
        "W8107", "W8108", "W8167", "W8192", "W8193", "W8202", "W8210",
        "W8211", "W8212", "W8213",
    }),
    "19.0": frozenset({
        "W8107", "W8108", "W8192", "W8202", "W8210", "W8220",
    }),
}


@pytest.mark.parametrize("version", _LIVE_EXTRACTOR_VERSIONS)
def test_falsifiable_classification_is_pinned_against_a_reviewed_snapshot(version):
    """Business rule: the set of rules the mechanical boundary-claim regex
    flags for a live-extractor version must exactly equal a reviewed
    snapshot. A drift here means either a new, un-triaged version-boundary
    claim was added (must be reviewed and added to the snapshot - the exact
    "escapes review" failure mode this layer exists to catch) or an existing
    claim's wording changed enough to fall out of/into the mechanical net
    (also worth a human look).
    """
    assert _mechanically_flagged_rule_ids(version) == _KNOWN_FALSIFIABLE_RULE_IDS[version]


def test_w8202_and_w8167_are_correctly_classified_as_falsifiable_not_editorial():
    """Business rule: the two rule_ids this file demonstrates are factually
    WRONG (W8202, W8167) must first be correctly IDENTIFIED as falsifiable -
    classification and content-correctness are separate concerns (module
    docstring). A rule that is wrong AND miscategorized as editorial would be
    invisible to any future source-diff oracle entirely.
    """
    for version in ("17.0", "18.0", "19.0"):
        assert "W8202" in _mechanically_flagged_rule_ids(version), (
            f"W8202 must be classified falsifiable at {version}"
        )
    for version in ("17.0", "18.0"):
        assert "W8167" in _mechanically_flagged_rule_ids(version), (
            f"W8167 must be classified falsifiable at {version}"
        )


def test_an_editorial_rule_is_not_misclassified_as_falsifiable():
    """Sanity control: the classifier must actually discriminate, not flag
    everything. W8161 ("`copy()` override does not call `super()`") is a
    timeless convention with no version-boundary claim in its text - it must
    NOT be in the mechanically-flagged set at any live-extractor version.
    """
    for version in _LIVE_EXTRACTOR_VERSIONS:
        rule_ids_present = {r["rule_id"] for r in _load_rules(version)}
        if "W8161" not in rule_ids_present:
            continue
        assert "W8161" not in _mechanically_flagged_rule_ids(version), (
            f"W8161 (editorial, no version claim) was misclassified as "
            f"falsifiable at {version}"
        )


def test_falsifiable_editorial_split_is_reported_per_live_extractor_version():
    """Coverage visibility (mirrors test_lint_rules_live_parser_coverage_is_
    reported's convention in test_parser_lint_rules.py): always announces,
    via a warning shown in pytest's terminal summary, the measured
    falsifiable/editorial split per live-extractor version, so the number is
    never buried in an aggregate pass/fail count. Compare against the
    phase2-B audit's own ~16%/84% split (that split was computed by hand
    across all 12 files; this mechanical regex, scoped to v17-19 only, is
    expected to be a conservative (smaller) subset - see the task report for
    the full comparison).
    """
    lines = []
    for version in _LIVE_EXTRACTOR_VERSIONS:
        total = len(_load_rules(version))
        falsifiable = len(_mechanically_flagged_rule_ids(version))
        pct = (falsifiable / total * 100) if total else 0.0
        lines.append(f"{version}: {falsifiable}/{total} falsifiable ({pct:.0f}%)")
    warnings.warn(
        UserWarning(
            "lint_rules falsifiable/editorial split (mechanical regex, "
            "v17-v19 only): " + "; ".join(lines) + ". Audit's hand-classified "
            "split across all 12 files was ~16% falsifiable / ~84% editorial "
            "(phase2-B section 1.2)."
        ),
        stacklevel=1,
    )


# ---------------------------------------------------------------------------
# Layer 2 - content-vs-real-source (dev-box only; skips per version when the
# checkout is absent, never fails on absence).
# ---------------------------------------------------------------------------


@pytest.mark.odoo_source
def test_w8202_v17_own_file_falsely_claims_removal_at_v17():
    """Business rule: a rule that says "removed in vN" about a version's OWN
    file must be true of that SAME version's real source. W8202 in
    lint_rules_17.0.json claims `name_get()` is "removed in v17" - real v17
    source (odoo/models.py) still DEFINES name_get() (a `.. deprecated::
    17.0` docstring + `warnings.warn(..., DeprecationWarning)` call -
    deprecated, not removed; true removal is v18). Same failure shape as
    issue #362: a version's own file makes a false claim about itself.
    EXPECT RED (phase2-B audit section 3.2, W8202 row).
    """
    root = checkout_root(17)
    if root is None:
        pytest.skip("Odoo 17 checkout not found (set OSM_ODOO_CHECKOUTS)")
    assert _name_get_defined(root), (
        "ground-truth check itself is broken: v17 real source should still "
        "define name_get()"
    )

    message = _rule("17.0", "W8202")["message"]
    assert "removed in v17" not in message, (
        f"W8202 v17.0 claims name_get() 'removed in v17' but real v17 "
        f"source still defines it (deprecated only; real removal is v18) - "
        f"got message: {message!r}"
    )


@pytest.mark.odoo_source
def test_w8202_v18_correctly_states_the_true_removal_boundary():
    """Control: v18.0's own W8202 entry ("removed in v18") is the ONE
    correct entry in this family (phase2-B audit). If this ever goes red,
    something else changed (the checkout or the message), not "more of the
    same defect" as the v17/v19 tests above/below.
    """
    root = checkout_root(18)
    if root is None:
        pytest.skip("Odoo 18 checkout not found (set OSM_ODOO_CHECKOUTS)")
    assert not _name_get_defined(root), (
        "ground-truth check itself is broken: v18 real source should no "
        "longer define name_get()"
    )
    assert "removed in v18" in _rule("18.0", "W8202")["message"]


@pytest.mark.odoo_source
def test_w8202_v19_contradicts_v18_true_removal_boundary():
    """Business rule: W8202 in lint_rules_19.0.json claims name_get() is
    "removed in v17+", contradicting lint_rules_18.0.json's own,
    real-source-confirmed "removed in v18" claim (both describe the SAME
    real-world fact - the removal boundary can only be one version). True
    boundary, confirmed against real source: v17 still defines name_get()
    (deprecated only); v18 does not. EXPECT RED (phase2-B audit section 3.2).
    """
    root17 = checkout_root(17)
    root18 = checkout_root(18)
    if root17 is None or root18 is None:
        pytest.skip("Odoo 17/18 checkouts not found (set OSM_ODOO_CHECKOUTS)")
    assert _name_get_defined(root17) and not _name_get_defined(root18), (
        "ground-truth check itself is broken: expected v17 still-defined / "
        "v18 removed"
    )

    message = _rule("19.0", "W8202")["message"]
    assert "v17+" not in message, (
        f"W8202 v19.0 claims removal 'in v17+' ({message!r}), contradicting "
        "the real, source-confirmed removal boundary (v18) and v18.0's own "
        "correct entry"
    )


@pytest.mark.odoo_source
def test_w8202_v16_recommends_switch_one_version_before_any_deprecation_signal():
    """Business rule: lint_rules_16.0.json recommends switching to
    `_compute_display_name()` "in v16" - but real v16 source's name_get()
    carries NO deprecation marker at all (no `.. deprecated::` docstring
    tag, no `warnings.warn(..., DeprecationWarning)` call); those markers
    first appear at v17. The recommendation is grounded in a fact that is
    true starting v17, not v16 - "early by one version" (phase2-B audit
    section 3.2/3.3). EXPECT RED.
    """
    root16 = checkout_root(16)
    root17 = checkout_root(17)
    if root16 is None or root17 is None:
        pytest.skip("Odoo 16/17 checkouts not found (set OSM_ODOO_CHECKOUTS)")
    body16 = _name_get_body(root16)
    body17 = _name_get_body(root17)
    assert body16 is not None and body17 is not None, (
        "ground-truth check itself is broken: could not extract name_get() "
        "body at v16/v17"
    )
    v16_has_deprecation_signal = "DeprecationWarning" in body16
    v17_has_deprecation_signal = "DeprecationWarning" in body17
    assert not v16_has_deprecation_signal and v17_has_deprecation_signal, (
        "ground-truth check itself is broken: expected the deprecation "
        "signal to first appear at v17, not v16"
    )

    message = _rule("16.0", "W8202")["message"]
    assert "in v16" not in message, (
        f"W8202 v16.0 recommends switching 'in v16' ({message!r}) but real "
        "v16 source carries no deprecation signal on name_get() yet - the "
        "recommendation is grounded a version early (the signal starts v17)"
    )


@pytest.mark.odoo_source
@pytest.mark.parametrize("version", ["16.0", "17.0", "18.0", "19.0"])
def test_w8167_claimed_v16_boundary_contradicts_real_v12_v13_transition(version):
    """Business rule: W8167 (present in lint_rules_{16,17,18,19}.0.json)
    claims `track_visibility` was "renamed to `tracking` in v16[+]". Real
    usage-count evidence (grep across each version's addons tree, phase2-B
    audit section 3.2) shows the rename already happened at the v12->v13
    boundary: v12 is track_visibility-majority (122 files vs 28 using
    `tracking`), v13 is already tracking-majority (27 vs 77), and the gap
    only widens every version after. v16 (23 vs 112) is near the END of the
    migration, not its start. The claimed boundary is off by THREE major
    versions. EXPECT RED for all four versions this rule appears in.
    """
    root12 = checkout_root(12)
    root13 = checkout_root(13)
    if root12 is None or root13 is None:
        pytest.skip("Odoo 12/13 checkouts not found (set OSM_ODOO_CHECKOUTS)")
    tv12 = _grep_file_count(root12 / "addons", r"track_visibility")
    tr12 = _grep_file_count(root12 / "addons", r"\btracking\s*=")
    tv13 = _grep_file_count(root13 / "addons", r"track_visibility")
    tr13 = _grep_file_count(root13 / "addons", r"\btracking\s*=")
    assert tv12 > tr12, (
        "ground-truth check itself is broken: v12 should still be "
        f"track_visibility-majority (got track_visibility={tv12}, tracking={tr12})"
    )
    assert tr13 > tv13, (
        "ground-truth check itself is broken: v13 should already be "
        f"tracking-majority (got track_visibility={tv13}, tracking={tr13})"
    )

    message = _rule(version, "W8167")["message"]
    assert "v13" in message, (
        f"W8167 in lint_rules_{version}.json says {message!r}, implying the "
        "rename happened at v16[+] - real usage-count evidence shows the "
        "true boundary is v13 (v12 track_visibility-majority, v13 already "
        "tracking-majority), off by three major versions"
    )
