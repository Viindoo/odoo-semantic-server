# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_master_data_seed_unit.py
"""Pure-logic unit tests extracted from test_master_data_seed.py (WS-D / DD2 demote).

These tests assert on the seed-module's default constants (``_PROFILE_DEFS``,
``_REPO_DEFS_BY_PROFILE``, ``_SEED_NAME_PATTERNS``) and run the cycle-free /
version-match guards over those in-memory tuples.  They take NO ``clean_pg``
fixture and open NO Postgres connection.  The parent file carries a module-level
``pytestmark = pytest.mark.postgres`` for its real seeding/idempotency tests
(which run migrations and query real tables), which a per-test override cannot
subtract; so these pure constant-checks live here in an unmarked module and now
run in the fast unit tier (``-m 'not postgres'``).

DD2 evidence: confirmed pure Python checks on ``_PROFILE_DEFS`` —
no DB fixture dependency.

Note: the two ``*_synthetic`` pure tests remain in the parent file because they
share the file-local ``_SYNTHETIC_PROFILES`` literal with the monkeypatch-based
DB mechanism tests (keeping that constant single-sourced).
"""
from src.db.seed_master_data import (
    _PROFILE_DEFS,
    _REPO_DEFS_BY_PROFILE,
    _SEED_NAME_PATTERNS,
)

# ---------------------------------------------------------------------------
# Default empty state (no DB, no monkeypatch)
# ---------------------------------------------------------------------------

def test_profile_defs_empty_by_default():
    """_PROFILE_DEFS must be empty in the open-core release."""
    assert _PROFILE_DEFS == []


def test_repo_defs_by_profile_empty_by_default():
    """_REPO_DEFS_BY_PROFILE must be empty in the open-core release."""
    assert _REPO_DEFS_BY_PROFILE == {}


def test_seed_name_patterns_empty_by_default():
    """_SEED_NAME_PATTERNS must be empty tuple so reset_seeded_data is a no-op."""
    assert _SEED_NAME_PATTERNS == ()


# ---------------------------------------------------------------------------
# Pure structural guards over _PROFILE_DEFS (no DB)
# ---------------------------------------------------------------------------

def test_profile_defs_no_cycles():
    """_PROFILE_DEFS parent chain must be cycle-free.

    With the default empty list this vacuously passes. The test is preserved
    as a guard for when _PROFILE_DEFS is populated.
    """
    parent_map = {name: parent for name, _v, _d, parent in _PROFILE_DEFS}
    max_depth = len(_PROFILE_DEFS) + 1

    for name, _v, _d, _parent in _PROFILE_DEFS:
        visited = set()
        current = name
        depth = 0
        while current is not None and depth <= max_depth:
            assert current not in visited, (
                f"Cycle detected in _PROFILE_DEFS starting from {name!r}: "
                f"visited {visited!r}, hit {current!r} again"
            )
            visited.add(current)
            current = parent_map.get(current)
            depth += 1


def test_profile_defs_version_match():
    """Each _PROFILE_DEFS entry with a parent must share the parent's odoo_version.

    Vacuously passes on the default empty list. Preserved as a CI guard for
    when _PROFILE_DEFS is populated.
    """
    version_map = {name: version for name, version, _d, _parent in _PROFILE_DEFS}

    for name, version, _desc, parent_name in _PROFILE_DEFS:
        if parent_name is None:
            continue
        assert parent_name in version_map, (
            f"_PROFILE_DEFS entry {name!r} references parent {parent_name!r} "
            f"which is not in _PROFILE_DEFS"
        )
        parent_version = version_map[parent_name]
        assert version == parent_version, (
            f"Version mismatch in _PROFILE_DEFS: {name!r} has version {version!r} "
            f"but parent {parent_name!r} has version {parent_version!r}"
        )
