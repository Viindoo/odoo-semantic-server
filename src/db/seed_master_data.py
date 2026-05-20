# SPDX-License-Identifier: AGPL-3.0-or-later
# src/db/seed_master_data.py
"""Master data seeding for profiles + repos.

Seeds 26 standard profiles (Odoo CE v8-v19, Standard Viindoo v8-v19,
Viindoo Internal v17/v18) and 48 corresponding repos rows. Idempotent via
INSERT ... ON CONFLICT DO NOTHING — safe to re-run.

Profile rows are seeded by ``seed_profiles()`` only —
``migrations/0002_master_data_seed.sql`` no longer contains the INSERTs
(kept as a schema-evolution hook for the upgrade-safe
``ALTER TABLE profiles ADD COLUMN IF NOT EXISTS description``). See commit
``e029d49`` for rationale. The repos seeding lives in Python because
``repos.local_path`` depends on ``Path.home()`` at runtime (see
``src/git_utils.py::default_clone_dir``) and cannot be hardcoded in pure SQL.

Called from two places:
1. ``src/db/migrate.py::main`` — after yoyo applies migrations, invokes
   ``seed_all(conn)`` — both profiles and repos seeded by Python; the SQL
   migration only owns the upgrade-safe ``ALTER TABLE``.
2. ``python -m src.manager seed-master-data`` CLI — invokes ``seed_all`` or
   ``reset_seeded_data`` for re-seed / destructive reset.
"""

import sys

from src.git_utils import default_clone_dir

# (name, odoo_version, description, parent_name_or_None) — 26 rows total.
# Hierarchy per version:
#   odoo_N → None (root)
#   standard_viindoo_N → odoo_N
#   viindoo_internal_N → standard_viindoo_N (only v17 and v18 exist)
_PROFILE_DEFS: list[tuple[str, str, str, str | None]] = [
    ("odoo_8",  "8.0",  "Odoo CE 8.0 (Viindoo fork as canonical CE)", None),
    ("odoo_9",  "9.0",  "Odoo CE 9.0 (Viindoo fork as canonical CE)", None),
    ("odoo_10", "10.0", "Odoo CE 10.0 (Viindoo fork as canonical CE)", None),
    ("odoo_11", "11.0", "Odoo CE 11.0 (Viindoo fork as canonical CE)", None),
    ("odoo_12", "12.0", "Odoo CE 12.0 (Viindoo fork as canonical CE)", None),
    ("odoo_13", "13.0", "Odoo CE 13.0 (Viindoo fork as canonical CE)", None),
    ("odoo_14", "14.0", "Odoo CE 14.0 (Viindoo fork as canonical CE)", None),
    ("odoo_15", "15.0", "Odoo CE 15.0 (Viindoo fork as canonical CE)", None),
    ("odoo_16", "16.0", "Odoo CE 16.0 (Viindoo fork as canonical CE)", None),
    ("odoo_17", "17.0", "Odoo CE 17.0 (Viindoo fork as canonical CE)", None),
    ("odoo_18", "18.0", "Odoo CE 18.0 (Viindoo fork as canonical CE)", None),
    ("odoo_19", "19.0", "Odoo CE 19.0 (Viindoo fork as canonical CE)", None),
    ("standard_viindoo_8",  "8.0",  "Standard Viindoo 8.0 (Odoo CE + Viindoo addons)",  "odoo_8"),
    ("standard_viindoo_9",  "9.0",  "Standard Viindoo 9.0 (Odoo CE + Viindoo addons)",  "odoo_9"),
    ("standard_viindoo_10", "10.0", "Standard Viindoo 10.0 (Odoo CE + Viindoo addons)", "odoo_10"),
    ("standard_viindoo_11", "11.0", "Standard Viindoo 11.0 (Odoo CE + Viindoo addons)", "odoo_11"),
    ("standard_viindoo_12", "12.0", "Standard Viindoo 12.0 (Odoo CE + Viindoo addons)", "odoo_12"),
    ("standard_viindoo_13", "13.0", "Standard Viindoo 13.0 (Odoo CE + Viindoo addons)", "odoo_13"),
    ("standard_viindoo_14", "14.0", "Standard Viindoo 14.0 (Odoo CE + Viindoo addons)", "odoo_14"),
    ("standard_viindoo_15", "15.0", "Standard Viindoo 15.0 (Odoo CE + Viindoo addons)", "odoo_15"),
    ("standard_viindoo_16", "16.0", "Standard Viindoo 16.0 (Odoo CE + Viindoo addons)", "odoo_16"),
    ("standard_viindoo_17", "17.0", "Standard Viindoo 17.0 (Odoo CE + Viindoo addons)", "odoo_17"),
    ("standard_viindoo_18", "18.0", "Standard Viindoo 18.0 (Odoo CE + Viindoo addons)", "odoo_18"),
    ("standard_viindoo_19", "19.0", "Standard Viindoo 19.0 (Odoo CE + Viindoo addons)", "odoo_19"),
    ("viindoo_internal_17", "17.0",
     "Viindoo Internal 17.0 (Standard Viindoo + internal repos)", "standard_viindoo_17"),
    ("viindoo_internal_18", "18.0",
     "Viindoo Internal 18.0 (Standard Viindoo + internal repos)", "standard_viindoo_18"),
]

