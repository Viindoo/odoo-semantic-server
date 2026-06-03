# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guard: pytest marker registry discipline (unit, no DB).

INTENT (business rule this protects): the test suite is tiered by markers so a
unit test never gets dragged into the slow integration/browser tier and a typo'd
marker never silently disables `--strict-markers`. This guard fails RED if:
  - the new tiering markers ({astro, http, nightly}) are removed/renamed, or
  - the marker registry collapses to empty, or
  - `--strict-markers` is dropped from addopts (the anti-typo enforcement).

This is mechanism (b) from the WS-A brief: parse the pyproject marker registry and
assert the contract directly. It does NOT heuristically scan test files for
"unit test in an integration file" — that produces false positives on files that
intentionally mix tiers, so it is deliberately omitted (correctness over ambition).
"""
import tomllib
from pathlib import Path

import pytest

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _load_pytest_ini() -> dict:
    with _PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    return data["tool"]["pytest"]["ini_options"]


def _registered_marker_names() -> set[str]:
    """Marker names from the 'name: description' registry entries in pyproject."""
    ini = _load_pytest_ini()
    names = set()
    for entry in ini.get("markers", []):
        # Registry format is "name: human description" — take the token before ':'.
        names.add(entry.split(":", 1)[0].strip())
    return names


def test_registry_is_non_empty():
    """A collapsed (empty) marker registry would silently neuter the tiering scheme."""
    assert _registered_marker_names(), "pyproject marker registry must not be empty"


@pytest.mark.parametrize("marker", ["astro", "http", "nightly"])
def test_tiering_markers_are_registered(marker):
    """The Wave-2 tiering markers must stay declared — removing one breaks the tier."""
    registered = _registered_marker_names()
    assert marker in registered, (
        f"marker '{marker}' missing from pyproject [tool.pytest.ini_options].markers — "
        f"registered: {sorted(registered)}"
    )

    # A registered marker must carry a non-empty human description (registry hygiene:
    # a bare 'http' with no description defeats `pytest --markers` discoverability).
    ini = _load_pytest_ini()
    entry = next(e for e in ini["markers"] if e.split(":", 1)[0].strip() == marker)
    assert ":" in entry and entry.split(":", 1)[1].strip(), (
        f"marker '{marker}' must have a non-empty description"
    )


def test_strict_markers_enforced():
    """--strict-markers must stay in addopts — it is the anti-typo/anti-tier-drift gate."""
    ini = _load_pytest_ini()
    addopts = ini.get("addopts", "")
    assert "--strict-markers" in addopts, (
        "addopts must include --strict-markers so an unregistered marker fails collection"
    )
