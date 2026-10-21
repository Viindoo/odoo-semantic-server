# SPDX-License-Identifier: AGPL-3.0-or-later
# src/db/repo_registry.py
"""CRUD for profiles + repos in PostgreSQL."""
import re
from pathlib import Path

import psycopg2.errors

from src.constants import WEBUI_LIFECYCLE_LOCK_WAIT_SECONDS
from src.db.exceptions import (
    ProfileCycleError,
    ProfileIndexedError,
    ProfileInputError,
    ProfileNameConflictError,
    ProfileNotFoundError,
    ProfileTenantRequiredError,
    ProfileVersionMismatchError,
    ProfileVisibilityError,
    RepoConflictError,
    RepoNotFoundError,
)
from src.db.pg import PgPool

# Profile name: alphanumeric + underscore, 1-50 chars (shell-safe, readable;
# also a Neo4j ``profile[]`` value and a comma-free RLS GUC list element).
# Anchored with ``\Z``, not ``$``: ``$`` also matches before a final "\n", so
# ``.match`` would accept "foo\n". Both patterns are used with ``.match``.
PROFILE_NAME_RE = re.compile(r"^[a-zA-Z0-9_]{1,50}\Z")
# Odoo version: N.N (e.g. 17.0, 8.0, 20.0). Major.minor only, the registry
# convention; the Odoo series list itself is data (see versions route).
ODOO_VERSION_RE = re.compile(r"^\d{1,2}\.\d+\Z")


def validate_profile_identity(name: str, odoo_version: str) -> None:
    """Raise :class:`ProfileInputError` unless *name* / *odoo_version* are well formed.

    The single validation the Web UI, the CLI and :meth:`RepoStore.add_profile`
    share, so a profile created by any path has the same shape.
    """
    if not isinstance(name, str) or not PROFILE_NAME_RE.match(name):
        raise ProfileInputError(
            f"profile name {name!r} invalid: 1-50 chars, letters, digits and"
            " underscore only (e.g. 'odoo_17', 'viindoo_internal_18')"
        )
    if not isinstance(odoo_version, str) or not ODOO_VERSION_RE.match(odoo_version):
        raise ProfileInputError(
            f"Odoo version {odoo_version!r} invalid: required format N.N"
            " (e.g. 17.0, 20.0)"
        )


def check_child_visibility(
    child_tenant_id: int | None,
    parent_tenant_id: int | None,
    *,
    child: str = "profile",
    parent: str = "parent profile",
) -> None:
    """The ONE visibility invariant: a child is never more visible than its parent.

    - parent shared (``tenant_id IS NULL``): any child is fine (shared, or
      private to any tenant - the normal ``odoo_17`` -> ``customer_17`` shape);
    - parent private to tenant T: the child must be private to T too. A shared
      child would publish a private chain's name and repos to every key, and a
      child of another tenant would let that tenant extend a chain it cannot see.

    Raises :class:`ProfileVisibilityError`; pure (no I/O).
    """
    if parent_tenant_id is None or child_tenant_id == parent_tenant_id:
        return
    shown = "shared (visible to every key)" if child_tenant_id is None else (
        f"tenant {child_tenant_id}"
    )
    raise ProfileVisibilityError(
        f"{child} would be more visible than {parent}: {parent} is private to"
        f" tenant {parent_tenant_id}, {child} would be {shown}. A profile under"
        f" a private parent must belong to the same tenant."
    )


def resolve_new_profile_tenant(
    *,
    parent_tenant_id: int | None,
    has_parent: bool,
    tenant_id: int | None,
    shared: bool,
) -> int | None:
    """Tenant of a profile being created - fail-closed (pure, no I/O).

    - ``tenant_id`` and ``shared`` together -> :class:`ProfileInputError`;
    - parent private to T: inherit T (an explicit ``tenant_id`` must be T,
      ``shared`` is refused) - :func:`check_child_visibility`;
    - otherwise (no parent, or a shared parent): ``tenant_id`` is required,
      unless ``shared=True`` says, explicitly, "visible to every key".
      Omitting both raises :class:`ProfileTenantRequiredError`.
    """
    if shared and tenant_id is not None:
        raise ProfileInputError("pass either tenant_id or shared=true, not both")
    if has_parent and parent_tenant_id is not None:
        if shared:
            check_child_visibility(None, parent_tenant_id)
        if tenant_id is None:
            return parent_tenant_id
        check_child_visibility(tenant_id, parent_tenant_id)
        return tenant_id
    if tenant_id is not None:
        return tenant_id
    if shared:
        return None
    raise ProfileTenantRequiredError(
        "tenant_id is required: choose the tenant that owns this profile, pick a"
        " private parent profile (its tenant is inherited), or pass shared=true"
        " to publish the profile to every key (Odoo CE/EE base profiles only)"
    )


# Sentinel: "derive the repo's tenant from its profile" (RepoStore.add_repo).
_INHERIT_TENANT = object()

# RepoStore.visibility_violations - read-only. ``chain`` walks every profile's
# ancestors with their depth; the NEAREST private ancestor decides (a shared
# profile above a private one does not relax it). Depth is capped as a guard;
# _validate_parent keeps the hierarchy acyclic.
_VISIBILITY_AUDIT_SQL = """
WITH RECURSIVE chain AS (
    SELECT p.id AS leaf_id, p.parent_profile_id AS anc_id, 1 AS depth
    FROM profiles p WHERE p.parent_profile_id IS NOT NULL
    UNION ALL
    SELECT c.leaf_id, a.parent_profile_id, c.depth + 1
    FROM chain c JOIN profiles a ON a.id = c.anc_id
    WHERE a.parent_profile_id IS NOT NULL AND c.depth < 64
),
nearest_private AS (
    SELECT DISTINCT ON (c.leaf_id) c.leaf_id, a.name AS anc_name,
           a.tenant_id AS anc_tenant
    FROM chain c JOIN profiles a ON a.id = c.anc_id
    WHERE a.tenant_id IS NOT NULL
    ORDER BY c.leaf_id, c.depth
)
SELECT CASE WHEN p.tenant_id IS NULL THEN 'shared_child_of_private'
            ELSE 'cross_tenant_chain' END AS kind,
       p.id AS profile_id, p.name AS profile_name, p.tenant_id,
       np.anc_name AS ancestor_name, np.anc_tenant AS ancestor_tenant_id,
       NULL::integer AS repo_id
FROM nearest_private np JOIN profiles p ON p.id = np.leaf_id
WHERE p.tenant_id IS DISTINCT FROM np.anc_tenant
UNION ALL
SELECT 'repo_tenant_mismatch', p.id, p.name, p.tenant_id, NULL, r.tenant_id, r.id
FROM repos r JOIN profiles p ON p.id = r.profile_id
WHERE r.tenant_id IS DISTINCT FROM p.tenant_id
ORDER BY 1, 3, 7
"""


