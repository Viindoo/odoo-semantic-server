# src/db/repo_registry.py
"""CRUD for profiles + repos in PostgreSQL."""
from pathlib import Path

import psycopg2.errors

from src.db.exceptions import (
    ProfileCycleError,
    ProfileIndexedError,
    ProfileNameConflictError,
    ProfileNotFoundError,
    ProfileVersionMismatchError,
    RepoConflictError,
    RepoNotFoundError,
)
from src.db.pg import PgPool


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
    ) -> int:
        """Insert a new profile. Raises ValueError if name already exists.

        When *parent_id* is provided, the parent's existence and odoo_version
        match are validated **before** the INSERT so that a failed validation
        cannot leave an orphan row.  (``checkout()`` sets autocommit=True, so
        an INSERT that auto-commits before validation would create an orphan.)
        """
        if parent_id is not None:
            # Validate parent existence + version match before touching the DB.
            # The child doesn't exist yet so cycle detection is trivially safe —
            # only version match and parent existence need to be checked here.
            with self._pool.checkout() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT odoo_version FROM profiles WHERE id = %s",
                        (parent_id,),
                    )
                    parent_row = cur.fetchone()
            if parent_row is None:
                raise ValueError(f"parent profile id={parent_id} not found")
            parent_version = parent_row[0]
            if parent_version != odoo_version:
                raise ValueError(
                    f"version mismatch: child odoo_version={odoo_version!r} != "
                    f"parent odoo_version={parent_version!r}"
                )

        try:
            with self._pool.checkout() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO profiles "
                        "(name, odoo_version, description, parent_profile_id) "
                        "VALUES (%s, %s, %s, %s) RETURNING id",
                        (name, odoo_version, description, parent_id),
                    )
                    row_id = cur.fetchone()[0]
            return row_id
        except psycopg2.errors.UniqueViolation as e:
            raise ValueError(f"Profile '{name}' already exists") from e

    def list_profiles(self) -> list[dict]:
        with self._pool.checkout() as conn:
            return self._pool.fetch_all(conn, "SELECT * FROM profiles ORDER BY id")

    def get_profile_by_id(self, profile_id: int) -> dict | None:
        """Return a single profile dict by id, or None if not found."""
        with self._pool.checkout() as conn:
            return self._pool.fetch_one(
                conn, "SELECT * FROM profiles WHERE id = %s", (profile_id,)
            )

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
                    "SELECT odoo_version FROM profiles WHERE id = %s",
                    (profile_id,),
                )
                child_row = cur.fetchone()

            with c.cursor() as cur:
                cur.execute(
                    "SELECT odoo_version FROM profiles WHERE id = %s",
                    (parent_id,),
                )
                parent_row = cur.fetchone()

            if child_row is None:
                raise ProfileNotFoundError(f"profile id={profile_id} not found")
            if parent_row is None:
                raise ProfileNotFoundError(f"parent profile id={parent_id} not found")

            child_version = child_row[0]
            parent_version = parent_row[0]

            if child_version != parent_version:
                raise ProfileVersionMismatchError(
                    f"version mismatch: child odoo_version={child_version!r} != "
                    f"parent odoo_version={parent_version!r}"
                )

            # Cycle check: walk upward from parent_id; if we reach profile_id
            # then setting this parent would create a cycle.
            with c.cursor() as cur:
                cur.execute(
                    """
                    WITH RECURSIVE ancestors AS (
                        SELECT id, parent_profile_id
                        FROM profiles WHERE id = %s
                        UNION ALL
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

        Raises:
            ValueError — cycle detected, version mismatch, or profile not found.
        """
        self._validate_parent(profile_id, parent_id)
        with self._pool.checkout() as conn:
            rowcount = self._pool.execute(
                conn,
                "UPDATE profiles "
                "SET parent_profile_id = %s "
                "WHERE id = %s AND parent_profile_id IS DISTINCT FROM %s",
                (parent_id, profile_id, parent_id),
            )
        if rowcount == 0:
            # Distinguish "already at the requested value" (idempotent, profile
            # exists) from "profile does not exist at all".
            profile = self.get_profile_by_id(profile_id)
            if profile is None:
                raise ProfileNotFoundError(f"profile id={profile_id} not found")
        return rowcount > 0

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
    ) -> list[str]:
        """Update editable fields of a profile.

        Returns:
            List of field names that were actually changed.

        Raises:
            ProfileNotFoundError  — profile_id does not exist.
            ProfileNameConflictError — new name already taken (UNIQUE).
            ProfileVersionMismatchError — new version conflicts with a descendant or ancestor.
            ProfileIndexedError — profile has indexed repos; name/version change blocked
                until re-indexed (HTTP 409).
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
                rowcount = self._pool.execute(
                    conn,
                    f"UPDATE profiles SET {set_clause} WHERE id = %s",
                    values,
                )
        except psycopg2.errors.UniqueViolation as e:
            raise ProfileNameConflictError(
                f"Profile name {name!r} already exists"
            ) from e

        if rowcount == 0:
            raise ProfileNotFoundError(f"profile id={profile_id} not found")

        return list(updates.keys())

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
    ) -> int:
        with self._pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO repos "
                    "(profile_id, url, branch, local_path, ssh_key_id, clone_status) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                    (profile_id, url, branch, local_path, ssh_key_id, clone_status),
                )
                row_id = cur.fetchone()[0]
            conn.commit()
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

    def get_repo_ids_by_local_path_basenames(self, basenames: list[str]) -> list[int]:
        """Return repo IDs whose local_path basename matches any entry in *basenames*.

        The Neo4j Module.repo property equals ``Path(local_path).name`` (the
        directory basename of the checkout).  This function maps those basename
        strings back to PostgreSQL ``repos.id`` values so that ``reset_head_sha``
        can null them out.

        Uses ``regexp_replace`` to extract the basename server-side — avoids
        fetching all rows into Python and doing the split there.

        Returns:
            List of repo IDs (may be shorter than basenames if some are not in DB).
        """
        if not basenames:
            return []
        with self._pool.checkout() as conn:
            rows = self._pool.fetch_all(
                conn,
                "SELECT id FROM repos WHERE regexp_replace(local_path, '^.*/', '') = ANY(%s)",
                (basenames,),
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

        Args:
            repo_id: ID of repo to update.
            url: New remote URL (or None to leave unchanged).
            branch: New branch name (or None to leave unchanged).
            ssh_key_id: New SSH key id (or None to leave unchanged).
            clear_ssh_key: When True, set ssh_key_id = NULL regardless of ssh_key_id arg.
            local_path: New local checkout path (or None to leave unchanged).

        Returns:
            List of field names that were updated.

        Raises:
            RepoNotFoundError: if repo_id does not exist.
            RepoConflictError: if the new (url, branch) would violate UNIQUE constraint.
        """
        # Verify repo exists and fetch current values for conflict-check
        existing = self.get_repo_by_id(repo_id)
        if existing is None:
            raise RepoNotFoundError(f"repo id={repo_id} not found")

        # Resolve effective values for UNIQUE check
        effective_url = url if url is not None else existing["url"]
        effective_branch = branch if branch is not None else existing["branch"]

        # Check UNIQUE(url, branch) before UPDATE — exclude current row
        if url is not None or branch is not None:
            with self._pool.checkout() as conn:
                conflict_row = self._pool.fetch_one(
                    conn,
                    "SELECT id FROM repos WHERE url = %s AND branch = %s AND id != %s",
                    (effective_url, effective_branch, repo_id),
                )
            if conflict_row is not None:
                raise RepoConflictError(
                    f"A repo with url={effective_url!r} branch={effective_branch!r} already exists"
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
            # UPDATE commits the same (url, branch) between our SELECT and UPDATE.
            raise RepoConflictError(
                f"A repo with url={effective_url!r} branch={effective_branch!r} "
                f"already exists (concurrent write)"
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