# URL convention: git@github.com:Viindoo/<repo>.git for ALL Viindoo repos
# (incl. Viindoo/odoo, which is the canonical Odoo CE fork for this deployment).
_VIINDOO_URL = "git@github.com:Viindoo/{repo}.git"

# Each value entry is (slug_hint, url, branch).
# slug_hint is informational; the actual local_path slug is derived by
# default_clone_dir() from the URL.
_REPO_DEFS_BY_PROFILE: dict[str, list[tuple[str, str, str]]] = {}


def _odoo_only(version: str) -> list[tuple[str, str, str]]:
    return [("odoo", _VIINDOO_URL.format(repo="odoo"), version)]


def _standard_viindoo(version: str) -> list[tuple[str, str, str]]:
    """Standard Viindoo DELTA repos — addons only, excluding the Odoo CE base.

    The Odoo CE base (Viindoo/odoo) is owned by the ``odoo_N`` profile because
    PostgreSQL ``UNIQUE (url, branch)`` on the ``repos`` table prevents the same
    (url, branch) pair from belonging to more than one profile. To use Standard
    Viindoo for a given version, an admin indexes BOTH ``odoo_N`` AND
    ``standard_viindoo_N``; MCP queries naturally combine them via shared
    ``odoo_version``.

    Composition rules (verified against ``gh api orgs/Viindoo/repos``):

    - ``tvtmaaddons``: all versions (v8–v19)
    - ``erponline-enterprise``: v10+ only
    - ``branding``: v13+ only
    """
    repos: list[tuple[str, str, str]] = [
        ("tvtmaaddons", _VIINDOO_URL.format(repo="tvtmaaddons"), version),
    ]
    major = int(version.split(".", 1)[0])
    if major >= 10:
        repos.append(("erponline-enterprise",
                      _VIINDOO_URL.format(repo="erponline-enterprise"), version))
    if major >= 13:
        repos.append(("branding", _VIINDOO_URL.format(repo="branding"), version))
    return repos