# Sentinel: distinguishes "profile row missing" from "profile exists, tenant_id IS NULL".
# Used by RepoStore.get_profile_tenant_id as a typed missing-row indicator.
#
# PUBLIC: this sentinel is part of get_profile_tenant_id's return contract —
# callers in other modules compare against it (``is PROFILE_MISSING``), so it is
# intentionally exported without an underscore prefix.
class _ProfileMissing:
    """Singleton sentinel returned when no profiles row matches a name lookup."""
    def __repr__(self) -> str:
        return "PROFILE_MISSING"


PROFILE_MISSING = _ProfileMissing()


class RepoStore:
    def __init__(self, pool: PgPool) -> None:
        self._pool = pool

    def add_profile(
        self,
        name: str,
        odoo_version: str,
        description: str = "",
        *,
        parent_id: int | None = None,
        tenant_id: int | None = None,
        shared: bool = False,
    ) -> int:
        """Insert a new profile and return its id - tenant fail-closed.

        The tenant is resolved by :func:`resolve_new_profile_tenant`: inherited
        from a private parent, else *tenant_id* is required unless *shared* is
        set explicitly. Name and version are validated by
        :func:`validate_profile_identity`.

        Parent existence, version match, the visibility invariant and the INSERT
        run in ONE transaction with the parent row locked (``FOR SHARE``), so a
        concurrent tenant change of the parent cannot slip between the check and
        the write, and a failed validation leaves no orphan row.

        Raises:
            ProfileInputError - bad name/version, tenant_id and shared together,
                or tenant_id does not exist.
            ProfileTenantRequiredError - no tenant, no private parent, no shared.
            ProfileVisibilityError - the child would be more visible than its parent.
            ValueError - parent not found, version mismatch, or name taken.
        """
        validate_profile_identity(name, odoo_version)
        with self._pool.checkout() as conn:
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    parent_tenant_id = None
                    if parent_id is not None:
                        cur.execute(
                            "SELECT odoo_version, tenant_id FROM profiles"
                            " WHERE id = %s FOR SHARE",
                            (parent_id,),
                        )
                        parent_row = cur.fetchone()
                        if parent_row is None:
                            raise ValueError(f"parent profile id={parent_id} not found")
                        parent_version, parent_tenant_id = parent_row
                        if parent_version != odoo_version:
                            raise ValueError(
                                f"version mismatch: child odoo_version={odoo_version!r} != "
                                f"parent odoo_version={parent_version!r}"
                            )
                    resolved_tenant = resolve_new_profile_tenant(
                        parent_tenant_id=parent_tenant_id,
                        has_parent=parent_id is not None,
                        tenant_id=tenant_id,
                        shared=shared,
                    )
                    cur.execute(
                        "INSERT INTO profiles "
                        "(name, odoo_version, description, parent_profile_id, tenant_id) "
                        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                        (name, odoo_version, description, parent_id, resolved_tenant),
                    )
                    row_id = cur.fetchone()[0]
                conn.commit()
            except psycopg2.errors.UniqueViolation as e:
                conn.rollback()
                raise ValueError(f"Profile '{name}' already exists") from e
            except psycopg2.errors.ForeignKeyViolation as e:
                conn.rollback()
                raise ProfileInputError(f"tenant id={tenant_id} not found") from e
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.autocommit = True
        return row_id

    def list_profiles(self) -> list[dict]:
        with self._pool.checkout() as conn:
            return self._pool.fetch_all(conn, "SELECT * FROM profiles ORDER BY id")

    def get_profile_by_id(self, profile_id: int) -> dict | None:
        """Return a single profile dict by id, or None if not found."""
        with self._pool.checkout() as conn:
            return self._pool.fetch_one(
                conn, "SELECT * FROM profiles WHERE id = %s", (profile_id,)
            )

    def get_profile_tenant_id(self, profile_name: str) -> "int | None | _ProfileMissing":
        """Return tenant_id for the profile row with the given name.

        Returns:
            int     – the tenant_id value when the profile row exists and has one.
            None    – the profile row exists but tenant_id IS NULL (shared/global).
            _MISSING sentinel – no row found for that name (profile doesn't exist).

        Callers MUST distinguish between None (shared) and the missing sentinel.
        Use ``is PROFILE_MISSING`` for the not-found check.
        """
        with self._pool.checkout() as conn:
            row = self._pool.fetch_one(
                conn,
                "SELECT tenant_id FROM profiles WHERE name = %s",
                (profile_name,),
            )
        if row is None:
            return PROFILE_MISSING
        return row["tenant_id"]

    # ------------------------------------------------------------------
    # Profile hierarchy helpers (M8 — Option Y)
    # ------------------------------------------------------------------

    def _validate_parent(
        self,
        profile_id: int,
        parent_id: int | None,
        *,
        conn=None,
    ) -> None:
        """Validate a proposed parent assignment.

        Raises ValueError when:
        - parent_id == profile_id (self-reference cycle).
        - Following parent_profile_id upward from *parent_id* reaches
          *profile_id* (would create a cycle).
        - parent.odoo_version != child.odoo_version (version mismatch).
        - the child would be more visible than the parent
          (:func:`check_child_visibility`, ProfileVisibilityError).

        *conn* is an optional already-open connection (used by add_profile to
        reuse the transaction). When None a new checkout is used.
        """
        if parent_id is None:
            return

        if parent_id == profile_id:
            raise ProfileCycleError(
                f"profile id={profile_id} cannot be its own parent (self-reference cycle)"
            )

        def _run(c):
            # Fetch child + parent versions in one shot, also get ancestor names
            # via recursive CTE to detect cycles.
            with c.cursor() as cur:
                cur.execute(
                    "SELECT odoo_version, tenant_id, name FROM profiles WHERE id = %s",
                    (profile_id,),
                )
                child_row = cur.fetchone()

            with c.cursor() as cur:
                cur.execute(
                    "SELECT odoo_version, tenant_id, name FROM profiles WHERE id = %s",
                    (parent_id,),
                )
                parent_row = cur.fetchone()

            if child_row is None:
                raise ProfileNotFoundError(f"profile id={profile_id} not found")
            if parent_row is None:
                raise ProfileNotFoundError(f"parent profile id={parent_id} not found")

            child_version, child_tenant, child_name = child_row
            parent_version, parent_tenant, parent_name = parent_row

            if child_version != parent_version:
                raise ProfileVersionMismatchError(
                    f"version mismatch: child odoo_version={child_version!r} != "
                    f"parent odoo_version={parent_version!r}"
                )
            check_child_visibility(
                child_tenant, parent_tenant,
                child=f"profile {child_name!r}", parent=f"parent {parent_name!r}",
            )

            # Cycle check: walk upward from parent_id; if we reach profile_id
            # then setting this parent would create a cycle.
            with c.cursor() as cur:
                cur.execute(
                    """
                    WITH RECURSIVE ancestors AS (
                        SELECT id, parent_profile_id
                        FROM profiles WHERE id = %s
                        -- UNION (not ALL): terminates on a cycle already in the data
                        UNION
                        SELECT p.id, p.parent_profile_id
                        FROM profiles p
                        JOIN ancestors a ON p.id = a.parent_profile_id
                    )
                    SELECT id FROM ancestors WHERE id = %s
                    """,
                    (parent_id, profile_id),
                )
                cycle_row = cur.fetchone()

            if cycle_row is not None:
                raise ProfileCycleError(
                    f"setting parent id={parent_id} for profile id={profile_id} "
                    f"would create a cycle"
                )

        if conn is not None:
            _run(conn)
        else:
            with self._pool.checkout() as c:
                _run(c)

    def set_profile_parent(self, profile_id: int, parent_id: int | None) -> bool:
        """Set (or clear) the parent_profile_id for *profile_id*.

        Validates cycle-free + version-match before updating. The UPDATE uses
        ``IS DISTINCT FROM`` so re-applying the same value is a no-op.

        Returns:
            True if the row was actually changed, False if already at the
            requested value (idempotent).

        Validation and UPDATE run in ONE transaction with both rows locked, so a
        concurrent tenant change cannot break the visibility invariant between
        the check and the write.

        Raises:
            ValueError - cycle detected, version mismatch, visibility
                (ProfileVisibilityError), or profile not found.
        """
        with self._pool.checkout() as conn:
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id FROM profiles WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                        ([profile_id] + ([parent_id] if parent_id is not None else []),),
                    )
                self._validate_parent(profile_id, parent_id, conn=conn)
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE profiles "
                        "SET parent_profile_id = %s "
                        "WHERE id = %s AND parent_profile_id IS DISTINCT FROM %s",
                        (parent_id, profile_id, parent_id),
                    )
                    rowcount = cur.rowcount
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.autocommit = True
        if rowcount == 0:
            # Distinguish "already at the requested value" (idempotent, profile
            # exists) from "profile does not exist at all".
            profile = self.get_profile_by_id(profile_id)
            if profile is None:
                raise ProfileNotFoundError(f"profile id={profile_id} not found")
        return rowcount > 0

    def set_profile_tenant(
        self,
        profile_id: int,
        tenant_id: int | None,
        *,
        confirm_shared: bool = False,
    ) -> dict:
        """Assign (or, with *confirm_shared*, clear) a profile's tenant - fail-closed.

        In ONE transaction (profile row locked ``FOR UPDATE``):

        - ``tenant_id=None`` (publish to every key) requires *confirm_shared*;
        - the profile must not become more visible than its parent, and no child
          may become more visible than the profile (:func:`check_child_visibility`);
        - the profile's repos follow: ``repos.tenant_id`` is set to the new tenant
          (the portal's write/read scope reads the repo row, ADR-0038).

        Returns ``{"changed": bool, "repos_updated": int}``.

        Raises:
            ProfileNotFoundError - no such profile.
            ProfileTenantRequiredError - ``None`` without *confirm_shared*.
            ProfileVisibilityError - parent or child invariant violated.
            ProfileInputError - *tenant_id* does not exist.
        """
        if tenant_id is None and not confirm_shared:
            raise ProfileTenantRequiredError(
                "making a profile shared publishes its modules to every key;"
                " pass confirm_shared=true to confirm"
            )
        with self._pool.checkout() as conn:
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT name, tenant_id, parent_profile_id FROM profiles"
                        " WHERE id = %s FOR UPDATE",
                        (profile_id,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise ProfileNotFoundError(f"profile id={profile_id} not found")
                    name, current_tenant, parent_id = row
                    if parent_id is not None:
                        cur.execute(
                            "SELECT name, tenant_id FROM profiles WHERE id = %s FOR SHARE",
                            (parent_id,),
                        )
                        parent_name, parent_tenant = cur.fetchone()
                        check_child_visibility(
                            tenant_id, parent_tenant,
                            child=f"profile {name!r}", parent=f"parent {parent_name!r}",
                        )
                    cur.execute(
                        "SELECT name, tenant_id FROM profiles WHERE parent_profile_id = %s"
                        " ORDER BY name FOR SHARE",
                        (profile_id,),
                    )
                    for child_name, child_tenant in cur.fetchall():
                        check_child_visibility(
                            child_tenant, tenant_id,
                            child=f"child profile {child_name!r}",
                            parent=f"profile {name!r}",
                        )
                    cur.execute(
                        "UPDATE profiles SET tenant_id = %s WHERE id = %s",
                        (tenant_id, profile_id),
                    )
                    cur.execute(
                        "UPDATE repos SET tenant_id = %s"
                        " WHERE profile_id = %s AND tenant_id IS DISTINCT FROM %s",
                        (tenant_id, profile_id, tenant_id),
                    )
                    repos_updated = cur.rowcount
                conn.commit()
            except psycopg2.errors.ForeignKeyViolation as e:
                conn.rollback()
                raise ProfileInputError(f"tenant id={tenant_id} not found") from e
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.autocommit = True
        return {"changed": current_tenant != tenant_id, "repos_updated": repos_updated}

    def set_repo_tenant(self, repo_id: int, tenant_id: int | None) -> bool:
        """Re-sync a repo's tenant; it must equal its profile's tenant.

        ``repos.tenant_id`` follows the profile (:meth:`set_profile_tenant`
        propagates it): a repo with another tenant than its profile would let
        that tenant manage content indexed into a profile it does not own, or
        expose a private profile's repo as shared in the portal. Returns False
        when the repo does not exist; raises ProfileVisibilityError on mismatch.

        Check and UPDATE run in ONE transaction with the profile row locked
        ``FOR SHARE`` (lock order profile -> repo, the same as
        :meth:`set_profile_tenant` and :meth:`add_repo`), so a concurrent
        tenant change of the profile cannot commit between the check and the
        write and leave the repo on the old tenant (``repo_tenant_mismatch``).
        """
        with self._pool.checkout() as conn:
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT profile_id FROM repos WHERE id = %s", (repo_id,))
                    rrow = cur.fetchone()
                    if rrow is None:
                        conn.rollback()
                        return False
                    profile_id = rrow[0]
                    cur.execute(
                        "SELECT tenant_id, name FROM profiles WHERE id = %s FOR SHARE",
                        (profile_id,),
                    )
                    prow = cur.fetchone()
                    if prow is None:
                        conn.rollback()
                        return False
                    profile_tenant, profile_name = prow
                    if profile_tenant != tenant_id:
                        owner = (
                            "shared" if profile_tenant is None
                            else f"private to tenant {profile_tenant}"
                        )
                        raise ProfileVisibilityError(
                            f"a repo's tenant follows its profile: profile"
                            f" {profile_name!r} is {owner}; change the profile's"
                            " tenant instead"
                        )
                    cur.execute(
                        "UPDATE repos SET tenant_id = %s WHERE id = %s AND profile_id = %s",
                        (tenant_id, repo_id, profile_id),
                    )
                    updated = cur.rowcount
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.autocommit = True
        return updated > 0

    def visibility_violations(self) -> list[dict]:
        """Read-only audit of the visibility invariant over the whole registry.

        One row per violation, ``kind`` one of:

        - ``shared_child_of_private``: a shared profile with a private ancestor
          (the leak shape: the private chain's names and repos reach every key);
        - ``cross_tenant_chain``: a private profile whose nearest private
          ancestor belongs to another tenant;
        - ``repo_tenant_mismatch``: a repo whose tenant differs from its profile's.

        An empty list means the invariant holds. Ops run it as
        ``python -m src.manager profile-visibility-audit`` (exit 1 on any row).
        """
        with self._pool.checkout() as conn:
            return self._pool.fetch_all(conn, _VISIBILITY_AUDIT_SQL)

    def _has_indexed_repos(self, profile_id: int) -> int:
        """Return the count of indexed repos for a profile (head_sha IS NOT NULL).

        A non-zero count means the profile has Neo4j/pgvector data keyed to its
        current name *and* version — changing either field requires re-indexing.
        """
        with self._pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM repos "
                    "WHERE profile_id = %s AND head_sha IS NOT NULL",
                    (profile_id,),
                )
                return cur.fetchone()[0]

    def update_profile(
        self,
        profile_id: int,
        *,
        name: str | None = None,
        version: str | None = None,
        description: str | None = None,
        lock_wait_seconds: float = WEBUI_LIFECYCLE_LOCK_WAIT_SECONDS,
    ) -> list[str]:
        """Update editable fields of a profile.

        A rename also rewrites the module lifecycle ledger's ``profile_name``
        in the same transaction (ADR-0056, review L7).

        Returns:
            List of field names that were actually changed.

        Raises:
            ProfileNotFoundError  — profile_id does not exist.
            ProfileNameConflictError — new name already taken (UNIQUE).
            ProfileVersionMismatchError — new version conflicts with a descendant or ancestor.
            ProfileIndexedError — profile has indexed repos; name/version change blocked
                until re-indexed (HTTP 409).
            LifecycleLockTimeout - rename: a ledger lock was not obtained within
                *lock_wait_seconds*; nothing changed (HTTP 409).
        """
        # Load current profile
        current = self.get_profile_by_id(profile_id)
        if current is None:
            raise ProfileNotFoundError(f"profile id={profile_id} not found")

        # Critical 2: Guard name/version changes when profile has indexed repos.
        # Neo4j Module.profile is a name string array — both name and version changes
        # cause stale graph data until a full re-index is run.
        name_changing = name is not None and name != current["name"]
        version_changing = version is not None and version != current["odoo_version"]

        if name_changing or version_changing:
            indexed_count = self._has_indexed_repos(profile_id)
            if indexed_count > 0:
                changed_fields = []
                if name_changing:
                    changed_fields.append("name")
                if version_changing:
                    changed_fields.append("version")
                fields_str = " and ".join(changed_fields)
                raise ProfileIndexedError(
                    f"Profile id={profile_id} has {indexed_count} indexed repo(s). "
                    f"Re-indexing required before {fields_str} change. "
                    f"Delete + recreate profile or trigger full reindex."
                )

        # Critical 1: If changing version, check ancestors for version mismatch.
        # This prevents a profile with no descendants from being moved to a version
        # incompatible with its parent.
        if version_changing:
            parent_id = current.get("parent_profile_id")
            if parent_id is not None:
                with self._pool.checkout() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT odoo_version FROM profiles WHERE id = %s",
                            (parent_id,),
                        )
                        parent_row = cur.fetchone()
                if parent_row is not None and parent_row[0] != version:
                    raise ProfileVersionMismatchError(
                        f"Cannot change version to {version!r}: parent profile "
                        f"id={parent_id} has version {parent_row[0]!r}"
                    )

        # If changing version, check all descendants share the same current version.
        # Descendants inherit version from the ancestor hierarchy (ADR-0016), so
        # any descendant with a *different* version would become inconsistent.
        if version_changing:
            with self._pool.checkout() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        WITH RECURSIVE descendants AS (
                            SELECT id, odoo_version
                            FROM profiles WHERE parent_profile_id = %s
                            UNION ALL
                            SELECT p.id, p.odoo_version
                            FROM profiles p
                            JOIN descendants d ON p.parent_profile_id = d.id
                        )
                        SELECT id, odoo_version FROM descendants
                        WHERE odoo_version != %s
                        LIMIT 1
                        """,
                        (profile_id, version),
                    )
                    conflict_row = cur.fetchone()
            if conflict_row is not None:
                raise ProfileVersionMismatchError(
                    f"Cannot change version to {version!r}: descendant profile "
                    f"id={conflict_row[0]} has version {conflict_row[1]!r}"
                )

        # Build dynamic SET clause
        updates: dict[str, object] = {}
        if name is not None and name != current["name"]:
            updates["name"] = name
        if version is not None and version != current["odoo_version"]:
            updates["odoo_version"] = version
        if description is not None and description != current.get("description"):
            updates["description"] = description

        if not updates:
            return []

        set_clause = ", ".join(f"{col} = %s" for col in updates)
        values = list(updates.values()) + [profile_id]

        try:
            with self._pool.checkout() as conn:
                if "name" not in updates:
                    rowcount = self._pool.execute(
                        conn,
                        f"UPDATE profiles SET {set_clause} WHERE id = %s",
                        values,
                    )
                else:
                    rowcount = self._rename_with_ledger(
                        conn, current["name"], updates["name"], set_clause, values,
                        lock_wait_seconds=lock_wait_seconds,
                    )
        except psycopg2.errors.UniqueViolation as e:
            raise ProfileNameConflictError(
                f"Profile name {name!r} already exists"
            ) from e

        if rowcount == 0:
            raise ProfileNotFoundError(f"profile id={profile_id} not found")

        return list(updates.keys())

    def _rename_with_ledger(
        self,
        conn,
        old_name: str,
        new_name: str,
        set_clause: str,
        values: list,
        *,
        lock_wait_seconds: float,
    ) -> int:
        """UPDATE the profile and the ledger's denormalized ``profile_name`` in ONE transaction.

        ``module_presence.profile_name`` is the ledger's tenant filter and RLS
        column (review L7): renaming only ``profiles`` would hide the profile's
        lifecycle history from its own tenant. The ``retire:<v>`` lock of every
        version the ledger holds rows at is held until the transaction
        committed, so no ledger writer interleaves with the rename. Raises
        ``LifecycleLockTimeout`` (nothing changed) when a lock is not obtained
        within *lock_wait_seconds*.
        """
        from contextlib import ExitStack

        from src.db.module_presence import ModulePresenceStore

        store = ModulePresenceStore(self._pool, lock_wait_seconds=lock_wait_seconds)
        versions = sorted({
            r["odoo_version"] for r in self._pool.fetch_all(
                conn,
                "SELECT DISTINCT odoo_version FROM module_presence WHERE profile_name = %s",
                (old_name,),
            )
        })
        with ExitStack() as locks:
            for version in versions:
                locks.enter_context(store.version_lock(version, conn=conn))
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    cur.execute(f"UPDATE profiles SET {set_clause} WHERE id = %s", values)
                    rowcount = cur.rowcount
                if rowcount:
                    store.rename_profile(old_name, new_name, conn=conn)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.autocommit = True
        return rowcount

    def get_ancestor_profile_names(self, profile_name: str) -> list[str]:
        """Return profile names from *self* (index 0) up to root (last).

        Uses a recursive CTE walking ``parent_profile_id`` upward. Returns
        ``[profile_name]`` (self only) when the profile has no parent.
        Returns ``[]`` when *profile_name* does not exist.
        """
        with self._pool.checkout() as conn:
            rows = self._pool.fetch_all(
                conn,
                """
                WITH RECURSIVE chain AS (
                    SELECT id, name, parent_profile_id, 0 AS depth
                    FROM profiles WHERE name = %s
                    UNION ALL
                    SELECT p.id, p.name, p.parent_profile_id, chain.depth + 1
                    FROM profiles p
                    JOIN chain ON p.id = chain.parent_profile_id
                )
                SELECT name FROM chain ORDER BY depth ASC
                """,
                (profile_name,),
            )
        return [r["name"] for r in rows]

    def get_children_profiles(self, profile_name: str) -> list[str]:
        """Return direct child profile names of *profile_name* (one level down).

        Returns ``[]`` when the profile does not exist or has no children.
        """
        with self._pool.checkout() as conn:
            rows = self._pool.fetch_all(
                conn,
                """
                SELECT name FROM profiles
                WHERE parent_profile_id = (
                    SELECT id FROM profiles WHERE name = %s
                )
                ORDER BY name ASC
                """,
                (profile_name,),
            )
        return [r["name"] for r in rows]

    def resolve_tenant_scope(self, tenant_id: int) -> tuple[list[str], list[str]]:
        """Return ``(own, shared)`` profile names for tenant *tenant_id* (WI-3, ADR-0034).

        ``own``    = profiles directly owned by the tenant (``profiles.tenant_id = tenant_id``).
        ``shared`` = ALL globally-shared profiles (``profiles.tenant_id IS NULL``) — the
                     Odoo CE/EE base, visible to every tenant.

        Why split (and why ``own`` does NOT include the shared ancestor): per
        ADR-0034 single-owner provenance, the indexer tags each node with the
        SINGLE OWNING profile of the repo that produced it (no longer the full
        ancestor chain) — so a shared-core node like ``base`` carries just
        ``["odoo_17"]`` and inheritance is resolved here at READ time via the
        ``shared`` array. The Neo4j choke-point grants a node iff it carries one of
        the tenant's ``own`` profiles OR EVERY profile on it is in ``shared`` (the
        ``all()`` predicate). That ``all()`` is load-bearing and must NOT be
        relaxed to ``any()``: a genuine same-name cross-tenant COLLISION — two
        INDEPENDENT tenants (not in an ancestor relationship) each defining a
        module of the same ``(name, version)`` — converges on ONE node whose
        ``profile[]`` is the union of BOTH owners (the MERGE key has no
        ``tenant_id`` per ADR-0034 D2). ``all()`` then correctly fail-closes that
        node to BOTH tenants, never serving tenant-B's content to tenant-A; an
        ``any()`` overlap check would leak it. Splitting own/shared + ``all()`` is
        what keeps the isolation (proven by the WI-4 cross-tenant leak test).

        A profile-less tenant gets ``own=[]`` and still sees purely-shared base
        nodes (global Odoo CE) but no tenant-private node.
        """
        with self._pool.checkout() as conn:
            own_rows = self._pool.fetch_all(
                conn, "SELECT name FROM profiles WHERE tenant_id = %s", (tenant_id,),
            )
            shared = self._shared_profile_names(conn)
        return sorted(r["name"] for r in own_rows), shared

    def shared_profile_names(self) -> list[str]:
        """Names of the globally shared profiles (``tenant_id IS NULL``), sorted.

        The same ``shared`` list :meth:`resolve_tenant_scope` returns to every
        tenant; the MCP tier uses it when an admin narrows by ``profile_name``.
        """
        with self._pool.checkout() as conn:
            return self._shared_profile_names(conn)

    def _shared_profile_names(self, conn) -> list[str]:
        rows = self._pool.fetch_all(
            conn, "SELECT name FROM profiles WHERE tenant_id IS NULL",
        )
        return sorted(r["name"] for r in rows)

    def resolve_allowed_profiles(self, tenant_id: int) -> list[str]:
        """Flat union ``own ∪ shared`` — for SINGLE-VALUE filters where each row
        carries exactly one profile name (pgvector ``embeddings.profile_name``,
        profile-name listing). The array-aware Neo4j filter uses
        :meth:`resolve_tenant_scope` directly. Empty own + shared = deny-all.
        """
        own, shared = self.resolve_tenant_scope(tenant_id)
        return sorted(set(own) | set(shared))

    def get_ancestor_repos(self, profile_name: str) -> list[dict]:
        """Return repos for *profile_name* and all its ancestors.

        Ordered by depth ASC (self = depth 0, root = deepest depth) so that
        callers see own repos first, then parent's repos, etc.

        Returns ``[]`` when the profile doesn't exist or has no repos.
        """
        with self._pool.checkout() as conn:
            return self._pool.fetch_all(
                conn,
                """
                WITH RECURSIVE chain AS (
                    SELECT id, name, parent_profile_id, 0 AS depth
                    FROM profiles WHERE name = %s
                    UNION ALL
                    SELECT p.id, p.name, p.parent_profile_id, chain.depth + 1
                    FROM profiles p
                    JOIN chain ON p.id = chain.parent_profile_id
                )
                SELECT r.*, chain.name AS profile_name, chain.depth,
                       p.odoo_version
                FROM chain
                JOIN repos r ON r.profile_id = chain.id
                JOIN profiles p ON p.id = chain.id
                ORDER BY chain.depth ASC, r.id ASC
                """,
                (profile_name,),
            )

    def add_repo(
        self,
        profile_id: int,
        url: str,
        branch: str,
        local_path: str,
        *,
        ssh_key_id: int | None = None,
        clone_status: str = "manual",
        tenant_id=_INHERIT_TENANT,
    ) -> int:
        """Insert a new repo row and return its id.

        Two distinct lifecycle columns (W1 clarification — ADR-0038):
          ``clone_status`` — git-clone lifecycle: manual/pending/cloned/error.
              Set by :meth:`set_clone_status`. Managed by the clone subprocess.
              Default 'manual' means "no auto-clone requested; indexer will
              handle the local path directly if it already exists on disk."
          ``status`` — indexer lifecycle: pending/running/done/error.
              Set by :meth:`update_repo_status`. Managed by the indexer.
              Defaults to 'pending' (migrate.py:70) — repo is freshly added and
              has not been indexed yet. This is intentional: the indexer picks up
              'pending' repos in its next run. Callers MUST NOT pass status explicitly
              unless they have a specific reason to override the default.

        Args:
            profile_id: profiles.id the repo belongs to.
            url: Remote URL (https or SSH).
            branch: Branch to index/clone.
            local_path: Server-managed path (derived via default_clone_dir).
            ssh_key_id: ssh_key_pairs.id for SSH auth; None for HTTPS.
            clone_status: git-clone lifecycle start state (default 'manual').
            tenant_id: omit to inherit the profile's tenant (the normal case).
                When given it must EQUAL the profile's tenant (None = shared):
                ``repos.tenant_id`` follows the profile, never more visible
                (ProfileVisibilityError otherwise).

        Returns:
            Integer id of the newly inserted repo row.
        """
        with self._pool.checkout() as conn:
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT tenant_id, name FROM profiles WHERE id = %s FOR SHARE",
                        (profile_id,),
                    )
                    prow = cur.fetchone()
                    if prow is None:
                        raise ProfileNotFoundError(f"profile id={profile_id} not found")
                    profile_tenant, profile_name = prow
                    if tenant_id is not _INHERIT_TENANT and tenant_id != profile_tenant:
                        raise ProfileVisibilityError(
                            f"repo tenant {tenant_id!r} differs from profile"
                            f" {profile_name!r} tenant {profile_tenant!r}; a repo's"
                            " tenant follows its profile"
                        )
                    cur.execute(
                        "INSERT INTO repos (profile_id, url, branch, local_path,"
                        " ssh_key_id, clone_status, tenant_id) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                        (profile_id, url, branch, local_path, ssh_key_id, clone_status,
                         profile_tenant),
                    )
                    row_id = cur.fetchone()[0]
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.autocommit = True
        return row_id

    def list_repos(self) -> list[dict]:
        with self._pool.checkout() as conn:
            return self._pool.fetch_all(conn, """
                SELECT r.*, p.name AS profile_name, p.odoo_version
                FROM repos r LEFT JOIN profiles p ON r.profile_id = p.id
                ORDER BY r.id
            """)

    def get_repos_for_profile(self, profile_name: str) -> list[dict]:
        with self._pool.checkout() as conn:
            return self._pool.fetch_all(
                conn,
                """
                SELECT r.*, p.odoo_version
                FROM repos r JOIN profiles p ON r.profile_id = p.id
                WHERE p.name = %s ORDER BY r.id
                """,
                (profile_name,),
            )

    def get_repos_for_profile_by_id(self, profile_id: int) -> list[dict]:
        """Return all repos for a profile identified by its numeric id."""
        with self._pool.checkout() as conn:
            return self._pool.fetch_all(
                conn,
                """
                SELECT r.*, p.name AS profile_name, p.odoo_version
                FROM repos r JOIN profiles p ON r.profile_id = p.id
                WHERE p.id = %s ORDER BY r.id
                """,
                (profile_id,),
            )

    def update_repo_status(
        self, repo_id: int, status: str, error_msg: str | None = None
    ) -> None:
        with self._pool.checkout() as conn:
            rowcount = self._pool.execute(
                conn,
                "UPDATE repos SET status = %s, error_msg = %s, "
                "last_indexed_at = CASE WHEN %s = 'indexed' THEN NOW() ELSE last_indexed_at END "
                "WHERE id = %s",
                (status, error_msg, status, repo_id),
            )
        if rowcount == 0:
            raise ValueError(f"repo id={repo_id} not found")

    def get_repo_head_sha(self, repo_id: int) -> str | None:
        """Return head_sha for repo_id, or None if NULL or repo doesn't exist."""
        with self._pool.checkout() as conn:
            row = self._pool.fetch_one(
                conn, "SELECT head_sha FROM repos WHERE id = %s", (repo_id,)
            )
        return row["head_sha"] if row is not None else None

    def update_repo_head_sha(self, repo_id: int, head_sha: str) -> None:
        """Update head_sha and bump last_indexed_at."""
        with self._pool.checkout() as conn:
            rowcount = self._pool.execute(
                conn,
                "UPDATE repos SET head_sha = %s, last_indexed_at = NOW() WHERE id = %s",
                (head_sha, repo_id),
            )
        if rowcount == 0:
            raise ValueError(f"repo id={repo_id} not found")

    def set_clone_status(
        self, repo_id: int, status: str, error_msg: str | None = None
    ) -> None:
        """Update clone_status and optionally clone_error_msg.

        Status enum: 'manual', 'pending', 'cloned', 'error'.

        Note: cloner errors are stored in `clone_error_msg` (NOT `error_msg`).
        `error_msg` is reserved for indexer errors (written by `update_repo_status`).
        Keeping them separate prevents the cloner success path from clearing a prior
        indexer error and vice versa.
        """
        valid_statuses = ("manual", "pending", "cloned", "error")
        if status not in valid_statuses:
            raise ValueError(f"Invalid clone_status: {status}. Must be one of {valid_statuses}")

        with self._pool.checkout() as conn:
            rowcount = self._pool.execute(
                conn,
                "UPDATE repos SET clone_status = %s, clone_error_msg = %s WHERE id = %s",
                (status, error_msg, repo_id),
            )
        if rowcount == 0:
            raise ValueError(f"repo id={repo_id} not found")

    def get_repos_by_clone_status(self, profile_name: str, status: str) -> list[dict]:
        """Return all repos for a profile matching the given clone_status."""
        with self._pool.checkout() as conn:
            return self._pool.fetch_all(
                conn,
                """
                SELECT r.*, p.odoo_version
                FROM repos r
                JOIN profiles p ON r.profile_id = p.id
                WHERE p.name = %s AND r.clone_status = %s
                ORDER BY r.id
                """,
                (profile_name, status),
            )

    def get_repo_by_id(self, repo_id: int) -> dict | None:
        """Return a single repo row joined with its profile, or None if not found."""
        with self._pool.checkout() as conn:
            return self._pool.fetch_one(
                conn,
                """
                SELECT r.*, p.name AS profile_name, p.odoo_version
                FROM repos r LEFT JOIN profiles p ON r.profile_id = p.id
                WHERE r.id = %s
                """,
                (repo_id,),
            )

    def get_repo_ids_by_local_path_basenames(
        self, basenames: list[str], odoo_version: str
    ) -> list[int]:
        """Return repo IDs whose local_path basename matches any entry in *basenames*
        AND whose owning profile is pinned to *odoo_version*.

        The Neo4j Module.repo property equals ``Path(local_path).name`` (the
        directory basename of the checkout).  This function maps those basename
        strings back to PostgreSQL ``repos.id`` values so that ``reset_head_sha``
        can null them out.

        The ``odoo_version`` predicate is REQUIRED (ADR-0007 W14 fix): the same
        repo (e.g. ``tvtmaaddons``) is cloned once per version/profile, so the
        basename alone matches every version's clone.  Cross-repo dependency
        propagation is version-scoped (``find_dependent_repos`` pins both sides
        to ``$version``), so a basename returned for version X must only reset
        clones at version X - never the same-basename clone at a different
        version.  Scoping by ``profiles.odoo_version`` (NOT by profile) keeps
        the legitimate cross-profile-same-version dependency intact (e.g. a
        ``viindoo_internal_17`` module depending on a ``standard_viindoo_17``
        module).

        Uses ``regexp_replace`` to extract the basename server-side — avoids
        fetching all rows into Python and doing the split there.

        Returns:
            List of repo IDs (may be shorter than basenames if some are not in
            DB at this version).
        """
        if not basenames:
            return []
        with self._pool.checkout() as conn:
            rows = self._pool.fetch_all(
                conn,
                """
                SELECT r.id
                FROM repos r
                JOIN profiles p ON r.profile_id = p.id
                WHERE regexp_replace(r.local_path, '^.*/', '') = ANY(%s)
                  AND p.odoo_version = %s
                """,
                (basenames, odoo_version),
            )
        return [r["id"] for r in rows]

    def reset_head_sha(self, repo_ids: list[int]) -> int:
        """Bulk-reset head_sha to NULL for given repo IDs.

        Forces those repos to be fully re-indexed on the next indexer run.
        Used by cross-repo dependency propagation (M7 W14): when upstream modules
        change, downstream repos' head_sha is NULLed so they are not skipped.

        Returns:
            Number of rows updated (may be less than len(repo_ids) if some IDs
            do not exist in the table).
        """
        if not repo_ids:
            return 0
        with self._pool.checkout() as conn:
            return self._pool.execute(
                conn,
                "UPDATE repos SET head_sha = NULL WHERE id = ANY(%s)",
                (repo_ids,),
            )

    def delete_profile(self, profile_id: int) -> dict:
        """Delete profile by ID. PG CASCADE removes child repos automatically.

        Computes the list of repos BEFORE delete so the caller can pass
        (repo_basename, odoo_version) pairs to Neo4j + pgvector cleanup.

        Returns dict with:
            repos: list of {repo_basename, odoo_version, module_paths} for caller
                   to pass to Neo4j + pgvector cleanup.
        """
        with self._pool.checkout() as conn:
            repo_rows = self._pool.fetch_all(
                conn,
                """
                SELECT r.local_path, p.odoo_version
                FROM repos r
                JOIN profiles p ON r.profile_id = p.id
                WHERE r.profile_id = %s
                """,
                (profile_id,),
            )
            repos = [
                {
                    "repo_basename": Path(r["local_path"]).name,
                    "odoo_version": r["odoo_version"],
                    "module_paths": [],
                }
                for r in repo_rows
            ]
            rowcount = self._pool.execute(
                conn, "DELETE FROM profiles WHERE id = %s", (profile_id,)
            )
            if rowcount == 0:
                raise ValueError(f"profile id={profile_id} not found")
        return {"repos": repos}

    def delete_repo(self, repo_id: int) -> dict:
        """Delete repo by ID. Returns {repo_basename, odoo_version} for Neo4j cleanup.

        Looks up repo info BEFORE deleting so the caller can clean up Neo4j
        and pgvector data scoped to this repo.

        Raises ValueError if repo not found.
        """
        with self._pool.checkout() as conn:
            row = self._pool.fetch_one(
                conn,
                """
                SELECT r.local_path, p.odoo_version
                FROM repos r
                JOIN profiles p ON r.profile_id = p.id
                WHERE r.id = %s
                """,
                (repo_id,),
            )
            if row is None:
                raise ValueError(f"repo id={repo_id} not found")
            repo_basename = Path(row["local_path"]).name
            odoo_version = row["odoo_version"]
            self._pool.execute(conn, "DELETE FROM repos WHERE id = %s", (repo_id,))
        return {"repo_basename": repo_basename, "odoo_version": odoo_version}

    def reset_repo_head_sha(self, repo_id: int) -> None:
        """Set repos.head_sha = NULL to force full re-index on next run.

        Used by the Web UI "Reset embed state" button (M8 W4) so that
        a repo previously indexed with --no-embed can be re-indexed
        with embeddings on the next run.

        Raises ValueError if repo not found.
        """
        with self._pool.checkout() as conn:
            rowcount = self._pool.execute(
                conn, "UPDATE repos SET head_sha = NULL WHERE id = %s", (repo_id,)
            )
        if rowcount == 0:
            raise ValueError(f"repo id={repo_id} not found")

    def update_repo(
        self,
        repo_id: int,
        *,
        url: str | None = None,
        branch: str | None = None,
        ssh_key_id: int | None = None,
        clear_ssh_key: bool = False,
        local_path: str | None = None,
    ) -> list[str]:
        """Update editable fields of a repo without touching head_sha.

        head_sha is intentionally preserved — this is the whole point of PATCH
        vs delete+recreate: the incremental indexer can still use the stored sha.

        local_path is server-managed (derived by default_clone_dir at creation).
        Callers MUST NOT pass user-supplied local_path values here; the parameter
        is kept only for internal server use (e.g. clone_all short-circuit path).

        Args:
            repo_id: ID of repo to update.
            url: New remote URL (or None to leave unchanged).
            branch: New branch name (or None to leave unchanged).
            ssh_key_id: New SSH key id (or None to leave unchanged).
            clear_ssh_key: When True, set ssh_key_id = NULL regardless of ssh_key_id arg.
            local_path: New local path — server-managed only (ignored in PATCH routes).

        Returns:
            List of field names that were updated.

        Raises:
            RepoNotFoundError: if repo_id does not exist.
            RepoConflictError: if the new (url, branch, profile_id) would violate UNIQUE constraint.
        """
        # Verify repo exists and fetch current values for conflict-check
        existing = self.get_repo_by_id(repo_id)
        if existing is None:
            raise RepoNotFoundError(f"repo id={repo_id} not found")

        # Resolve effective values for UNIQUE check
        effective_url = url if url is not None else existing["url"]
        effective_branch = branch if branch is not None else existing["branch"]
        # profile_id is immutable after creation — use the stored value
        profile_id = existing["profile_id"]

        # Check UNIQUE(url, branch, profile_id) before UPDATE — exclude current row
        # Scoped to profile_id so the same upstream URL can be registered under
        # different profiles/tenants (ADR-0034 D2 + m13_002 constraint change).
        if url is not None or branch is not None:
            with self._pool.checkout() as conn:
                conflict_row = self._pool.fetch_one(
                    conn,
                    "SELECT id FROM repos WHERE url = %s AND branch = %s"
                    " AND profile_id = %s AND id != %s",
                    (effective_url, effective_branch, profile_id, repo_id),
                )
            if conflict_row is not None:
                raise RepoConflictError(
                    f"A repo with url={effective_url!r} branch={effective_branch!r}"
                    f" already exists under profile_id={profile_id}"
                )

        # Build dynamic SET clause — only include fields the caller wants to change
        set_parts: list[str] = []
        params: list = []
        updated_fields: list[str] = []

        if url is not None:
            set_parts.append("url = %s")
            params.append(url)
            updated_fields.append("url")

        if branch is not None:
            set_parts.append("branch = %s")
            params.append(branch)
            updated_fields.append("branch")

        if clear_ssh_key:
            set_parts.append("ssh_key_id = NULL")
            updated_fields.append("ssh_key_id")
        elif ssh_key_id is not None:
            set_parts.append("ssh_key_id = %s")
            params.append(ssh_key_id)
            updated_fields.append("ssh_key_id")

        if local_path is not None:
            set_parts.append("local_path = %s")
            params.append(local_path)
            updated_fields.append("local_path")

        if not set_parts:
            return []  # nothing to update — idempotent no-op

        params.append(repo_id)
        sql = f"UPDATE repos SET {', '.join(set_parts)} WHERE id = %s"

        try:
            with self._pool.checkout() as conn:
                self._pool.execute(conn, sql, tuple(params))
        except psycopg2.errors.UniqueViolation as e:
            # Safety net for TOCTOU race: pre-check can pass while a concurrent
            # UPDATE commits the same (url, branch, profile_id) between our SELECT
            # and UPDATE.
            raise RepoConflictError(
                f"A repo with url={effective_url!r} branch={effective_branch!r} "
                f"already exists under profile_id={profile_id} (concurrent write)"
            ) from e

        return updated_fields

    def update_repo_local_path(self, repo_id: int, local_path: str) -> None:
        """Update local_path for a repo after a successful clone.

        Does NOT touch last_indexed_at — cloning is not indexing. last_indexed_at
        is bumped only by update_repo_head_sha (called at the end of a real index run).
        """
        with self._pool.checkout() as conn:
            rowcount = self._pool.execute(
                conn,
                "UPDATE repos SET local_path = %s WHERE id = %s",
                (local_path, repo_id),
            )
        if rowcount == 0:
            raise ValueError(f"repo id={repo_id} not found")
