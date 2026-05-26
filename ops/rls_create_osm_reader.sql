-- ops/rls_create_osm_reader.sql — non-owner MCP read role for RLS enforcement.
-- ADR-0034 A5 / runbook §5.14 Bước 1. Idempotent + re-runnable.
--
-- The MCP :8002 process connects as this role so RLS on `embeddings` engages:
-- osm_reader is NON-owner, NON-superuser, NON-BYPASSRLS → subject to the
-- embeddings_tenant policy. It is granted EXACTLY what the :8002 process touches
-- at runtime — not just `embeddings`. Besides the ANN search, :8002 does:
--   * API-key auth        → SELECT api_keys (fail-closed 401 without it) + UPDATE last_used_at
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
GRANT CONNECT ON DATABASE odoo_semantic TO osm_reader;
GRANT USAGE   ON SCHEMA public          TO osm_reader;

-- 3. RLS target — SELECT only. NEVER grant write here.
GRANT SELECT ON TABLE embeddings TO osm_reader;

-- 4. Mandatory reads for the :8002 request path.
GRANT SELECT ON TABLE api_keys TO osm_reader;   -- API-key auth (fail-closed 401 without it)
GRANT UPDATE ON TABLE api_keys TO osm_reader;   -- last_used_at touch (best-effort)
GRANT SELECT ON TABLE profiles TO osm_reader;   -- resolve_tenant_scope + set_active_profile
GRANT SELECT ON TABLE repos    TO osm_reader;   -- repo URL display (best-effort)

-- 5. Session context (ADR-0029) — set_active_version/profile UPSERT + read.
--    api_key_session_state.api_key_id is an FK integer PK (no sequence).
GRANT SELECT, INSERT, UPDATE ON TABLE api_key_session_state TO osm_reader;

-- 6. Best-effort writes (failure is swallowed; granted to avoid noisy log warnings).
GRANT INSERT ON TABLE usage_log       TO osm_reader;
GRANT INSERT ON TABLE admin_audit_log TO osm_reader;

-- 7. Routers mounted on :8002 (feedback + tenant deploy-key — src/mcp/server.py).
GRANT SELECT, INSERT ON TABLE pattern_feedback TO osm_reader;
GRANT SELECT, INSERT ON TABLE ssh_key_pairs    TO osm_reader;

-- 8. Sequences backing the SERIAL PKs of the INSERT tables above.
GRANT USAGE ON SEQUENCE usage_log_id_seq        TO osm_reader;
GRANT USAGE ON SEQUENCE admin_audit_log_id_seq  TO osm_reader;
GRANT USAGE ON SEQUENCE pattern_feedback_id_seq TO osm_reader;
GRANT USAGE ON SEQUENCE ssh_key_pairs_id_seq    TO osm_reader;

-- NOTE: FORCE ROW LEVEL SECURITY (runbook §5.14 Bước 2) and the MCP DSN flip
-- (Bước 3) are performed by the cutover script AFTER this file runs — kept
-- separate so the role + grants can be (re)applied idempotently on their own.
