# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_master_data_seed.py
"""Tests for master data seeding mechanism (profiles + repos).

The default _PROFILE_DEFS and _REPO_DEFS_BY_PROFILE are empty. Most tests
use monkeypatch to inject synthetic generic fixtures and verify the seeding
MECHANISM (2-pass FK, idempotency, conflict warning, reset, cycle guard).

Tests that verify the default-empty state do NOT use monkeypatch.

Uses the session-scoped pg_conn + per-test clean_pg fixture to wipe schema
tables before/after each test.
"""

import pytest

from src.db.migrate import run_migrations
from src.db.seed_master_data import (
    _PROFILE_DEFS,
    _REPO_DEFS_BY_PROFILE,
    _SEED_NAME_PATTERNS,
    reset_seeded_data,
    seed_all,
    seed_profiles,
    seed_repos,
)

pytestmark = pytest.mark.postgres

# ---------------------------------------------------------------------------
# Synthetic generic fixtures for mechanism tests
# ---------------------------------------------------------------------------

_SYNTHETIC_PROFILES: list[tuple[str, str, str, str | None]] = [
    ("t_root",  "17.0", "Root profile 17.0",  None),
    ("t_child", "17.0", "Child profile 17.0", "t_root"),
]

_SYNTHETIC_REPOS: dict[str, list[tuple[str, str, str]]] = {
    "t_root":  [("base", "git@github.com:example/base.git",  "17.0")],
    "t_child": [("ext",  "git@github.com:example/ext.git",   "17.0")],
}

_SYNTHETIC_PATTERNS = ("t\\_root", "t\\_child")


def _count_profiles(conn, pattern: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM profiles WHERE name LIKE %s ESCAPE '\\'",
            (pattern,),
        )
        return cur.fetchone()[0]


def _count_repos_for_profile(conn, profile_name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM repos r "
            "JOIN profiles p ON p.id = r.profile_id "
            "WHERE p.name = %s",
            (profile_name,),
        )
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Default empty state (no monkeypatch)
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


def test_seed_all_returns_zero_on_empty_defs(clean_pg):
    """seed_all() with default empty defs returns all-zero counts."""
    run_migrations(clean_pg)
    summary = seed_all(clean_pg)
    assert summary == {
        "profiles_inserted": 0,
        "profiles_skipped": 0,
        "repos_inserted": 0,
        "repos_skipped": 0,
    }


# ---------------------------------------------------------------------------
# Mechanism tests (synthetic fixtures via monkeypatch)
# ---------------------------------------------------------------------------

def test_seed_profiles_inserts_and_counts(clean_pg, monkeypatch):
    """seed_profiles() with synthetic defs inserts expected count; idempotent on second call."""
    import src.db.seed_master_data as smd
    monkeypatch.setattr(smd, "_PROFILE_DEFS", _SYNTHETIC_PROFILES)

    run_migrations(clean_pg)
    inserted, skipped = seed_profiles(clean_pg)
    assert inserted == 2
    assert skipped == 0

    # Re-run is idempotent
    inserted2, skipped2 = seed_profiles(clean_pg)
    assert inserted2 == 0
    assert skipped2 == 2


def test_seed_repos_inserts_correct_count(clean_pg, monkeypatch):
    """seed_repos() with synthetic defs inserts expected repo count."""
    import src.db.seed_master_data as smd
    monkeypatch.setattr(smd, "_PROFILE_DEFS", _SYNTHETIC_PROFILES)
    monkeypatch.setattr(smd, "_REPO_DEFS_BY_PROFILE", _SYNTHETIC_REPOS)

    run_migrations(clean_pg)
    seed_profiles(clean_pg)
    inserted, skipped = seed_repos(clean_pg)
    assert inserted == 2  # 1 for t_root, 1 for t_child
    assert skipped == 0
    assert _count_repos_for_profile(clean_pg, "t_root") == 1
    assert _count_repos_for_profile(clean_pg, "t_child") == 1


def test_seed_all_idempotent(clean_pg, monkeypatch):
    """Calling seed_all() twice does not duplicate rows."""
    import src.db.seed_master_data as smd
    monkeypatch.setattr(smd, "_PROFILE_DEFS", _SYNTHETIC_PROFILES)
    monkeypatch.setattr(smd, "_REPO_DEFS_BY_PROFILE", _SYNTHETIC_REPOS)

    run_migrations(clean_pg)
    first = seed_all(clean_pg)
    second = seed_all(clean_pg)

    assert first["profiles_inserted"] == 2
    assert first["repos_inserted"] == 2
    assert second["profiles_inserted"] == 0
    assert second["profiles_skipped"] == 2
    assert second["repos_inserted"] == 0
    assert second["repos_skipped"] == 2

    # No duplicates
    with clean_pg.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM repos")
        assert cur.fetchone()[0] == 2


