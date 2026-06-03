-- migrations/m13_020_grant_webui_users_osm_reader.sql
-- Grant osm_reader column-level SELECT (id, is_admin) on webui_users
-- (API-key auth owner_is_admin lookup).
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
--   → EVERY authenticated MCP call 500'd. The columns actually read on the
--   osm_reader path are ONLY u.id (the join key) and u.is_admin.
--
-- Deploy reality:
--   m13_019 introduced the webui_users READ at the auth choke-point but its header
--   wrongly assumed "no new tables → no osm_reader GRANT changes". The gap is a
--   read of an EXISTING table the reader never had SELECT on. This migration closes
--   that grant gap. Prod was hot-fixed live with the BROAD full-table grant
--       GRANT SELECT ON TABLE webui_users TO osm_reader;
--   so this migration REVOKE-then-GRANTs to converge that live full-table-hotfixed
--   prod (and any DB) DOWN to least-privilege column-level on deploy.
--
-- Grant scope decision = COLUMN-LEVEL `GRANT SELECT (id, is_admin) ON webui_users`:
--   webui_users has NO row-level security and holds secrets (password_hash, email,
--   oauth_id, totp-related columns). The osm_reader read path (verify_api_key_full)
--   only ever projects id (the join key) + is_admin, so column-level SELECT keeps
--   those secret columns unreadable by the read tier — strictly safer than the
--   broad full-table grant and sufficient for the auth lookup. The sibling
--   list_api_keys path that reads more webui_users columns runs on the :8003 owner
--   connection, NOT on osm_reader, so the read tier never needs the wider columns.
--   The REVOKE drops any pre-existing broad table-level SELECT (e.g. the prod
--   hotfix) so we converge to least-privilege; the column GRANT then re-establishes
--   exactly the two columns the auth path needs.
--
-- ops/rls_create_osm_reader.sql stays the SSOT for the role + full grant set; this
-- grant is mirrored there. `python -m src.db.migrate` does NOT run that ops file, so
-- this self-contained, pg_roles-guarded GRANT keeps a migrate-only deploy correct.
--
-- Idempotent — safe to re-run. pg_roles guard keeps it safe on a DB without the role
-- (matches the m13_011 / m13_014 / m13_018 in-migration grant idiom). The REVOKE is
-- a no-op if the broad grant is absent; the column GRANT is a no-op if already present.

DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'osm_reader') THEN
        -- Drop any pre-existing broad table-level SELECT (prod was hot-fixed with
        -- the full-table grant) so we converge to least-privilege column-level.
        REVOKE SELECT ON TABLE webui_users FROM osm_reader;
        -- osm_reader's only webui_users read (verify_api_key_full) projects just
        -- id (join key) + is_admin. Column-level keeps password_hash/email/oauth_id
        -- unreadable by the read tier (webui_users has NO RLS).
        GRANT SELECT (id, is_admin) ON TABLE webui_users TO osm_reader;
    END IF;
END $$;
