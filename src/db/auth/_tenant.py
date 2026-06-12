# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tenant + membership domain methods for AuthStore (tenant CRUD, membership,
profile/repo tenant assignment, default-mint-tenant resolution)."""
from src.db.auth._shared import VIINDOO_EMAIL_DOMAINS


class _TenantMixin:
    """Tenant + membership SQL operations (multi-tenant model, ADR-0038).

    Composed into AuthStore; relies on ``self._pool`` set by AuthStore.__init__.
    """

    # ------------------------------------------------------------------
    # W1: Tenant membership CRUD (multi-tenant-per-user model, ADR-0038)
    # ------------------------------------------------------------------

    def list_tenants(self) -> list[dict]:
        """Return all tenants ordered by id, with member/repo/profile counts.

        Returns:
            List of dicts with keys: id, name, active, created_at,
            member_count, repo_count, profile_count.
        """
        with self._pool.checkout() as conn:
            rows = self._pool.fetch_all(
                conn,
                """
                SELECT
                    t.id, t.name, t.active, t.created_at,
                    (SELECT count(*) FROM tenant_members tm WHERE tm.tenant_id = t.id)
                        AS member_count,
                    (SELECT count(*) FROM repos r WHERE r.tenant_id = t.id)
                        AS repo_count,
                    (SELECT count(*) FROM profiles p WHERE p.tenant_id = t.id)
                        AS profile_count
                FROM tenants t
                ORDER BY t.id
                """,
            )
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "active": bool(r["active"]),
                "created_at": str(r["created_at"]) if r["created_at"] else None,
                "member_count": int(r["member_count"]),
                "repo_count": int(r["repo_count"]),
                "profile_count": int(r["profile_count"]),
            }
            for r in rows
        ]

    def get_tenant_by_id(self, tenant_id: int) -> dict | None:
        """Return a single tenant row or None if not found."""
        with self._pool.checkout() as conn:
            row = self._pool.fetch_one(
                conn,
                "SELECT id, name, active, created_at FROM tenants WHERE id = %s",
                (tenant_id,),
            )
        if row is None:
            return None
        return {
            "id": row["id"],
            "name": row["name"],
            "active": bool(row["active"]),
            "created_at": str(row["created_at"]) if row["created_at"] else None,
        }

    def create_tenant(self, name: str) -> int:
        """Create a new tenant.

        Args:
            name: Unique tenant name. Must not contain ',' (GUC-delimiter guard).

        Returns:
            Integer id of the newly created tenant.

        Raises:
            ValueError: name contains ',' or is empty.
            psycopg2.errors.UniqueViolation: name is already taken.
        """
        if not name or not name.strip():
            raise ValueError("Tenant name must not be empty")
        if "," in name:
            raise ValueError("Tenant name must not contain ','")
        name = name.strip()
        with self._pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO tenants (name) VALUES (%s) RETURNING id",
                    (name,),
                )
                row_id = cur.fetchone()[0]
            conn.commit()
        return row_id

    def get_public_tenant_id(self) -> int:
        """Return the id of the 'public' tenant (Odoo-only free-signup scope).

        The 'public' tenant is created by migration m13_019. New free-signup keys
        for non-Viindoo, non-admin users are bound to it so they read only the
        shared 'odoo_*' base profiles (ADR-0034).

        Raises:
            RuntimeError: if the 'public' tenant is absent (m13_019 not applied).
                Fail-closed: callers must NOT fall back to tenant_id=None.
        """
        with self._pool.checkout() as conn:
            row = self._pool.fetch_one(
                conn, "SELECT id FROM tenants WHERE name = %s", ("public",)
            )
        if row is None:
            raise RuntimeError("public tenant missing — run m13_019")
        return row["id"]

    def get_viindoo_tenant_id(self) -> int:
        """Return the id of the Viindoo tenant.

        New free-signup keys for @viindoo.com users are bound to this tenant so
        they can read the restricted 'standard_viindoo_*' / 'viindoo_internal_*'
        profiles (ADR-0034).

        Resolution (SSOT = the data the migration actually produces, not a
        duplicated literal tenant name):
          1. The tenant that OWNS the viindoo profiles — i.e. the DISTINCT
             non-NULL tenant_id of the 'standard_viindoo_*' / 'viindoo_internal_*'
             profiles that m13_019 moved. If this resolves to exactly one
             tenant_id, use it (ties the resolver to m13_019's effect, immune to
             a tenant rename).
          2. Fallback to the literal name 'Viindoo Technology JSC' — for fresh
             installs where m13_019 has not yet moved any profile (step 1 yields
             zero rows).

        Raises:
            RuntimeError: if the tenant is absent (m13_019 not applied) — same
                fail-closed contract as before; callers must NOT fall back to
                tenant_id=None.
            RuntimeError: if the viindoo profiles are owned by MORE THAN ONE
                tenant (data inconsistency) — fail-closed rather than guess.
        """
        with self._pool.checkout() as conn:
            # NB: '%%' (not '%') — psycopg2 treats the SQL as a format string and
            # would otherwise read the lone '%' as a parameter placeholder.
            owner_rows = self._pool.fetch_all(
                conn,
                r"""
                SELECT DISTINCT tenant_id
                  FROM profiles
                 WHERE (name LIKE 'standard\_viindoo\_%%' ESCAPE '\'
                        OR name LIKE 'viindoo\_internal\_%%' ESCAPE '\')
                   AND tenant_id IS NOT NULL
                """,
            )
            if len(owner_rows) == 1:
                return owner_rows[0]["tenant_id"]
            if len(owner_rows) > 1:
                raise RuntimeError(
                    "viindoo profiles owned by multiple tenants "
                    f"({[r['tenant_id'] for r in owner_rows]}) — refusing to "
                    "guess the Viindoo tenant (data inconsistency)"
                )
            # 2. Fresh-install fallback: resolve by the canonical name.
            row = self._pool.fetch_one(
                conn,
                "SELECT id FROM tenants WHERE name = %s",
                ("Viindoo Technology JSC",),
            )
        if row is None:
            raise RuntimeError("Viindoo tenant missing — run m13_019")
        return row["id"]

    def resolve_default_mint_tenant_id(self, user_id: int | None) -> int:
        """Resolve the tenant a newly-minted key for ``user_id`` must be bound to.

        Precedence (ADR-0034 + ADR-0038):
          1. tenant_members membership (multi-tenant SSOT): if the user is a
             member of EXACTLY ONE tenant → use that tenant_id. This makes a
             paid-tenant member self-minting a key land in their real tenant,
             not in 'public' just because their email is gmail.
          2. else by email domain: @viindoo.com → Viindoo tenant (sees the
             restricted viindoo profiles).
          3. else → public tenant (sees only the shared 'odoo_*' base).

        Membership count handling:
          - 0 memberships → fall through to step 2/3 (domain / public).
          - exactly 1     → that tenant.
          - >1 (ambiguous, no per-request tenant available here) → fall through
            to step 2/3 deterministically. We do NOT guess among several
            tenants; domain/public is the safe, stable default and the key can
            be re-scoped explicitly afterwards. Documented choice.

        NEVER returns None. The unrestricted (tenant_id=None) sentinel is reserved
        for admins and system/CLI keys, which do NOT go through this resolver —
        their mint sites pass tenant_id=None explicitly. Binding a free key to a
        non-NULL tenant is the whole point of the m13_019 isolation fix; falling
        back to None here would re-open the hole.

        Args:
            user_id: webui_users.id of the key owner. May be None for system/CLI
                contexts — those are treated as public (Odoo-only), never
                unrestricted.

        Raises:
            RuntimeError: if the required tenant is absent (m13_019 not applied).
                Propagated so the mint FAILS fail-closed rather than minting an
                unrestricted key.
        """
        # 1. Exactly-one tenant_members membership wins (ADR-0038 multi-tenant).
        if user_id is not None:
            member_tenants = self.list_tenant_ids_for_user(user_id)
            if len(member_tenants) == 1:
                return member_tenants[0]

        # 2. Email domain.
        email = None
        if user_id is not None:
            email = self.get_user_field(user_id, "email")
        if email and "@" in str(email):
            domain = str(email).rsplit("@", 1)[1].strip().lower()
            if domain in VIINDOO_EMAIL_DOMAINS:
                return self.get_viindoo_tenant_id()

        # 3. Public fallback (fail-closed: a concrete tenant, never None).
        return self.get_public_tenant_id()

    def update_tenant(
        self, tenant_id: int, *, name: str | None = None, active: bool | None = None
    ) -> bool:
        """Update a tenant's name and/or active flag.

        Args:
            tenant_id: The tenant to update.
            name: New name (optional). Must not be empty or contain ','.
            active: New active flag (optional).

        Returns:
            True if the tenant was found and updated, False if not found.

        Raises:
            ValueError: name contains ',' or is empty.
            psycopg2.errors.UniqueViolation: name is already taken.
        """
        if name is not None and "," in name:
            raise ValueError("Tenant name must not contain ','")
        if name is not None and not name.strip():
            raise ValueError("Tenant name must not be empty")
        parts = []
        params: list = []
        if name is not None:
            parts.append("name = %s")
            params.append(name.strip())
        if active is not None:
            parts.append("active = %s")
            params.append(active)
        if not parts:
            return True
        params.append(tenant_id)
        with self._pool.checkout() as conn:
            rowcount = self._pool.execute(
                conn,
                f"UPDATE tenants SET {', '.join(parts)} WHERE id = %s",  # noqa: S608
                tuple(params),
            )
        return rowcount > 0

    def delete_tenant(self, tenant_id: int) -> bool:
        """Delete a tenant.

        Raises ValueError if the tenant still has repos or profiles assigned to it
        (D8 — no silent cascade-to-NULL; membership CASCADE is allowed).

        TOCTOU safety: the tenant row is locked with SELECT ... FOR UPDATE before the
        resource-count check, and the count + DELETE run in a single transaction. A
        concurrent PATCH that assigns a repo/profile to this tenant takes a FOR KEY
        SHARE lock on this same row (the FK reference), which conflicts with FOR
        UPDATE — so the assign is serialised against the delete and cannot slip in
        between the count check and the DELETE. Without this, the count and DELETE ran
        in two separate pool checkouts; because repos.tenant_id / profiles.tenant_id
        are ON DELETE CASCADE (m13_002), a raced assign would have been silently
        cascade-deleted — exactly the data loss D8 forbids. Mirrors set_user_admin().

        Args:
            tenant_id: The tenant to delete.

        Returns:
            True if deleted, False if not found.

        Raises:
            ValueError: tenant still has repo or profile resources assigned.
        """
        with self._pool.checkout() as conn:
            conn.autocommit = False
            try:
                # Lock the tenant row first — serialises concurrent resource-assigns
                # (their FK FOR KEY SHARE lock conflicts with this FOR UPDATE).
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id FROM tenants WHERE id = %s FOR UPDATE",
                        (tenant_id,),
                    )
                    if cur.fetchone() is None:
                        conn.commit()
                        return False  # not found
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT count(*) FROM repos WHERE tenant_id = %s",
                        (tenant_id,),
                    )
                    repo_count = cur.fetchone()[0]
                if repo_count > 0:
                    raise ValueError(
                        f"Tenant {tenant_id} still has {repo_count} repo(s) assigned. "
                        "Unassign them before deleting the tenant."
                    )
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT count(*) FROM profiles WHERE tenant_id = %s",
                        (tenant_id,),
                    )
                    profile_count = cur.fetchone()[0]
                if profile_count > 0:
                    raise ValueError(
                        f"Tenant {tenant_id} still has {profile_count} profile(s) "
                        "assigned. Unassign them before deleting the tenant."
                    )
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
                    rowcount = cur.rowcount
                conn.commit()
                return rowcount > 0
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.autocommit = True

    def list_tenant_ids_for_user(self, user_id: int) -> list[int]:
        """Return tenant_ids the user has a membership row for. [] if none.

        Args:
            user_id: webui_users.id to look up.

        Returns:
            List of tenant_id integers. Empty list if no memberships.
        """
        with self._pool.checkout() as conn:
            rows = self._pool.fetch_all(
                conn,
                "SELECT tenant_id FROM tenant_members WHERE user_id = %s",
                (user_id,),
            )
        return [r["tenant_id"] for r in rows]

    def list_tenant_memberships_for_user(self, user_id: int) -> list[dict]:
        """Return [{tenant_id, name, role}] for all tenants the user belongs to.

        Joins tenant_members with tenants to include the tenant name.
        Used by GET /api/account/tenants (W2 self-service portal).

        Args:
            user_id: webui_users.id to look up.

        Returns:
            List of dicts with keys: tenant_id, name, role. Empty list if no memberships.
        """
        with self._pool.checkout() as conn:
            rows = self._pool.fetch_all(
                conn,
                """
                SELECT tm.tenant_id, t.name, tm.role
                FROM tenant_members tm
                JOIN tenants t ON t.id = tm.tenant_id
                WHERE tm.user_id = %s
                ORDER BY t.name
                """,
                (user_id,),
            )
        return [
            {
                "tenant_id": r["tenant_id"],
                "name": r["name"],
                "role": r["role"],
            }
            for r in rows
        ]

    def user_is_member_of(self, user_id: int, tenant_id: int) -> bool:
        """True iff a tenant_members row exists for (user_id, tenant_id).

        Args:
            user_id: webui_users.id.
            tenant_id: tenants.id.

        Returns:
            True if membership row exists, False otherwise.
        """
        with self._pool.checkout() as conn:
            row = self._pool.fetch_one(
                conn,
                "SELECT 1 FROM tenant_members WHERE user_id = %s AND tenant_id = %s LIMIT 1",
                (user_id, tenant_id),
            )
        return row is not None

    def add_tenant_member(self, user_id: int, tenant_id: int, role: str = "member") -> None:
        """Add or update a user's membership in a tenant.

        Idempotent: ON CONFLICT upserts the role if the row already exists.

        Args:
            user_id: webui_users.id.
            tenant_id: tenants.id.
            role: 'member' (default) or 'tenant_admin'.
        """
        if role not in ("member", "tenant_admin"):
            raise ValueError(f"Invalid role '{role}'. Must be 'member' or 'tenant_admin'.")
        with self._pool.checkout() as conn:
            self._pool.execute(
                conn,
                """
                INSERT INTO tenant_members (user_id, tenant_id, role)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, tenant_id) DO UPDATE SET role = EXCLUDED.role
                """,
                (user_id, tenant_id, role),
            )

    def remove_tenant_member(self, user_id: int, tenant_id: int) -> None:
        """Remove a user's membership from a tenant.

        Args:
            user_id: webui_users.id.
            tenant_id: tenants.id.
        """
        with self._pool.checkout() as conn:
            self._pool.execute(
                conn,
                "DELETE FROM tenant_members WHERE user_id = %s AND tenant_id = %s",
                (user_id, tenant_id),
            )

    def list_members_of_tenant(self, tenant_id: int) -> list[dict]:
        """Return all members of a tenant with user details.

        Args:
            tenant_id: tenants.id to list members for.

        Returns:
            List of dicts with keys: user_id, username, email, role, created_at.
        """
        with self._pool.checkout() as conn:
            rows = self._pool.fetch_all(
                conn,
                """
                SELECT tm.user_id, u.username, u.email, tm.role, tm.created_at
                FROM tenant_members tm
                JOIN webui_users u ON u.id = tm.user_id
                WHERE tm.tenant_id = %s
                ORDER BY u.username
                """,
                (tenant_id,),
            )
        return [
            {
                "user_id": r["user_id"],
                "username": r["username"],
                "email": r["email"],
                "role": r["role"],
                "created_at": str(r["created_at"]) if r["created_at"] else None,
            }
            for r in rows
        ]

    def assign_profile_tenant(self, profile_id: int, tenant_id: int | None) -> bool:
        """Assign (or clear) the tenant for a profile.

        Args:
            profile_id: profiles.id to update.
            tenant_id: tenants.id, or None to set as shared/global.

        Returns:
            True if the profile was found and updated, False otherwise.
        """
        with self._pool.checkout() as conn:
            rowcount = self._pool.execute(
                conn,
                "UPDATE profiles SET tenant_id = %s WHERE id = %s",
                (tenant_id, profile_id),
            )
        return rowcount > 0

    def assign_repo_tenant(self, repo_id: int, tenant_id: int | None) -> bool:
        """Assign (or clear) the tenant for a repo.

        Args:
            repo_id: repos.id to update.
            tenant_id: tenants.id, or None to set as shared/global.

        Returns:
            True if the repo was found and updated, False otherwise.
        """
        with self._pool.checkout() as conn:
            rowcount = self._pool.execute(
                conn,
                "UPDATE repos SET tenant_id = %s WHERE id = %s",
                (tenant_id, repo_id),
            )
        return rowcount > 0