def _viindoo_internal(version: str) -> list[tuple[str, str, str]]:
    """Viindoo Internal DELTA repos — internal-only repos, no overlap with
    Standard Viindoo or Odoo CE.

    Same rationale as ``_standard_viindoo``: ``UNIQUE (url, branch)`` forces
    delta-only ownership. To use Viindoo Internal for a given version, an admin
    indexes ``odoo_N`` + ``standard_viindoo_N`` + ``viindoo_internal_N``.

    Internal additions (verified against GitHub):

    - ``saas-infrastructure``: v12–v18 (used for v17/v18)
    - ``saas-infrastructure-common``: v13–v18 (used for v17/v18)
    - ``themes``: v12–v17 only (NO v18 — max branch is 17.0)
    - ``odoo-api``: v13–v18 (used for v17/v18)
    """
    repos: list[tuple[str, str, str]] = []
    repos.append(("saas-infrastructure",
                  _VIINDOO_URL.format(repo="saas-infrastructure"), version))
    repos.append(("saas-infrastructure-common",
                  _VIINDOO_URL.format(repo="saas-infrastructure-common"), version))
    if version == "17.0":
        # themes max branch is 17.0 — exclude from v18
        repos.append(("themes", _VIINDOO_URL.format(repo="themes"), version))
    repos.append(("odoo-api", _VIINDOO_URL.format(repo="odoo-api"), version))
    return repos


# Build _REPO_DEFS_BY_PROFILE from rules above. Done at module-import time so
# the data structure is observable + testable without invoking helpers.
for _v in (8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19):
    _ver = f"{_v}.0"
    _REPO_DEFS_BY_PROFILE[f"odoo_{_v}"] = _odoo_only(_ver)
    _REPO_DEFS_BY_PROFILE[f"standard_viindoo_{_v}"] = _standard_viindoo(_ver)

_REPO_DEFS_BY_PROFILE["viindoo_internal_17"] = _viindoo_internal("17.0")
_REPO_DEFS_BY_PROFILE["viindoo_internal_18"] = _viindoo_internal("18.0")


# Name-prefix filter used by reset_seeded_data and CLI status counters.
_SEED_NAME_PATTERNS = ("odoo\\_%", "standard\\_viindoo\\_%", "viindoo\\_internal\\_%")


def seed_profiles(conn) -> tuple[int, int]:
    """Idempotent 2-pass INSERT + FK update for the 26 seeded profiles.

    Pass 1: INSERT all rows with parent_profile_id=NULL (ON CONFLICT DO NOTHING).
    Pass 2: for each def with a non-None parent_name, look up child_id + parent_id
            by name, then UPDATE parent_profile_id idempotently (IS DISTINCT FROM).

    Returns ``(inserted, skipped)`` where inserted + skipped == 26.

    Commits before returning when the caller passes a non-autocommit connection.
    Under ``migrate.main()`` the connection is autocommit=True and the guard is
    a no-op.
    """
    # --- Pass 1: INSERT ---------------------------------------------------
    inserted = 0
    skipped = 0
    for name, version, description, _parent in _PROFILE_DEFS:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO profiles (name, odoo_version, description) "
                "VALUES (%s, %s, %s) ON CONFLICT (name) DO NOTHING RETURNING id",
                (name, version, description),
            )
            if cur.fetchone() is not None:
                inserted += 1
            else:
                skipped += 1

    if not conn.autocommit:
        conn.commit()

    # --- Pass 2: set parent_profile_id FK ----------------------------------
    # We do a fresh import here to avoid circular-import issues; repo_store()
    # is not available at module import time (pool may not be initialised).
    # Direct SQL is simpler and avoids the pool entirely for this seed path.
    parent_updates = 0
    for name, _version, _desc, parent_name in _PROFILE_DEFS:
        if parent_name is None:
            continue

        with conn.cursor() as cur:
            cur.execute("SELECT id FROM profiles WHERE name = %s", (name,))
            child_row = cur.fetchone()
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM profiles WHERE name = %s", (parent_name,))
            parent_row = cur.fetchone()

        if child_row is None or parent_row is None:
            # Defensive: profile not present (deleted, or partial seed). Skip silently.
            continue

        child_id = child_row[0]
        parent_id = parent_row[0]

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE profiles "
                "SET parent_profile_id = %s "
                "WHERE id = %s AND parent_profile_id IS DISTINCT FROM %s",
                (parent_id, child_id, parent_id),
            )
            if cur.rowcount > 0:
                parent_updates += 1

    if not conn.autocommit:
        conn.commit()

    if parent_updates:
        print(f"✓ Updated parent_profile_id for {parent_updates} seeded profiles")

    return inserted, skipped


