# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_pattern_catalogue_invariants_unit.py
"""Pure-logic unit tests extracted from test_pattern_catalogue_invariants.py (WS-D / DD2 demote).

These catalogue-shape invariants read ``src/data/patterns.json`` from disk and
assert on the parsed list (presence of expected pattern IDs, ≥80 entries, ≥3
gotchas per W3-3 entry).  They never open a Neo4j session or a Postgres
connection and never request the ``clean_neo4j`` / ``clean_pg_embeddings``
fixtures.  The parent file carries a module-level
``pytestmark = pytest.mark.neo4j`` for its ``suggest_pattern`` pipeline tests
(which seed real Neo4j + pgvector), which a per-test override cannot subtract;
so these pure file-read invariants live here in an unmarked module and now run
in the fast unit tier (``-m 'not neo4j'``).

DD2 evidence: confirmed JSON-file read + set/length assertions only —
no DB fixture dependency.
"""
import json
from pathlib import Path

# Catalogue path relative to repo root
_PATTERNS_PATH = Path(__file__).resolve().parent.parent / "src" / "data" / "patterns.json"

# -------------------------------------------------------------------
# Expected pattern IDs from W3-3 (anti-truncation guard)
# -------------------------------------------------------------------
_W3_3_PATTERN_IDS = {
    "portal-sudo-public-user-access",
    "portal-layout-template-inherit",
    "portal-mixin-ensure-token",
    "portal-compute-with-sudo",
    "wizard-transient-default-get-context",
    "wizard-action-close-vs-open",
    "wizard-backorder-default-get-x2m",
    "multi-company-with-company-context",
    "multi-company-ir-rule-domain-force",
    "multi-company-property-field",
    "ir-attachment-create-res-model-res-id",
    "ir-attachment-binary-field-attachment-true",
    "ir-attachment-download-url",
    "owl-onmounted-lifecycle",
    "owl-usestate-reactive-mutation",
    "owl-template-t-attf-class",
    "owl-patch-service-override",
    "security-acl-csv-group-model",
    "security-ir-rule-portal-domain",
    "security-groups-field-attribute",
    "domain-or-operator-prefix-notation",
    "domain-child-of-parent-of",
    "domain-filter-domain-search-view",
    "mail-thread-mixin-message-post",
    "mail-thread-activity-schedule",
    "mail-thread-override-message-post",
    "report-qweb-t-foreach-docs",
    "report-qweb-t-set-subtotal",
    "website-published-mixin",
}

# WG-5 patterns (anti-truncation guard)
_WG5_PATTERN_IDS = {
    "owl-field-widget-register-v17",
    "owl-field-widget-with-template-v17",
}

# -------------------------------------------------------------------
# Test-writing patterns from issue #329 (anti-truncation + range-sane guard)
#
# HARDCODED ORACLE - deliberately independent of patterns.json so that
# deleting or renaming any of these 8 ``category="test"`` entries makes a
# test FAIL.  Do NOT derive this set from the JSON: an oracle that reads
# its own answer from the file under test cannot detect the file losing an
# entry.
# -------------------------------------------------------------------
_TEST_PATTERN_IDS = {
    "test-transaction-savepoint-v16plus",
    "test-savepointcase-v8-v15",
    "test-computed-field",
    "test-access-rights",
    "test-multicompany-constraint",
    "test-httpcase-tour-qunit-v17",
    "test-httpcase-tour-hoot-v18",
    "test-form-onchange",
}


# -------------------------------------------------------------------
# Catalogue-level tests (no DB required)
# -------------------------------------------------------------------

def test_catalogue_contains_all_w3_3_ids():
    """All W3-3 pattern IDs must be present in patterns.json (anti-truncation guard)."""
    data = json.loads(_PATTERNS_PATH.read_text(encoding="utf-8"))
    present_ids = {p["pattern_id"] for p in data}
    missing = _W3_3_PATTERN_IDS - present_ids
    assert not missing, (
        f"W3-3 patterns missing from catalogue ({len(missing)} absent): {sorted(missing)}"
    )


def test_catalogue_contains_wg5_patterns():
    """WG-5 OWL field-widget patterns must be present (anti-truncation guard)."""
    data = json.loads(_PATTERNS_PATH.read_text(encoding="utf-8"))
    present_ids = {p["pattern_id"] for p in data}
    missing = _WG5_PATTERN_IDS - present_ids
    assert not missing, (
        f"WG-5 patterns missing from catalogue ({len(missing)} absent): {sorted(missing)}"
    )