def test_seed_sets_parent_fk(clean_pg, monkeypatch):
    """After seed_profiles(), t_child.parent_profile_id → t_root (2-pass FK)."""
    import src.db.seed_master_data as smd
    monkeypatch.setattr(smd, "_PROFILE_DEFS", _SYNTHETIC_PROFILES)

    run_migrations(clean_pg)
    seed_profiles(clean_pg)

    with clean_pg.cursor() as cur:
        cur.execute(
            "SELECT p_child.parent_profile_id, p_parent.name "
            "FROM profiles p_child "
            "JOIN profiles p_parent ON p_parent.id = p_child.parent_profile_id "
            "WHERE p_child.name = %s",
            ("t_child",),
        )
        row = cur.fetchone()

    assert row is not None, "t_child must have a parent_profile_id set"
    assert row[1] == "t_root", f"expected parent t_root, got {row[1]}"


def test_seed_parent_fk_idempotent(clean_pg, monkeypatch):
    """Second seed_profiles() call must not change parent_profile_id."""
    import src.db.seed_master_data as smd
    monkeypatch.setattr(smd, "_PROFILE_DEFS", _SYNTHETIC_PROFILES)

    run_migrations(clean_pg)
    seed_profiles(clean_pg)

    with clean_pg.cursor() as cur:
        cur.execute(
            "SELECT parent_profile_id FROM profiles WHERE name = %s", ("t_child",)
        )
        parent_id_before = cur.fetchone()[0]

    seed_profiles(clean_pg)

    with clean_pg.cursor() as cur:
        cur.execute(
            "SELECT parent_profile_id FROM profiles WHERE name = %s", ("t_child",)
        )
        parent_id_after = cur.fetchone()[0]

    assert parent_id_before == parent_id_after, (
        "parent_profile_id must not change on second seed"
    )


def test_admin_data_wins_on_name_conflict(clean_pg, monkeypatch):
    """Manual profile with same name as a seeded profile is NOT overwritten."""
    import src.db.seed_master_data as smd
    monkeypatch.setattr(smd, "_PROFILE_DEFS", _SYNTHETIC_PROFILES)

    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute(
            "INSERT INTO profiles (name, odoo_version, description) VALUES (%s, %s, %s)",
            ("t_root", "17.0", "MANUAL — do not overwrite"),
        )

    seed_profiles(clean_pg)

    with clean_pg.cursor() as cur:
        cur.execute(
            "SELECT description FROM profiles WHERE name = %s", ("t_root",)
        )
        assert cur.fetchone()[0] == "MANUAL — do not overwrite"


def test_seed_repos_warns_on_cross_profile_conflict(clean_pg, monkeypatch, capsys):
    """Cross-profile (url, branch) skip emits a clarifying warning on stderr."""
    import src.db.seed_master_data as smd
    monkeypatch.setattr(smd, "_PROFILE_DEFS", _SYNTHETIC_PROFILES)
    monkeypatch.setattr(smd, "_REPO_DEFS_BY_PROFILE", _SYNTHETIC_REPOS)

    run_migrations(clean_pg)
    seed_profiles(clean_pg)

    # Pre-register the same url+branch under a non-seeded profile
    with clean_pg.cursor() as cur:
        cur.execute(
            "INSERT INTO profiles (name, odoo_version, description) "
            "VALUES (%s, %s, %s) RETURNING id",
            ("legacy_consumer", "17.0", "Legacy"),
        )
        legacy_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO repos (profile_id, url, branch, local_path, clone_status) "
            "VALUES (%s, %s, %s, %s, %s)",
            (legacy_id, "git@github.com:example/base.git", "17.0", "/tmp/legacy", "manual"),
        )

    # Now seed — t_root wants the same url@branch; should skip + warn
    seed_repos(clean_pg)
    captured = capsys.readouterr()
    assert "legacy_consumer" in captured.err
    assert "t_root" in captured.err


