# tests/test_profile_seed_completeness.py
"""OBS-1: Assert that all expected Odoo CE profile versions are present after seeding.

After migration + seed_all(), the ``odoo_N`` profiles for every version in
8–17 and 19 (v18 deferred — repo missing from disk) must exist. This test
serves as a regression guard so future seed edits cannot silently drop a
version.

Requires PostgreSQL (marked ``postgres``). Skip with ``-m 'not postgres'``.
"""

import pytest

from src.db.migrate import run_migrations
from src.db.seed_master_data import seed_all

pytestmark = pytest.mark.postgres

# Versions that must be present after seed_all().
# v18 is intentionally excluded from REQUIRED_VERSIONS — repo missing on disk (OBS-1 skip).
# Update this list when v18 is added (see coverage-report.md, OBS-1 deferred ticket).
REQUIRED_ODOO_VERSIONS = {
    "8.0", "9.0", "10.0", "11.0", "12.0",
    "13.0", "14.0", "15.0", "16.0", "17.0",
    "19.0",
}

# The canonical profile names that correspond to each required version.
REQUIRED_PROFILE_NAMES = {f"odoo_{v.split('.')[0]}" for v in REQUIRED_ODOO_VERSIONS}


def _get_seeded_profile_names(conn) -> set[str]:
    """Return the set of profile names whose name starts with 'odoo_' from the DB."""
    with conn.cursor() as cur:
        cur.execute(
            r"SELECT name FROM profiles WHERE name LIKE 'odoo\_%' ESCAPE '\'"
        )
        return {row[0] for row in cur.fetchall()}


def test_all_required_odoo_profiles_present_after_seed(clean_pg):
    """After run_migrations + seed_all, all required odoo_N profiles must exist."""
    run_migrations(clean_pg)
    seed_all(clean_pg)

    present = _get_seeded_profile_names(clean_pg)
    missing = REQUIRED_PROFILE_NAMES - present

    assert not missing, (
        f"Missing odoo_N profiles after seed: {sorted(missing)}\n"
        f"Present: {sorted(present)}\n"
        "If v18 is now available, add '18.0' to REQUIRED_ODOO_VERSIONS and "
        "remove the OBS-1 deferred note."
    )


def test_v18_profile_intentionally_absent_or_present(clean_pg):
    """Soft check: v18 is absent because its repo is not yet on disk (OBS-1 deferred).

    This test passes whether odoo_18 is present or absent — it is purely
    documentation. When v18 repo is added, move '18.0' to REQUIRED_ODOO_VERSIONS
    in this file and delete this test.
    """
    run_migrations(clean_pg)
    seed_all(clean_pg)

    with clean_pg.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM profiles WHERE name = 'odoo_18'")
        count = cur.fetchone()[0]

    # We record the state but do not assert absence or presence — just document.
    assert count in (0, 1), f"Unexpected count for odoo_18: {count}"


def test_required_profiles_have_correct_odoo_version(clean_pg):
    """Each required odoo_N profile must store the matching odoo_version string."""
    run_migrations(clean_pg)
    seed_all(clean_pg)

    with clean_pg.cursor() as cur:
        cur.execute(
            r"SELECT name, odoo_version FROM profiles WHERE name LIKE 'odoo\_%' ESCAPE '\'"
        )
        rows = {row[0]: row[1] for row in cur.fetchall()}

    mismatches = []
    for major_str in [v.split(".")[0] for v in REQUIRED_ODOO_VERSIONS]:
        profile_name = f"odoo_{major_str}"
        expected_version = f"{major_str}.0"
        actual = rows.get(profile_name)
        if actual != expected_version:
            mismatches.append(
                f"{profile_name}: expected odoo_version={expected_version!r}, "
                f"got {actual!r}"
            )

    assert not mismatches, "odoo_version mismatches:\n" + "\n".join(mismatches)


def test_required_profiles_are_root_profiles(clean_pg):
    """All odoo_N profiles must be root profiles (parent_profile_id IS NULL).

    Per ADR-0016: odoo_N profiles are the root tier; standard_viindoo_N
    and viindoo_internal_N are children. No odoo_N should have a parent.
    """
    run_migrations(clean_pg)
    seed_all(clean_pg)

    with clean_pg.cursor() as cur:
        cur.execute(
            r"SELECT name, parent_profile_id "
            r"FROM profiles "
            r"WHERE name LIKE 'odoo\_%' ESCAPE '\' "
            r"  AND parent_profile_id IS NOT NULL"
        )
        bad_rows = cur.fetchall()

    assert not bad_rows, (
        f"odoo_N profiles with unexpected parent_profile_id: {bad_rows}"
    )
