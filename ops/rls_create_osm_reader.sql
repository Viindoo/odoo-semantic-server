-- ops/rls_create_osm_reader.sql — non-owner MCP read role for RLS enforcement.
-- ADR-0034 A5 / runbook §5.14 Bước 1. Idempotent + re-runnable.
--
-- The MCP :8002 process connects as this role so RLS on `embeddings` engages:
-- osm_reader is NON-owner, NON-superuser, NON-BYPASSRLS → subject to the
-- embeddings_tenant policy. It is granted EXACTLY what the :8002 process touches
-- at runtime — not just `embeddings`. Besides the ANN search, :8002 does:
--   * API-key auth        → SELECT api_keys (fail-closed 401 without it) + UPDATE last_used_at
--                           + column-level SELECT (id, is_admin) on webui_users
--                             (owner_is_admin lookup — verify_api_key_full LEFT JOIN,
--                             f9ccc23). Column-level least-privilege: webui_users has
--                             NO RLS + holds password_hash/email/oauth_id; the auth
--                             path only needs the join key + is_admin flag.
--   * tenant scope/profile → SELECT profiles
--   * session pinning      → SELECT/INSERT/UPDATE api_key_session_state (ADR-0029)
--   * repo URL display     → SELECT repos
--   * usage + audit log    → INSERT usage_log / admin_audit_log (best-effort)
--   * feedback router      → SELECT/INSERT pattern_feedback   (mounted on :8002, server.py)
--   * deploy-key router    → SELECT/INSERT ssh_key_pairs      (mounted on :8002, server.py)
-- embeddings stays SELECT-only (the read tier must never write embeddings).
--
-- Password is supplied at run time (NEVER hardcoded). The cutover script
-- (Stage 1C) generates it and writes the same value into the MCP-only mcp.env DSN:
--   docker exec -i odoo-semantic-mcp-postgres-1 \
--     psql -U odoo_semantic -d odoo_semantic \
--     -v ON_ERROR_STOP=1 -v osm_pw="<generated>" \
--     -f - < ops/rls_create_osm_reader.sql

\set ON_ERROR_STOP on

-- 1. Role — create if absent, then (re)assert attributes + password on every run.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'osm_reader') THEN
    CREATE ROLE osm_reader LOGIN NOSUPERUSER NOINHERIT NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
END$$;

ALTER ROLE osm_reader
  LOGIN NOSUPERUSER NOINHERIT NOCREATEDB NOCREATEROLE NOBYPASSRLS
  PASSWORD :'osm_pw';

-- 2. Connection + schema usage.
GRANT CONNECT ON DATABASE :"db_name" TO osm_reader;
GRANT USAGE   ON SCHEMA public          TO osm_reader;

-- 3. RLS target — SELECT only. NEVER grant write here.
GRANT SELECT ON TABLE embeddings TO osm_reader;

-- 4. Mandatory reads for the :8002 request path.
GRANT SELECT ON TABLE api_keys TO osm_reader;   -- API-key auth (fail-closed 401 without it)
GRANT UPDATE ON TABLE api_keys TO osm_reader;   -- last_used_at touch (best-effort)
GRANT SELECT (id, is_admin) ON TABLE webui_users TO osm_reader; -- API-key auth owner_is_admin lookup (verify_api_key_full LEFT JOIN webui_users — f9ccc23); column-level (id, is_admin) least-privilege — webui_users has NO RLS + holds password_hash/email
GRANT SELECT ON TABLE profiles TO osm_reader;   -- resolve_tenant_scope + set_active_profile
GRANT SELECT ON TABLE repos    TO osm_reader;   -- repo URL display (best-effort)
-- M10B P0: per-API-key plan lookup + monthly quota counter (ADR-0039 control plane).
-- :8002 only SELECTs plans (seed-only via m13_006 — no runtime INSERT, so plans_id_seq
-- needs no USAGE grant). usage_counter is read on every authed request (quota gate)
-- and UPSERTed by the buffered flush task (_flush_usage_buffer_async); PK is
-- (api_key_id, period_yyyymm) so no SERIAL sequence is involved.
GRANT SELECT ON TABLE plans TO osm_reader;
GRANT SELECT, INSERT, UPDATE ON TABLE usage_counter TO osm_reader;
-- m13_008: waitlist_emails — admin viewer page can MCP-read without RLS silent-empty bug.
-- FastAPI writes as DB owner (no INSERT here). Defensive: added proactively so future
-- admin viewer tools work without requiring another cutover run.
GRANT SELECT ON TABLE waitlist_emails TO osm_reader;

