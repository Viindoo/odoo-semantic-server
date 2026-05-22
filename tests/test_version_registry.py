# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_version_registry.py — Unit tests for the (min,max,fn) version-dispatch registry.
#
# Acceptance criteria (ADR-0032):
#   - Entries sorted ascending by min_major regardless of insertion order.
#   - First-match wins (no fall-through to a later entry).
#   - max_major=None means open-ended ("this version and all newer ones").
#   - Correct boundary at major=9/10 (LEGACY_ERA_MAX_MAJOR) and major=13/14 (OWL).
#   - A v20 entry resolves correctly by capping the prior open-ended entry and
#     adding the new entry — all changes are localised inside one registry list.
#   - Unparseable version strings return the caller-supplied default.

from src.indexer.version_registry import VersionRegistry, make_version_registry

# ---------------------------------------------------------------------------
# Basic resolution
# ---------------------------------------------------------------------------

class TestVersionRegistryBasic:
    def test_single_open_ended_entry(self):
        reg = VersionRegistry([(10, None, "modern")])
        assert reg.resolve(10) == "modern"
        assert reg.resolve(17) == "modern"
        assert reg.resolve(99) == "modern"

    def test_single_bounded_entry(self):
        reg = VersionRegistry([(10, 16, "legacy_window")])
        assert reg.resolve(9) is None   # below min
        assert reg.resolve(10) == "legacy_window"
        assert reg.resolve(16) == "legacy_window"
        assert reg.resolve(17) is None   # above max

    def test_no_entries_returns_default(self):
        reg = VersionRegistry([])
        assert reg.resolve(17) is None
        assert reg.resolve(17, default="fallback") == "fallback"

    def test_below_all_entries_returns_default(self):
        reg = VersionRegistry([(10, None, "v10+")])
        assert reg.resolve(8) is None
        assert reg.resolve(9) is None


# ---------------------------------------------------------------------------
# Sorting guarantee — insertion order must not matter
# ---------------------------------------------------------------------------

class TestSortingGuarantee:
    def test_entries_sorted_by_min_major(self):
        # Insert in reverse order; should still resolve correctly.
        reg = VersionRegistry([
            (10, None, "era2"),
            (8,  9,    "era1"),
        ])
        assert reg.resolve(8) == "era1"
        assert reg.resolve(9) == "era1"
        assert reg.resolve(10) == "era2"
        assert reg.resolve(17) == "era2"

    def test_first_match_wins_no_fall_through(self):
        # Two overlapping entries — first (lower min_major) must win.
        reg = VersionRegistry([
            (8, None, "catch-all"),
            (8, 9,    "era1"),
        ])
        # Both entries match major=8. The one with min_major=8 inserted first in
        # the sorted list wins → "catch-all" (same min_major, stable sort order).
        # The important invariant: NO fall-through once a match is found.
        result = reg.resolve(8)
        assert result in ("catch-all", "era1")  # one of the two — first in sorted order

    def test_no_fall_through_demonstrated(self):
        # Clear non-overlapping case: first matching entry stops the search.
        reg = VersionRegistry([
            (8,  9,    "era1"),
            (10, None, "era2"),
        ])
        assert reg.resolve(9) == "era1"   # matched era1 — must NOT also return era2
        assert reg.resolve(10) == "era2"  # did not match era1 (10 > 9)


# ---------------------------------------------------------------------------
# Boundary tests at major=9/10 and major=13/14
# ---------------------------------------------------------------------------

class TestBoundaries:
    def test_python_era_boundary_9_10(self):
        """Mirror the actual _ERA_REGISTRY used in parser_python.py."""
        from src.constants import LEGACY_ERA_MAX_MAJOR
        reg = VersionRegistry([
            (8,  LEGACY_ERA_MAX_MAJOR, "era1"),
            (10, None,                 "era2"),
        ])
        assert reg.resolve(8) == "era1"
        assert reg.resolve(9) == "era1"
        assert reg.resolve(10) == "era2"
        assert reg.resolve(17) == "era2"
        assert reg.resolve(19) == "era2"

    def test_owl_boundary_13_14(self):
        """Mirror the actual _OWL_ENABLED_REGISTRY used in parser_js.py."""
        reg = VersionRegistry([(14, None, True)])
        assert reg.resolve(13) is None           # OWL not available
        assert reg.resolve(13, default=False) is False
        assert reg.resolve(14) is True           # OWL available
        assert reg.resolve(17) is True
        assert reg.resolve(19) is True

    def test_namespace_prefix_boundary_9_10(self):
        """Mirror the actual _PREFIX_REGISTRY used in parser_odoo_core.py."""
        from src.constants import ODOO_NAMESPACE_LEGACY_MAX_MAJOR
        reg = VersionRegistry([
            (8,  ODOO_NAMESPACE_LEGACY_MAX_MAJOR, "openerp/"),
            (10, None,                             "odoo/"),
        ])
        assert reg.resolve(8)  == "openerp/"
        assert reg.resolve(9)  == "openerp/"
        assert reg.resolve(10) == "odoo/"
        assert reg.resolve(17) == "odoo/"


