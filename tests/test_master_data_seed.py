# tests/test_master_data_seed.py
"""Tests for master data seeding (profiles + repos).

Uses the session-scoped pg_conn + per-test clean_pg fixture to wipe schema
tables (including yoyo state) before/after each test. run_migrations() then
re-creates schema + applies migration 0002 (which seeds profiles). seed_repos()
fills the repos table with default_clone_dir-derived local paths.
"""

import pytest

from src.db.migrate import run_migrations
from src.db.seed_master_data import (
    _PROFILE_DEFS,
    _REPO_DEFS_BY_PROFILE,
    reset_seeded_data,
    seed_all,
    seed_profiles,
    seed_repos,
)


def _count_seeded_profiles(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM profiles "
            r"WHERE name LIKE 'odoo\_%' ESCAPE '\' "
            r"   OR name LIKE 'standard\_viindoo\_%' ESCAPE '\' "
            r"   OR name LIKE 'viindoo\_internal\_%' ESCAPE '\'"
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


def test_seed_profiles_inserts_26(clean_pg):
    """seed_profiles() inserts the 26 master data profiles; idempotent."""
    run_migrations(clean_pg)
    inserted, skipped = seed_profiles(clean_pg)
    assert inserted == 26
    assert skipped == 0
    assert _count_seeded_profiles(clean_pg) == 26
    # Re-run is a no-op
    inserted2, skipped2 = seed_profiles(clean_pg)
    assert inserted2 == 0
    assert skipped2 == 26
    # Sanity: _PROFILE_DEFS matches
    assert len(_PROFILE_DEFS) == 26


def test_seed_repos_inserts_48_total(clean_pg):
    """seed_repos() inserts the expected 48 repos across all 26 profiles.

    Delta-only model: each profile owns only repos NOT present in a lower tier.
    Odoo CE owns Viindoo/odoo; Standard Viindoo owns addons only; Viindoo
    Internal owns internal-only repos. PostgreSQL ``UNIQUE (url, branch)`` on
    ``repos`` enforces this.
    """
    run_migrations(clean_pg)
    seed_profiles(clean_pg)  # FK requires profile rows
    inserted, skipped = seed_repos(clean_pg)
    assert inserted == 48
    assert skipped == 0
    # Sanity against the data definition
    assert sum(len(v) for v in _REPO_DEFS_BY_PROFILE.values()) == 48


def test_seed_all_idempotent(clean_pg):
    """Calling seed_all() twice does not duplicate rows."""
    run_migrations(clean_pg)
    first = seed_all(clean_pg)
    second = seed_all(clean_pg)
    # First call: empty schema → everything inserts
    assert first["profiles_inserted"] == 26
    assert first["profiles_skipped"] == 0
    assert first["repos_inserted"] == 48
    assert first["repos_skipped"] == 0
    # Second call: everything already present → all skipped
    assert second["profiles_inserted"] == 0
    assert second["profiles_skipped"] == 26
    assert second["repos_inserted"] == 0
    assert second["repos_skipped"] == 48
    # No duplicates: row counts unchanged
    assert _count_seeded_profiles(clean_pg) == 26
    with clean_pg.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM repos")
        assert cur.fetchone()[0] == 48


def test_seeded_profile_repo_counts_match_matrix(clean_pg):
    """Each profile tier has the right delta-only repo count per the design matrix."""
    run_migrations(clean_pg)
    seed_all(clean_pg)
    # Odoo CE: 1 repo (Viindoo/odoo) per version
    for v in range(8, 20):
        assert _count_repos_for_profile(clean_pg, f"odoo_{v}") == 1, f"odoo_{v}"
    # Standard Viindoo (delta — addons only; Odoo CE base lives under odoo_N):
    #   v8-v9   → 1 (tvtmaaddons)
    #   v10-v12 → 2 (+erponline-enterprise)
    #   v13-v19 → 3 (+branding)
    for v in (8, 9):
        assert _count_repos_for_profile(clean_pg, f"standard_viindoo_{v}") == 1
    for v in (10, 11, 12):
        assert _count_repos_for_profile(clean_pg, f"standard_viindoo_{v}") == 2
    for v in (13, 14, 15, 16, 17, 18, 19):
        assert _count_repos_for_profile(clean_pg, f"standard_viindoo_{v}") == 3
    # Viindoo Internal (delta — internal repos only):
    #   v17 → 4 (saas-infra, saas-infra-common, themes, odoo-api)
    #   v18 → 3 (no themes — max branch is 17.0)
    assert _count_repos_for_profile(clean_pg, "viindoo_internal_17") == 4
    assert _count_repos_for_profile(clean_pg, "viindoo_internal_18") == 3


def test_viindoo_internal_18_excludes_themes(clean_pg):
    """themes max branch is 17.0 → viindoo_internal_18 must not include themes."""
    run_migrations(clean_pg)
    seed_all(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute(
            "SELECT r.url FROM repos r "
            "JOIN profiles p ON p.id = r.profile_id "
            "WHERE p.name = %s",
            ("viindoo_internal_18",),
        )
        urls = [row[0] for row in cur.fetchall()]
    assert all("themes" not in url for url in urls), urls
    # v17 should include themes (positive control)
    with clean_pg.cursor() as cur:
        cur.execute(
            "SELECT r.url FROM repos r "
            "JOIN profiles p ON p.id = r.profile_id "
            "WHERE p.name = %s",
            ("viindoo_internal_17",),
        )
        urls17 = [row[0] for row in cur.fetchall()]
    assert any("themes" in url for url in urls17), urls17


def test_seeded_repos_clone_status_manual(clean_pg):
    """All seeded repos must have clone_status='manual'."""
    run_migrations(clean_pg)
    seed_all(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT clone_status FROM repos r "
            "JOIN profiles p ON p.id = r.profile_id "
            r"WHERE p.name LIKE 'odoo\_%' ESCAPE '\' "
            r"   OR p.name LIKE 'standard\_viindoo\_%' ESCAPE '\' "
            r"   OR p.name LIKE 'viindoo\_internal\_%' ESCAPE '\'"
        )
        statuses = {row[0] for row in cur.fetchall()}
    assert statuses == {"manual"}


def test_admin_data_wins_on_name_conflict(clean_pg):
    """Manual profile created before seed is NOT overwritten — admin data wins."""
    run_migrations(clean_pg)
    # Drop the auto-seeded odoo_17 and replace with a manual profile of the same name
    # but different description.
    with clean_pg.cursor() as cur:
        cur.execute("DELETE FROM profiles WHERE name = %s", ("odoo_17",))
        cur.execute(
            "INSERT INTO profiles (name, odoo_version, description) VALUES (%s, %s, %s)",
            ("odoo_17", "17.0", "MANUAL — do not overwrite"),
        )
    # Re-seed
    seed_profiles(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("SELECT description FROM profiles WHERE name = %s", ("odoo_17",))
        assert cur.fetchone()[0] == "MANUAL — do not overwrite"


def test_reset_seeded_data_deletes_only_seeded_profiles(clean_pg):
    """reset_seeded_data deletes prefix-matching profiles + cascades repos.

    Profiles not matching the seed prefixes (e.g. legacy 'viindoo17' without
    underscore from the apply-preset CLI) MUST be preserved.
    """
    run_migrations(clean_pg)
    seed_all(clean_pg)
    # Manually create a non-seed profile to verify it survives reset
    with clean_pg.cursor() as cur:
        cur.execute(
            "INSERT INTO profiles (name, odoo_version, description) VALUES (%s, %s, %s)",
            ("viindoo17", "17.0", "Legacy preset profile"),
        )
    assert _count_seeded_profiles(clean_pg) == 26
    deleted = reset_seeded_data(clean_pg)
    assert deleted == 26
    assert _count_seeded_profiles(clean_pg) == 0
    # All seeded repos cascaded away
    with clean_pg.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM repos")
        assert cur.fetchone()[0] == 0
    # Legacy non-seed profile survives
    with clean_pg.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM profiles WHERE name = %s", ("viindoo17",)
        )
        assert cur.fetchone()[0] == 1


@pytest.mark.parametrize(
    "profile_name,expected_url_substring",
    [
        ("odoo_17",              "Viindoo/odoo.git"),
        ("standard_viindoo_13",  "Viindoo/branding.git"),
        ("viindoo_internal_17",  "Viindoo/saas-infrastructure.git"),
    ],
)
def test_seed_repos_uses_viindoo_ssh_url(clean_pg, profile_name, expected_url_substring):
    """All seeded repos must use git@github.com:Viindoo/* SSH URLs."""
    run_migrations(clean_pg)
    seed_all(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute(
            "SELECT r.url FROM repos r JOIN profiles p ON p.id = r.profile_id "
            "WHERE p.name = %s",
            (profile_name,),
        )
        urls = [row[0] for row in cur.fetchall()]
    assert all(url.startswith("git@github.com:Viindoo/") for url in urls), urls
    assert any(expected_url_substring in url for url in urls), (
        f"{profile_name} missing {expected_url_substring} in {urls}"
    )