def test_reset_seeded_data_deletes_matching_profiles(clean_pg, monkeypatch):
    """reset_seeded_data deletes prefix-matching profiles + cascades repos."""
    import src.db.seed_master_data as smd
    monkeypatch.setattr(smd, "_PROFILE_DEFS", _SYNTHETIC_PROFILES)
    monkeypatch.setattr(smd, "_REPO_DEFS_BY_PROFILE", _SYNTHETIC_REPOS)
    monkeypatch.setattr(smd, "_SEED_NAME_PATTERNS", _SYNTHETIC_PATTERNS)

    run_migrations(clean_pg)
    seed_all(clean_pg)

    # Create a non-seed profile to verify it survives reset
    with clean_pg.cursor() as cur:
        cur.execute(
            "INSERT INTO profiles (name, odoo_version, description) VALUES (%s, %s, %s)",
            ("unrelated_profile", "17.0", "Should survive"),
        )

    deleted = reset_seeded_data(clean_pg)
    assert deleted == 2  # t_root + t_child

    # Seeded profiles gone
    assert _count_profiles(clean_pg, "t\\_%") == 0

    # Seeded repos cascaded away
    with clean_pg.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM repos r "
            "JOIN profiles p ON p.id = r.profile_id "
            "WHERE p.name IN ('t_root', 't_child')"
        )
        assert cur.fetchone()[0] == 0

    # Non-seed profile survives
    with clean_pg.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM profiles WHERE name = %s", ("unrelated_profile",)
        )
        assert cur.fetchone()[0] == 1


def test_reset_seeded_data_noop_when_patterns_empty(clean_pg):
    """reset_seeded_data() with empty _SEED_NAME_PATTERNS deletes nothing."""
    run_migrations(clean_pg)
    deleted = reset_seeded_data(clean_pg)
    assert deleted == 0


def test_cli_rejects_reset_and_profiles_only_combination(clean_pg, capsys):
    """`seed-master-data --reset --profiles-only` must error out with exit 1."""
    from argparse import Namespace

    from src.manager.__main__ import _cmd_seed_master_data

    run_migrations(clean_pg)
    rc = _cmd_seed_master_data(
        Namespace(reset=True, profiles_only=True), clean_pg
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "cannot be combined" in captured.err


def test_seed_repos_clone_status_manual(clean_pg, monkeypatch):
    """All seeded repos must have clone_status='manual'."""
    import src.db.seed_master_data as smd
    monkeypatch.setattr(smd, "_PROFILE_DEFS", _SYNTHETIC_PROFILES)
    monkeypatch.setattr(smd, "_REPO_DEFS_BY_PROFILE", _SYNTHETIC_REPOS)

    run_migrations(clean_pg)
    seed_all(clean_pg)

    with clean_pg.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT clone_status FROM repos r "
            "JOIN profiles p ON p.id = r.profile_id "
            "WHERE p.name IN ('t_root', 't_child')"
        )
        statuses = {row[0] for row in cur.fetchall()}
    assert statuses == {"manual"}


def test_seed_repos_skips_missing_profile(clean_pg, monkeypatch):
    """seed_repos() silently skips profiles that don't exist in the DB."""
    import src.db.seed_master_data as smd
    # Only register repos for a profile that won't be seeded
    monkeypatch.setattr(smd, "_PROFILE_DEFS", [])  # no profiles seeded
    monkeypatch.setattr(smd, "_REPO_DEFS_BY_PROFILE", _SYNTHETIC_REPOS)

    run_migrations(clean_pg)
    inserted, skipped = seed_repos(clean_pg)
    # t_root and t_child profiles don't exist → both skipped silently
    assert inserted == 0
    assert skipped == 0


# ---------------------------------------------------------------------------
# Pure unit tests — no DB, no pytestmark override
# These tests run without the postgres fixture.
# ---------------------------------------------------------------------------

# Override the module-level mark for these specific tests
@pytest.mark.filterwarnings("ignore")
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


def test_profile_defs_no_cycles_with_synthetic():
    """_PROFILE_DEFS cycle-free guard works on synthetic fixtures."""
    profile_defs = _SYNTHETIC_PROFILES
    parent_map = {name: parent for name, _v, _d, parent in profile_defs}
    max_depth = len(profile_defs) + 1

    for name, _v, _d, _parent in profile_defs:
        visited = set()
        current = name
        depth = 0
        while current is not None and depth <= max_depth:
            assert current not in visited, (
                f"Cycle detected starting from {name!r}: visited {visited!r}, hit {current!r}"
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


def test_profile_defs_version_match_synthetic():
    """Version-match guard detects cross-version parent in synthetic defs."""
    # This should pass (both t_root and t_child are 17.0)
    version_map = {name: ver for name, ver, _d, _parent in _SYNTHETIC_PROFILES}
    for name, version, _desc, parent_name in _SYNTHETIC_PROFILES:
        if parent_name is None:
            continue
        assert parent_name in version_map
        assert version == version_map[parent_name], (
            f"{name!r} version {version!r} != parent {parent_name!r} version "
            f"{version_map[parent_name]!r}"
        )