def test_catalogue_size_at_least_80():
    """Catalogue must contain ≥80 entries after W3-3 additions."""
    data = json.loads(_PATTERNS_PATH.read_text(encoding="utf-8"))
    assert len(data) >= 80, f"Expected ≥80 patterns in catalogue, got {len(data)}"


def test_catalogue_w3_3_entries_have_3_gotchas():
    """Every W3-3 entry must have exactly ≥3 gotchas (schema-enforced, but double-check)."""
    data = json.loads(_PATTERNS_PATH.read_text(encoding="utf-8"))
    by_id = {p["pattern_id"]: p for p in data}
    violations = [
        pid for pid in _W3_3_PATTERN_IDS
        if pid in by_id and len(by_id[pid].get("gotchas", [])) < 3
    ]
    assert not violations, (
        f"W3-3 patterns with fewer than 3 gotchas: {violations}"
    )


def test_catalogue_contains_all_test_patterns():
    """All 8 test-writing pattern IDs must be present with category=='test' (anti-truncation guard).

    Issue #329: deleting or renaming any of these production ``test-*`` entries
    must FAIL here.  Checking category guards against an entry being demoted out
    of the ``category="test"`` filter that ``suggest_pattern(category='test')``
    relies on.
    """
    data = json.loads(_PATTERNS_PATH.read_text(encoding="utf-8"))
    by_id = {p["pattern_id"]: p for p in data}
    missing = _TEST_PATTERN_IDS - set(by_id)
    assert not missing, (
        f"test patterns missing from catalogue ({len(missing)} absent): {sorted(missing)}"
    )
    wrong_category = [
        pid for pid in _TEST_PATTERN_IDS
        if by_id[pid].get("category") != "test"
    ]
    assert not wrong_category, (
        f"test patterns not tagged category=='test': {sorted(wrong_category)}"
    )


def test_test_patterns_have_3_gotchas():
    """Every test-writing entry must have ≥3 gotchas (behavior-level guard vs schema relaxation)."""
    data = json.loads(_PATTERNS_PATH.read_text(encoding="utf-8"))
    by_id = {p["pattern_id"]: p for p in data}
    violations = [
        pid for pid in _TEST_PATTERN_IDS
        if pid in by_id and len(by_id[pid].get("gotchas", [])) < 3
    ]
    assert not violations, (
        f"test patterns with fewer than 3 gotchas: {sorted(violations)}"
    )


def test_test_patterns_version_range_sane():
    """Every test-writing entry must have a sane [min, max] version range (numeric compare).

    Protects the ``odoo_version_max`` range filter that WI-1 adds: an inverted
    range (max < min) would silently exclude the pattern from every version.
    NUMERIC compare, not string (``"8.0" < "15.0"`` only by float, not lexicographically).
    """
    data = json.loads(_PATTERNS_PATH.read_text(encoding="utf-8"))
    by_id = {p["pattern_id"]: p for p in data}
    violations = []
    for pid in _TEST_PATTERN_IDS:
        entry = by_id.get(pid)
        if entry is None:
            continue
        vmin = entry.get("odoo_version_min")
        vmax = entry.get("odoo_version_max")
        if vmax is not None and float(vmax) < float(vmin):
            violations.append((pid, vmin, vmax))
    assert not violations, (
        f"test patterns with inverted version range (max < min): {sorted(violations)}"
    )


def test_all_patterns_have_valid_category():
    """#331 backfill guard: every entry must have category in ('test', 'production').

    No entry may be uncategorized (None or absent key). Locks the backfill so that:
    - adding a new pattern without a category makes this test fail immediately.
    - removing the ``category`` key from an existing entry makes this test fail.

    The oracle is the closed set {'test', 'production'} - hardcoded here so the
    test cannot trivially pass by accepting any value the file happens to contain.
    """
    data = json.loads(_PATTERNS_PATH.read_text(encoding="utf-8"))
    valid_categories = {"test", "production"}
    violations = [
        (p.get("pattern_id", f"<index {i}>"), p.get("category"))
        for i, p in enumerate(data)
        if p.get("category") not in valid_categories
    ]
    assert not violations, (
        f"patterns.json has {len(violations)} entries with missing or invalid category "
        f"(must be 'test' or 'production'): {sorted(violations)}"
    )
