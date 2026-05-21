# SPDX-License-Identifier: AGPL-3.0-or-later
# tests/test_profile_seed_completeness.py
"""Assert that seed_all() on empty defaults produces zero rows.

With the open-core release _PROFILE_DEFS is empty — no profiles are seeded by
default. These tests document and guard that state. When a deployment populates
_PROFILE_DEFS, they should replace or extend this file accordingly.

Requires PostgreSQL (marked ``postgres``). Skip with ``-m 'not postgres'``.
"""

import pytest

from src.db.migrate import run_migrations
from src.db.seed_master_data import _PROFILE_DEFS, _REPO_DEFS_BY_PROFILE, seed_all

pytestmark = pytest.mark.postgres


def test_seed_all_produces_no_profiles_by_default(clean_pg):
    """seed_all() on default empty defs inserts zero profile rows."""
    run_migrations(clean_pg)
    summary = seed_all(clean_pg)
    assert summary["profiles_inserted"] == 0
    assert summary["profiles_skipped"] == 0


def test_seed_all_produces_no_repos_by_default(clean_pg):
    """seed_all() on default empty defs inserts zero repo rows."""
    run_migrations(clean_pg)
    seed_all(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM repos")
        assert cur.fetchone()[0] == 0


def test_profile_defs_empty(clean_pg):
    """_PROFILE_DEFS is empty in the open-core release — no built-in profiles."""
    assert _PROFILE_DEFS == []


def test_repo_defs_empty(clean_pg):
    """_REPO_DEFS_BY_PROFILE is empty in the open-core release — no built-in repos."""
    assert _REPO_DEFS_BY_PROFILE == {}


def test_seed_all_idempotent_on_empty(clean_pg):
    """Calling seed_all() twice on empty defs produces consistent all-zero counts."""
    run_migrations(clean_pg)
    first = seed_all(clean_pg)
    second = seed_all(clean_pg)
    assert first == second == {
        "profiles_inserted": 0,
        "profiles_skipped": 0,
        "repos_inserted": 0,
        "repos_skipped": 0,
    }


def test_seed_all_with_synthetic_fixtures(clean_pg, monkeypatch):
    """Mechanism smoke test: seed_all() with synthetic fixtures inserts the right counts.

    This test verifies the seeding machinery works end-to-end. It uses
    monkeypatched generic fixtures unrelated to any specific deployment.
    """
    import src.db.seed_master_data as smd

    monkeypatch.setattr(smd, "_PROFILE_DEFS", [
        ("acme_root", "17.0", "Acme root 17.0", None),
        ("acme_ext",  "17.0", "Acme ext 17.0",  "acme_root"),
    ])
    monkeypatch.setattr(smd, "_REPO_DEFS_BY_PROFILE", {
        "acme_root": [("base", "git@github.com:acme/base.git", "17.0")],
        "acme_ext":  [("ext",  "git@github.com:acme/ext.git",  "17.0")],
    })

    run_migrations(clean_pg)
    summary = smd.seed_all(clean_pg)

    assert summary["profiles_inserted"] == 2
    assert summary["repos_inserted"] == 2

    # Parent FK set correctly
    with clean_pg.cursor() as cur:
        cur.execute(
            "SELECT p_parent.name FROM profiles p_child "
            "JOIN profiles p_parent ON p_parent.id = p_child.parent_profile_id "
            "WHERE p_child.name = 'acme_ext'"
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "acme_root"
