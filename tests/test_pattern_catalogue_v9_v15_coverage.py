# SPDX-License-Identifier: AGPL-3.0-or-later
"""
WI-A3 — PatternExample v9–v15 coverage tests.

Validates that:
1. suggest_pattern serves at least 3 patterns at each Odoo version in {9.0..15.0}.
2. v9-v15 patterns use only APIs that exist at their minimum version.
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


def _serves(pattern, version):
    """Same window rule as src/mcp/tools/guidance.py::_version_covers (numeric compare)."""
    if float(pattern["odoo_version_min"]) > version:
        return False
    vmax = pattern.get("odoo_version_max")
    return vmax is None or float(vmax) >= version


def test_each_v9_v15_version_has_min_3_patterns(patterns):
    """suggest_pattern serves at least 3 patterns at every version in 9.0-15.0."""
    served = {
        v: [p["pattern_id"] for p in patterns if _serves(p, float(v))]
        for v in sorted(TARGET_VERSIONS, key=float)
    }
    under_minimum = {v: ids for v, ids in served.items() if len(ids) < MIN_PATTERNS_PER_VERSION}
    assert not under_minimum, (
        f"Versions served fewer than {MIN_PATTERNS_PER_VERSION} patterns: "
        + ", ".join(f"{v} has {len(ids)}" for v, ids in sorted(under_minimum.items()))
    )


def test_v9_v15_patterns_target_stable_apis(v9_v15_patterns):
    """
    v9-v15 patterns must not reference unstable/version-mismatched APIs:
    - language must be one of {python, xml, js}
    - patterns served below 12.0 must not use @api.model_create_multi (odoo/api.py defines
      model_create_multi from 12.0; absent at 8.0-11.0)
    - patterns served below 13.0 must not use @api.depends_context (odoo/api.py defines
      depends_context from 13.0; absent at 8.0-12.0)
    """
    VALID_LANGUAGES = {"python", "xml", "js"}
    violations: list[str] = []

    for p in v9_v15_patterns:
        pid = p["pattern_id"]
        snippet = p.get("snippet_text", "")
        version_min = p["odoo_version_min"]
        language = p.get("language", "")

        if language not in VALID_LANGUAGES:
            violations.append(
                f"{pid}: invalid language '{language}' "
                f"(must be one of {VALID_LANGUAGES})"
            )

        major = float(version_min)

        if major < 12.0 and "@api.model_create_multi" in snippet:
            violations.append(
                f"{pid} (v{version_min}): references @api.model_create_multi "
                f"which exists from v12 - not valid for pre-v12 patterns"
            )

        if major < 13.0 and "@api.depends_context" in snippet:
            violations.append(
                f"{pid} (v{version_min}): references @api.depends_context "
                f"which exists from v13 - not valid for pre-v13 patterns"
            )

    assert not violations, (
        f"Found {len(violations)} API stability violation(s):\n"
        + "\n".join(f"  - {v}" for v in violations)
    )