def seed_repos(conn) -> tuple[int, int]:
    """Idempotent INSERT for repos under each seeded profile.

    Returns ``(inserted, skipped)``. For each profile in
    ``_REPO_DEFS_BY_PROFILE``: lookup ``profile_id`` by name; if profile is
    missing, skip its repos silently (admin may have deleted the profile).
    For each repo tuple, INSERT with ``local_path =
    default_clone_dir(profile_name, url)`` and ``clone_status='manual'``.
    ON CONFLICT (url, branch) DO NOTHING — repos already registered (under
    any profile) are left alone.

    Commits before returning when the caller passes a non-autocommit
    connection, so callers cannot accidentally leave an open transaction.
    Under ``migrate.main()`` the connection is autocommit=True and the
    guard is a no-op.
    """
    inserted = 0
    skipped = 0
    for profile_name, repos in _REPO_DEFS_BY_PROFILE.items():
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM profiles WHERE name = %s", (profile_name,))
            row = cur.fetchone()
        if row is None:
            continue
        profile_id = row[0]
        for _slug, url, branch in repos:
            local_path = str(default_clone_dir(profile_name, url))
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO repos "
                    "(profile_id, url, branch, local_path, clone_status) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (url, branch) DO NOTHING RETURNING id",
                    (profile_id, url, branch, local_path, "manual"),
                )
                if cur.fetchone() is not None:
                    inserted += 1
                else:
                    skipped += 1
                    # Identify which profile already owns this (url, branch) so
                    # the admin knows why their newly-seeded profile may end up
                    # with zero repos.
                    with conn.cursor() as cur2:
                        cur2.execute(
                            "SELECT p.name FROM repos r "
                            "JOIN profiles p ON p.id = r.profile_id "
                            "WHERE r.url = %s AND r.branch = %s",
                            (url, branch),
                        )
                        row2 = cur2.fetchone()
                    if row2 is not None and row2[0] != profile_name:
                        print(
                            f"⚠ Skipping {url}@{branch} for profile '{profile_name}' — "
                            f"already registered under '{row2[0]}'.",
                            file=sys.stderr,
                        )
    if not conn.autocommit:
        conn.commit()
    return inserted, skipped


def seed_all(conn) -> dict:
    """Run ``seed_profiles`` + ``seed_repos``. Returns counts summary."""
    p_in, p_sk = seed_profiles(conn)
    r_in, r_sk = seed_repos(conn)
    return {
        "profiles_inserted": p_in,
        "profiles_skipped": p_sk,
        "repos_inserted": r_in,
        "repos_skipped": r_sk,
    }


def reset_seeded_data(conn) -> int:
    """DESTRUCTIVE: delete every profile whose name matches a seed prefix.

    ON DELETE CASCADE removes child repos. Caller MUST confirm with the user
    before invoking this — the CLI ``seed-master-data --reset`` prompts for
    ``YES`` typed input.

    Returns the number of profile rows deleted.
    """
    # Break self-FK chain so DELETEs don't trip ON DELETE RESTRICT regardless of iteration order.
    with conn.cursor() as cur:
        for pattern in _SEED_NAME_PATTERNS:
            cur.execute(
                "UPDATE profiles SET parent_profile_id = NULL WHERE name LIKE %s",
                (pattern,),
            )

    deleted = 0
    for pattern in _SEED_NAME_PATTERNS:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM profiles WHERE name LIKE %s ESCAPE '\\'",
                (pattern,),
            )
            deleted += cur.rowcount
    if not conn.autocommit:
        conn.commit()
    return deleted
