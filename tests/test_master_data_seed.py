# tests/test_master_data_seed.py
"""Tests for master data seeding (profiles + repos).

Uses the session-scoped pg_conn + per-test clean_pg fixture to wipe schema
tables (including yoyo state) before/after each test. ``run_migrations()``
re-creates the schema (migration 0002 only contains an idempotent
``ALTER TABLE``). Tests then call ``seed_profiles``/``seed_repos``/``seed_all``
explicitly to drive the master-data seeding code path.
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

pytestmark = pytest.mark.postgres


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
    #   v8-v9   → 1 (acme_addons)
    #   v10-v12 → 2 (+acme_enterprise)
    #   v13-v19 → 3 (+branding)
    for v in (8, 9):
        assert _count_repos_for_profile(clean_pg, f"standard_profile_{v}") == 1
    for v in (10, 11, 12):
        assert _count_repos_for_profile(clean_pg, f"standard_profile_{v}") == 2
    for v in (13, 14, 15, 16, 17, 18, 19):
        assert _count_repos_for_profile(clean_pg, f"standard_profile_{v}") == 3
    # Viindoo Internal (delta — internal repos only):
    #   v17 → 4 (saas-infra, saas-infra-common, themes, acme_api)
    #   v18 → 3 (no themes — max branch is 17.0)
    assert _count_repos_for_profile(clean_pg, "internal_profile_17") == 4
    assert _count_repos_for_profile(clean_pg, "internal_profile_18") == 3


def test_internal_profile_18_excludes_themes(clean_pg):
    """themes max branch is 17.0 → internal_profile_18 must not include themes."""
    run_migrations(clean_pg)
    seed_all(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute(
            "SELECT r.url FROM repos r "
            "JOIN profiles p ON p.id = r.profile_id "
            "WHERE p.name = %s",
            ("internal_profile_18",),
        )
        urls = [row[0] for row in cur.fetchall()]
    assert all("acme_themes" not in url for url in urls), urls
    # v17 should include themes (positive control)
    with clean_pg.cursor() as cur:
        cur.execute(
            "SELECT r.url FROM repos r "
            "JOIN profiles p ON p.id = r.profile_id "
            "WHERE p.name = %s",
            ("internal_profile_17",),
        )
        urls17 = [row[0] for row in cur.fetchall()]
    assert any("acme_themes" in url for url in urls17), urls17


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
    # Create a manual profile of the same name BEFORE seed runs.
    with clean_pg.cursor() as cur:
        cur.execute(
            "INSERT INTO profiles (name, odoo_version, description) VALUES (%s, %s, %s)",
            ("odoo_17", "17.0", "MANUAL — do not overwrite"),
        )
    # Run the seed — odoo_17 already exists → INSERT skipped → manual row preserved
    seed_profiles(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("SELECT description FROM profiles WHERE name = %s", ("odoo_17",))
        assert cur.fetchone()[0] == "MANUAL — do not overwrite"


def test_seed_repos_warns_on_cross_profile_conflict(clean_pg, capsys):
    """Cross-profile (url, branch) skip emits a clarifying warning."""
    run_migrations(clean_pg)
    # Pre-register Viindoo/odoo @ 17.0 under a non-seed profile name
    seed_profiles(clean_pg)  # ensures odoo_17 row exists
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
            (legacy_id, "git@github.com:Viindoo/odoo.git", "17.0", "/tmp/legacy", "manual"),
        )
    # Now seed — odoo_17 wants Viindoo/odoo@17.0 too; should skip + warn
    seed_repos(clean_pg)
    captured = capsys.readouterr()
    assert "legacy_consumer" in captured.err
    assert "odoo_17" in captured.err


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


def test_cli_rejects_reset_and_profiles_only_combination(clean_pg, capsys):
    """`seed-master-data --reset --profiles-only` must error out (Opus MED fix).

    Combining the flags would CASCADE-delete child repos then skip re-seeding
    them — silent foot-gun. The CLI rejects the combination with exit 1.
    """
    from argparse import Namespace

    from src.manager.__main__ import _cmd_seed_master_data

    run_migrations(clean_pg)
    rc = _cmd_seed_master_data(
        Namespace(reset=True, profiles_only=True), clean_pg
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "cannot be combined" in captured.err
    # DB state unchanged (no DELETE, no INSERT)
    assert _count_seeded_profiles(clean_pg) == 0


@pytest.mark.parametrize(
    "profile_name,expected_url_substring",
    [
        ("odoo_17",              "Viindoo/odoo.git"),
        ("standard_profile_13",  "example/branding-repo.git"),
        ("internal_profile_17",  "Viindoo/acme_infra.git"),
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


# ---------------------------------------------------------------------------
# M8 — Parent FK seed tests
# ---------------------------------------------------------------------------

def test_seed_sets_parent_fk(clean_pg):
    """After seed_profiles(), internal_profile_17.parent_profile_id → standard_profile_17."""
    run_migrations(clean_pg)
    seed_profiles(clean_pg)

    with clean_pg.cursor() as cur:
        cur.execute(
            "SELECT p_child.parent_profile_id, p_parent.name "
            "FROM profiles p_child "
            "JOIN profiles p_parent ON p_parent.id = p_child.parent_profile_id "
            "WHERE p_child.name = %s",
            ("internal_profile_17",),
        )
        row = cur.fetchone()

    assert row is not None, "internal_profile_17 must have a parent_profile_id set"
    assert row[1] == "standard_profile_17", (
        f"expected parent standard_profile_17, got {row[1]}"
    )


def test_seed_parent_chain_standard_to_odoo(clean_pg):
    """standard_profile_17.parent_profile_id → odoo_17 after seed."""
    run_migrations(clean_pg)
    seed_profiles(clean_pg)

    with clean_pg.cursor() as cur:
        cur.execute(
            "SELECT p_parent.name "
            "FROM profiles p_child "
            "JOIN profiles p_parent ON p_parent.id = p_child.parent_profile_id "
            "WHERE p_child.name = %s",
            ("standard_profile_17",),
        )
        row = cur.fetchone()

    assert row is not None
    assert row[0] == "odoo_17"


def test_seed_idempotent_when_parent_already_set(clean_pg):
    """Second seed_profiles() call is a no-op — does not change parent_profile_id."""
    run_migrations(clean_pg)
    seed_profiles(clean_pg)

    # Record parent FK before second seed
    with clean_pg.cursor() as cur:
        cur.execute(
            "SELECT parent_profile_id FROM profiles WHERE name = %s",
            ("internal_profile_17",),
        )
        parent_id_before = cur.fetchone()[0]

    # Second seed
    seed_profiles(clean_pg)

    with clean_pg.cursor() as cur:
        cur.execute(
            "SELECT parent_profile_id FROM profiles WHERE name = %s",
            ("internal_profile_17",),
        )
        parent_id_after = cur.fetchone()[0]

    assert parent_id_before == parent_id_after, (
        "parent_profile_id must not change on second seed"
    )


# ---------------------------------------------------------------------------
# Fix-I: import-time CI guards on _PROFILE_DEFS
# Pure unit tests — no DB needed, no pytestmark = pytest.mark.postgres.
# ---------------------------------------------------------------------------

def test_profile_defs_no_cycles():
    """_PROFILE_DEFS parent chain must be cycle-free.

    For each entry, follow parent_name upward; if any name is revisited,
    the definition has a cycle.  Uses a chain length limit of
    len(_PROFILE_DEFS) + 1 to detect infinite loops.
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

    This mirrors the runtime check in _validate_parent so that a careless
    edit to _PROFILE_DEFS fails fast in CI rather than silently creating
    cross-version parent links at deploy time.
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
