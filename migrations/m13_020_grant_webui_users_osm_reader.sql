-- migrations/m13_020_grant_webui_users_osm_reader.sql
-- Grant osm_reader SELECT on webui_users (API-key auth owner_is_admin lookup).
--
-- Context (security refactor f9ccc23 / ADR-0034 RLS read split):
--   f9ccc23 reworked verify_api_key_full() in src/db/auth_registry.py to read the
--   key owner's is_admin flag for the read-side null-tenant escalation guard. It
--   does so via a single round-trip:
--       SELECT k.id, k.tenant_id, k.user_id,
--              COALESCE(u.is_admin, FALSE) AS owner_is_admin
--         FROM api_keys k
--         LEFT JOIN webui_users u ON u.id = k.user_id
--        WHERE k.key_hash = %s AND k.active = TRUE ...
--   The MCP :8002 process connects to Postgres as the non-owner role `osm_reader`
--   (ADR-0034 A5). osm_reader was granted SELECT on api_keys but NEVER on
--   webui_users, so on deploy this LEFT JOIN raised
--       psycopg2.errors.InsufficientPrivilege: permission denied for table webui_users
--   → EVERY authenticated MCP call 500'd. The columns actually read are only
--   u.id (the join key) and u.is_admin.
--
-- Deploy reality:
--   m13_019 introduced the webui_users READ at the auth choke-point but its header
--   wrongly assumed "no new tables → no osm_reader GRANT changes". The gap is a
--   read of an EXISTING table the reader never had SELECT on. This migration closes
--   that grant gap. Prod was hot-fixed live with
--       GRANT SELECT ON TABLE webui_users TO osm_reader;
--   so this migration is a pure idempotent no-op there (zero drift / no reconciliation).
--
-- Grant scope decision = FULL-TABLE `GRANT SELECT ON TABLE webui_users`:
--   (a) every existing osm_reader grant in ops/rls_create_osm_reader.sql is
--       full-table — consistency with the house convention;
--   (b) it matches the already-applied prod hotfix verbatim, so re-running this on
--       prod reconciles to a no-op (no drift to reconcile);
--   (c) the reader IS the MCP API-key auth authority — this read is on its hot path.
--   Least-privilege alternative considered + NOT chosen here: column-level
--   `GRANT SELECT (id, is_admin) ON webui_users TO osm_reader` would narrow the
--   reader away from password_hash / totp-related columns webui_users also holds.
--   We did not pick it: it diverges from the full-table convention and would NOT
--   match the prod hotfix, forcing a reconciliation step. Convention + no-reconcile
--   wins here; the reader is already a NOSUPERUSER/NOBYPASSRLS role and the auth
--   path never selects the secret columns.
--
-- ops/rls_create_osm_reader.sql stays the SSOT for the role + full grant set; this
-- grant is mirrored there. `python -m src.db.migrate` does NOT run that ops file, so
-- this self-contained, pg_roles-guarded GRANT keeps a migrate-only deploy correct.
--
-- Idempotent — safe to re-run. pg_roles guard keeps it safe on a DB without the role
-- (matches the m13_011 / m13_014 / m13_018 in-migration grant idiom).

DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'osm_reader') THEN
        GRANT SELECT ON TABLE webui_users TO osm_reader;
    END IF;
END $$;
