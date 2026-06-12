# SPDX-License-Identifier: AGPL-3.0-or-later
"""SSH key-pair domain methods for AuthStore (admin access keys + per-tenant
deploy keys)."""


class _SshKeyMixin:
    """SSH key-pair SQL operations.

    Composed into AuthStore; relies on ``self._pool`` set by AuthStore.__init__.
    """

    # ------------------------------------------------------------------
    # SSH key pairs
    # ------------------------------------------------------------------

    def list_ssh_keys(self) -> list[dict]:
        """List admin-managed access-key SSH pairs (without private key).

        Excludes ``key_type='deploy_key'`` rows: per-tenant deploy keys are
        owned via the self-service endpoint (ADR-0034 D7), not the admin SSH-key
        surface, and must never appear in the admin "Stored Keys" table, the
        Add-Repo SSH-key dropdown, or the dashboard count. The clone path
        resolves credentials by id via get_ssh_key_by_id (unaffected by this
        filter).

        Returns:
            List of dicts with keys: id, name, public_key, key_version, created_at.
        """
        with self._pool.checkout() as conn:
            return self._pool.fetch_all(
                conn,
                "SELECT id, name, public_key, key_version, created_at"
                " FROM ssh_key_pairs WHERE key_type = 'access_key' ORDER BY id",
            )

    def get_ssh_key_by_id(self, key_id: int) -> dict | None:
        """Return ssh_key_pairs row (including private_key_encrypted) or None."""
        with self._pool.checkout() as conn:
            return self._pool.fetch_one(
                conn,
                "SELECT id, name, public_key, private_key_encrypted, key_version, created_at "
                "FROM ssh_key_pairs WHERE id = %s",
                (key_id,),
            )

    def save_ssh_key(
        self,
        name: str,
        public_key: str,
        private_key_encrypted: str,
        key_version: int = 1,
    ) -> int:
        """Save SSH key pair. Return id.

        Args:
            name: Descriptive name for this key pair.
            public_key: The full public key string (e.g. 'ssh-ed25519 AAAA...').
            private_key_encrypted: Private key encrypted with a master key (not stored raw).
            key_version: Version of the encryption key used (default 1).

        Returns:
            Integer id of the newly created ssh_key_pair row.
        """
        with self._pool.checkout() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ssh_key_pairs"
                    " (name, public_key, private_key_encrypted, key_version)"
                    " VALUES (%s, %s, %s, %s) RETURNING id",
                    (name, public_key, private_key_encrypted, key_version),
                )
                row_id = cur.fetchone()[0]
            conn.commit()
        return row_id

    def delete_ssh_key(self, key_id: int) -> None:
        """Delete SSH key pair by id.

        Args:
            key_id: The ssh_key_pairs id to delete.
        """
        with self._pool.checkout() as conn:
            self._pool.execute(
                conn,
                "DELETE FROM ssh_key_pairs WHERE id = %s",
                (key_id,),
            )

    def get_or_create_tenant_deploy_key(self, conn, tenant_id: int) -> str:
        """Return the public key for the tenant's deploy keypair.

        Idempotent: if a row with key_type='deploy_key' and the given tenant_id
        already exists, return its public_key.  Otherwise generate a new Ed25519
        keypair, FERNET-encrypt the private key (reusing generate_ed25519_keypair
        from src.web_ui.routes.ssh_keys), and INSERT it.  The private key is
        NEVER returned.

        This method accepts an open psycopg2 connection so the caller can manage
        the transaction boundary.  Caller is responsible for commit.

        Args:
            conn: Open psycopg2 connection (NOT drawn from the pool — caller owns it).
            tenant_id: tenants.id for which the deploy key is being requested.

        Returns:
            The OpenSSH public key string (e.g. 'ssh-ed25519 AAAA...').

        Raises:
            RuntimeError: If FERNET_KEY is not set (ADR-0020 fail-fast policy).
        """
        from src.web_ui.routes.ssh_keys import generate_ed25519_keypair

        with conn.cursor() as cur:
            cur.execute(
                "SELECT public_key FROM ssh_key_pairs "
                "WHERE tenant_id = %s AND key_type = 'deploy_key' "
                "LIMIT 1",
                (tenant_id,),
            )
            row = cur.fetchone()

        if row is not None:
            return row[0]

        # No deploy key yet — generate and persist one.
        public_key, private_key_encrypted = generate_ed25519_keypair()
        # ON CONFLICT guards the check-then-insert race: if a concurrent
        # first-time request for the same tenant won, our INSERT no-ops
        # (RETURNING yields no row) and we re-read the winner's public key so
        # both callers return the SAME key. Conflict target matches the partial
        # index ux_ssh_deploy_key_per_tenant (m13_002).
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ssh_key_pairs"
                " (name, public_key, private_key_encrypted, key_version, key_type, tenant_id)"
                " VALUES (%s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (tenant_id) WHERE key_type = 'deploy_key' DO NOTHING"
                " RETURNING public_key",
                (
                    f"deploy-key-tenant-{tenant_id}",
                    public_key,
                    private_key_encrypted,
                    1,
                    "deploy_key",
                    tenant_id,
                ),
            )
            row = cur.fetchone()
        if row is not None:
            return row[0]

        # Lost the race — a peer inserted the tenant's deploy key first.
        # Return their public key so the result is idempotent.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT public_key FROM ssh_key_pairs "
                "WHERE tenant_id = %s AND key_type = 'deploy_key' "
                "LIMIT 1",
                (tenant_id,),
            )
            row = cur.fetchone()
        return row[0]
