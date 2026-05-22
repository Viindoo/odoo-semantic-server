# SPDX-License-Identifier: AGPL-3.0-or-later
# src/db/seed_master_data.py
"""Master data seeding for profiles + repos.

No master data is seeded by default — profiles and repos are created by an
admin via the web UI (/admin) or the JSON API. The seed_* functions remain as
an idempotent extension point operating on _PROFILE_DEFS / _REPO_DEFS_BY_PROFILE
(empty by default).

Technical notes:

``seed_profiles()`` uses a 2-pass approach:
  Pass 1: INSERT all rows with parent_profile_id=NULL (ON CONFLICT DO NOTHING).
  Pass 2: for each def with a non-None parent_name, look up child_id + parent_id
          by name, then UPDATE parent_profile_id idempotently (IS DISTINCT FROM).
This 2-pass pattern is required to resolve self-referential FK without ordering
constraints on the input list.

``seed_repos()`` iterates _REPO_DEFS_BY_PROFILE; for each profile, looks up
profile_id by name and INSERTs repos with ``local_path =
default_clone_dir(profile_name, url)`` and ``clone_status='manual'``.
ON CONFLICT (url, branch, profile_id) DO NOTHING — a repo already registered
under the SAME profile is left alone (ADR-0034 D2 allows the same upstream
url+branch under different profiles).

``repos.local_path`` depends on ``Path.home()`` at runtime (see
``src/git_utils.py::default_clone_dir``) and cannot be hardcoded in pure SQL.

Called from two places:
1. ``src/db/migrate.py::main`` — after yoyo applies migrations, invokes
   ``seed_all(conn)`` — both profiles and repos seeded by Python; the SQL
   migration only owns the upgrade-safe ``ALTER TABLE``.
2. ``python -m src.manager seed-master-data`` CLI — invokes ``seed_all`` or
   ``reset_seeded_data`` for re-seed / destructive reset.
"""

from src.git_utils import default_clone_dir

# (name, odoo_version, description, parent_name_or_None)
# Empty by default — populate to enable bulk seeding via seed_all().
_PROFILE_DEFS: list[tuple[str, str, str, str | None]] = []

# Each value entry is (slug_hint, url, branch).
# slug_hint is informational; the actual local_path slug is derived by
# default_clone_dir() from the URL.
# Empty by default — populate in tandem with _PROFILE_DEFS.
_REPO_DEFS_BY_PROFILE: dict[str, list[tuple[str, str, str]]] = {}

# Name-prefix patterns used by reset_seeded_data (LIKE escape syntax).
# Empty tuple → reset_seeded_data() is a no-op on the default empty seed.
_SEED_NAME_PATTERNS: tuple[str, ...] = ()


def seed_profiles(conn) -> tuple[int, int]:
    """Idempotent 2-pass INSERT + FK update for the seeded profiles.

    Pass 1: INSERT all rows with parent_profile_id=NULL (ON CONFLICT DO NOTHING).
    Pass 2: for each def with a non-None parent_name, look up child_id + parent_id
            by name, then UPDATE parent_profile_id idempotently (IS DISTINCT FROM).

    Returns ``(inserted, skipped)`` where inserted + skipped == len(_PROFILE_DEFS).

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
    ON CONFLICT (url, branch, profile_id) DO NOTHING — a repo already
    registered under the SAME profile is left alone (ADR-0034 D2).

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
                    "ON CONFLICT (url, branch, profile_id) DO NOTHING RETURNING id",
                    (profile_id, url, branch, local_path, "manual"),
                )
                if cur.fetchone() is not None:
                    inserted += 1
                else:
                    # Same (url, branch) already registered under THIS profile
                    # (idempotent re-seed). Cross-profile duplicates are allowed
                    # per ADR-0034 D2 and INSERT a separate row rather than skip.
                    skipped += 1
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