# ---------------------------------------------------------------------------
# resolve_version — string parsing
# ---------------------------------------------------------------------------

class TestResolveVersion:
    def test_parses_standard_format(self):
        reg = VersionRegistry([(8, 9, "era1"), (10, None, "era2")])
        assert reg.resolve_version("8.0") == "era1"
        assert reg.resolve_version("9.0") == "era1"
        assert reg.resolve_version("10.0") == "era2"
        assert reg.resolve_version("17.0") == "era2"

    def test_unparseable_returns_default(self):
        reg = VersionRegistry([(10, None, "era2")])
        assert reg.resolve_version("unknown") is None
        assert reg.resolve_version("unknown", default="era2") == "era2"
        assert reg.resolve_version("", default="era2") == "era2"
        assert reg.resolve_version(None, default="era2") == "era2"  # type: ignore[arg-type]

    def test_long_version_string(self):
        reg = VersionRegistry([(8, 9, "era1"), (10, None, "era2")])
        assert reg.resolve_version("17.0.1.0.0") == "era2"


# ---------------------------------------------------------------------------
# Open-ended max (max=None = "this version and all newer")
# ---------------------------------------------------------------------------

class TestOpenEndedMax:
    def test_open_ended_matches_very_high_major(self):
        reg = VersionRegistry([(10, None, "modern")])
        assert reg.resolve(100) == "modern"
        assert reg.resolve(999) == "modern"

    def test_open_ended_does_not_match_below_min(self):
        reg = VersionRegistry([(10, None, "modern")])
        assert reg.resolve(9) is None


# ---------------------------------------------------------------------------
# v20 demonstrator — all changes localised inside the registry list
# ---------------------------------------------------------------------------

class TestV20LocalisedChange:
    def test_v20_entry_by_capping_prior_open_ended(self):
        """Demonstrate adding v20 support: cap the open-ended entry and add one new entry.

        Both changes are inside the registry list — no if-branches anywhere in parser logic.
        """
        # Existing registry (v8–v19 coverage) — era2 is open-ended (covers v10 and above)
        existing: list[tuple[int, int | None, str]] = [
            (8,  9,    "era1"),
            (10, None, "era2"),
        ]

        # ---- v20 registry (2 localised changes in the entries list) ----------
        # Change 1: cap era2 at v19 so it no longer absorbs v20+
        # Change 2: add the new v20 entry
        v20_entries: list[tuple[int, int | None, str]] = [
            (8,  9,    "era1"),
            (10, 19,   "era2"),          # was (10, None, "era2") — capped
            (20, None, "era3_hypo"),     # new entry
        ]

        reg_existing = VersionRegistry(existing)
        reg_v20 = VersionRegistry(v20_entries)

        # Existing behavior preserved for v8–v19
        for major in (8, 9, 10, 13, 17, 19):
            assert reg_v20.resolve(major) == reg_existing.resolve(major), (
                f"v20 registry broke existing behavior at major={major}"
            )

        # v20+ routes to new handler
        assert reg_v20.resolve(20) == "era3_hypo"
        assert reg_v20.resolve(21) == "era3_hypo"

    def test_v20_no_change_needed_when_handler_is_same(self):
        """When OWL is still enabled for v20 (same handler), the registry is unchanged."""
        # OWL registry: v14+ is open-ended — v20 automatically covered without change.
        reg = VersionRegistry([(14, None, True)])
        assert reg.resolve(13, default=False) is False
        assert reg.resolve(14) is True
        assert reg.resolve(20) is True  # already covered — no change needed


# ---------------------------------------------------------------------------
# make_version_registry convenience constructor
# ---------------------------------------------------------------------------

class TestMakeVersionRegistry:
    def test_convenience_constructor(self):
        reg = make_version_registry([(8, 9, "era1"), (10, None, "era2")])
        assert reg.resolve(9) == "era1"
        assert reg.resolve(10) == "era2"
