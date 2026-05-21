# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_domain_operators.py
"""M10.5 P2 — pure unit tests for version-aware domain operators.

No Neo4j required. Locks the cross-version operator matrix surfaced by the
v8→v19 survey: `parent_of` from v9, `any`/`not any` from v17, v19 access-rights
variants. See src/constants.py::valid_domain_operators.
"""
from src.constants import RELATIONAL_TTYPES, valid_domain_operators


def test_base_operators_present_all_versions():
    for v in ("8.0", "11.0", "17.0", "19.0"):
        ops = valid_domain_operators(v)
        for base in ("=", "!=", "like", "ilike", "in", "not in", "child_of", "=like"):
            assert base in ops, f"{base} missing in {v}"


def test_parent_of_gated_from_v9():
    assert "parent_of" not in valid_domain_operators("8.0")
    assert "parent_of" in valid_domain_operators("9.0")
    assert "parent_of" in valid_domain_operators("17.0")


def test_any_gated_from_v17():
    assert "any" not in valid_domain_operators("16.0")
    assert "not any" not in valid_domain_operators("16.0")
    assert "any" in valid_domain_operators("17.0")
    assert "not any" in valid_domain_operators("18.0")


def test_v19_access_rights_variants():
    ops = valid_domain_operators("19.0")
    assert {"any!", "not any!", "not =like", "not =ilike"} <= ops


def test_unknown_version_is_permissive():
    # sentinels / unparseable → permissive superset (avoid false positives).
    for v in ("auto", "default", "latest", "", "garbage"):
        ops = valid_domain_operators(v)
        assert "any" in ops and "parent_of" in ops


def test_relational_ttypes_lowercase():
    assert RELATIONAL_TTYPES == frozenset({"many2one", "one2many", "many2many"})