-- WI-RV F-E: Admin Settings module tables (m13_010 + m13_011 + m13_012).
-- ----------------------------------------------------------------------
-- ``app_settings`` is read on every authed MCP request via the settings
-- overlay (``src/settings.py::get_setting``) — without SELECT, every read
-- falls through to the code default and the operator-tunable layer silently
-- becomes dead.  ``bootstrap_settings_safe`` (server.py startup) also
-- INSERTs catalogue rows ON CONFLICT DO NOTHING, so MCP needs INSERT here
-- too — without it the bootstrap log warning is the only signal that the
-- catalogue is missing, which has caused operator confusion in the past.
--
-- ``app_settings_history`` is WRITTEN only by FastAPI (DB owner) on admin
-- CRUD — MCP does NOT mutate it.  SELECT only so future MCP-side audit
-- read paths (e.g. read-only tenant override viewer) work without another
-- cutover script run.
--
-- ``ee_modules`` is read by ``src.data.ee_modules.get_ee_modules`` which
-- the MCP feature-check + capability-proof tools call to disambiguate
-- Community vs Enterprise modules.  Source-of-truth lives in the seed
-- migration; runtime MCP never writes.
--
-- ``patterns`` is read by ``recompute_sentinel_sha`` indirectly (via
-- ``_load_patterns_from_db``) on every reseed-gating check.  Today the
-- read path runs inside the indexer worker, but MCP-side curated pattern
-- lookups under M11 will need SELECT too.  Defensive grant included now
-- so the next M11 wave does not require a second cutover.
GRANT SELECT, INSERT ON TABLE app_settings           TO osm_reader;
GRANT SELECT          ON TABLE app_settings_history  TO osm_reader;
GRANT SELECT          ON TABLE ee_modules            TO osm_reader;
GRANT SELECT          ON TABLE patterns              TO osm_reader;
-- app_settings.id is BIGSERIAL and MCP (bootstrap_settings_safe) INSERTs into
-- it on startup, so osm_reader needs USAGE on its backing sequence too — see
-- §8.  Without the sequence USAGE the INSERT fails at nextval() with
-- "permission denied for sequence app_settings_id_seq" BEFORE the
-- ON CONFLICT DO NOTHING is ever evaluated (Postgres computes the column
-- default first).  This was the ADR-0042 deploy bug (hotfixed live):
-- granting INSERT on a table without USAGE on its serial/identity sequence is
-- an INCOMPLETE grant.  app_settings_history / ee_modules / patterns are
-- SELECT-only here (no INSERT) so they need no sequence USAGE.
--
-- NOTE on degraded modes: when MCP is denied permission on any of these
-- (e.g. running against an older RLS deployment where this grant block
-- hasn't been applied), every callsite falls through to a static
-- in-process default — see ``settings.py::_resolve_default`` and
-- ``ee_modules.py::_DEFAULT_EE_MODULES``.  Operators see WARN-level log
-- lines but the service does not 500.  Re-running this file is the fix.

-- 5. Session context (ADR-0029) — set_active_version/profile UPSERT + read.
--    api_key_session_state.api_key_id is an FK integer PK (no sequence).
GRANT SELECT, INSERT, UPDATE ON TABLE api_key_session_state TO osm_reader;

-- 6. Best-effort writes (failure is swallowed; granted to avoid noisy log warnings).
GRANT INSERT ON TABLE usage_log       TO osm_reader;
GRANT INSERT ON TABLE admin_audit_log TO osm_reader;

-- 7. Routers mounted on :8002 (feedback + tenant deploy-key — src/mcp/server.py).
GRANT SELECT, INSERT ON TABLE pattern_feedback TO osm_reader;
GRANT SELECT, INSERT ON TABLE ssh_key_pairs    TO osm_reader;

-- 8. Sequences backing the SERIAL/BIGSERIAL PKs of the INSERT tables above.
--    RULE: every table osm_reader has INSERT on AND that has a serial/identity
--    column MUST also have USAGE on its backing sequence — INSERT alone is an
--    incomplete grant (nextval() is denied before ON CONFLICT runs).  Tables
--    osm_reader INSERTs into whose PK is composite/non-serial (e.g.
--    api_key_session_state: FK api_key_id PK) have no sequence and are
--    intentionally absent below.
GRANT USAGE ON SEQUENCE usage_log_id_seq        TO osm_reader;
GRANT USAGE ON SEQUENCE admin_audit_log_id_seq  TO osm_reader;
GRANT USAGE ON SEQUENCE pattern_feedback_id_seq TO osm_reader;
GRANT USAGE ON SEQUENCE ssh_key_pairs_id_seq    TO osm_reader;
-- ADR-0042 Admin Settings: app_settings.id BIGSERIAL — MCP INSERTs catalogue
-- rows via bootstrap_settings_safe(), so the reader needs USAGE on its sequence.
GRANT USAGE ON SEQUENCE app_settings_id_seq     TO osm_reader;

-- M10B P1 billing tables (m13_014_billing_p1.sql)
-- subscriptions: SELECT for /account + /tenant portal pages.
-- billing_webhook_events: SELECT for admin viewer read.
-- NO INSERT/sequence grants: Activation API + webhook handler write as DB owner.
-- Same grants duplicated in the migration itself for deploy-safety per house convention
-- (migrations run via python -m src.db.migrate; this file runs only on cutover/ops).
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'osm_reader') THEN
        GRANT SELECT ON TABLE subscriptions          TO osm_reader;
        GRANT SELECT ON TABLE billing_webhook_events TO osm_reader;
    END IF;
END $$;

-- NOTE: FORCE ROW LEVEL SECURITY (runbook §5.14 Bước 2) and the MCP DSN flip
-- (Bước 3) are performed by the cutover script AFTER this file runs — kept
-- separate so the role + grants can be (re)applied idempotently on their own.
