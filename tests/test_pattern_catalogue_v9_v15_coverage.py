# SPDX-License-Identifier: AGPL-3.0-or-later
"""
WI-A3 — PatternExample v9–v15 coverage tests.

Validates that:
1. Each Odoo version in {9.0..15.0} has at least 3 patterns in the catalogue.
2. v9–v15 patterns use only stable APIs (no v17-specific decorators where not applicable).
"""

import json
from pathlib import Path

import pytest

PATTERNS_PATH = Path(__file__).parent.parent / "src" / "data" / "patterns.json"
TARGET_VERSIONS = {"9.0", "10.0", "11.0", "12.0", "13.0", "14.0", "15.0"}
MIN_PATTERNS_PER_VERSION = 3


@pytest.fixture(scope="module")
def patterns():
    with PATTERNS_PATH.open() as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def v9_v15_patterns(patterns):
    return [p for p in patterns if p["odoo_version_min"] in TARGET_VERSIONS]


def test_each_v9_v15_version_has_min_3_patterns(patterns):
    """Each version in 9.0–15.0 must have at least 3 patterns."""
    by_version: dict[str, list[str]] = {}
    for p in patterns:
        v = p["odoo_version_min"]
        if v in TARGET_VERSIONS:
            by_version.setdefault(v, []).append(p["pattern_id"])

    missing_versions = TARGET_VERSIONS - set(by_version.keys())
    assert not missing_versions, (
        f"Versions with ZERO patterns (need ≥{MIN_PATTERNS_PER_VERSION}): "
        f"{sorted(missing_versions)}"
    )

    under_minimum = {
        v: ids
        for v, ids in by_version.items()
        if len(ids) < MIN_PATTERNS_PER_VERSION
    }
    assert not under_minimum, (
        f"Versions below minimum ({MIN_PATTERNS_PER_VERSION} patterns): "
        + ", ".join(
            f"{v} has {len(ids)}" for v, ids in sorted(under_minimum.items())
        )
    )


def test_v9_v15_patterns_target_stable_apis(v9_v15_patterns):
    """
    v9–v15 patterns must not reference unstable/version-mismatched APIs:
    - language must be one of {python, xml, js}
    - versions < 13.0 must not use @api.model_create_multi (v13+ only)
    - versions < 17.0 must not use @api.depends_context with multiple args
      (single-arg 'lang' was stable from v11; multiple-arg form is v17+)
    """
    VALID_LANGUAGES = {"python", "xml", "js"}
    violations: list[str] = []

    for p in v9_v15_patterns:
        pid = p["pattern_id"]
        snippet = p.get("snippet_text", "")
        version_min = p["odoo_version_min"]
        language = p.get("language", "")

        # Check language is valid
        if language not in VALID_LANGUAGES:
            violations.append(
                f"{pid}: invalid language '{language}' "
                f"(must be one of {VALID_LANGUAGES})"
            )

        # Versions < 13.0 must not reference @api.model_create_multi
        try:
            major = float(version_min)
        except ValueError:
            major = 0.0

        if major < 13.0 and "@api.model_create_multi" in snippet:
            violations.append(
                f"{pid} (v{version_min}): references @api.model_create_multi "
                f"which was introduced in v13 — not valid for pre-v13 patterns"
            )

        # Versions < 17.0 must not use @api.depends_context with multiple keyword
        # arguments in a single decorator call (v17+ feature).
        # Single-arg depends_context('lang') is fine from v11 onward.
        if major < 17.0 and "@api.depends_context" in snippet:
            # Count args: if the snippet has @api.depends_context with >1 arg it's v17+
            import re
            matches = re.findall(
                r"@api\.depends_context\(([^)]+)\)", snippet
            )
            for m in matches:
                args = [a.strip() for a in m.split(",") if a.strip()]
                if len(args) > 1:
                    violations.append(
                        f"{pid} (v{version_min}): @api.depends_context with "
                        f"{len(args)} args ({m.strip()}) — multi-arg form is v17+ only"
                    )

    assert not violations, (
        f"Found {len(violations)} API stability violation(s):\n"
        + "\n".join(f"  - {v}" for v in violations)
    )
