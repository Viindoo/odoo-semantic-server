-- migrations/m13_010_app_settings.sql
-- Admin Settings module — overlay store for 15 Tier-1 settings.
-- Code defaults remain in src/constants.py + src/settings_registry.py;
-- DB rows override at runtime via src/settings.get_setting() helper.
--
-- ADR-0041: Admin Settings architecture.
--
-- Two tables:
--   1. app_settings        — per-key JSONB overlay (system / tenant / per_key scope)
--   2. app_settings_history — immutable change log (orphan-safe for forensic)
--
-- Idempotent — safe to re-run.

-- ===========================================================================
-- 1. app_settings
-- ===========================================================================

CREATE TABLE IF NOT EXISTS app_settings (
    id               BIGSERIAL   PRIMARY KEY,
    key              TEXT        NOT NULL,
    value_json       JSONB       NOT NULL,
    category         TEXT        NOT NULL,
    scope            TEXT        NOT NULL DEFAULT 'system'
                                 CHECK (scope IN ('system', 'tenant', 'per_key')),
    tenant_id        INTEGER     REFERENCES tenants(id) ON DELETE CASCADE,
    data_type        TEXT        NOT NULL
                                 CHECK (data_type IN (
                                     'int', 'float', 'str', 'bool',
                                     'duration_seconds', 'list_str', 'struct'
                                 )),
    validation_json  JSONB       NOT NULL DEFAULT '{}'::jsonb,
    default_value    JSONB       NOT NULL,
    requires_restart BOOLEAN     NOT NULL DEFAULT FALSE,
    requires_reseed  BOOLEAN     NOT NULL DEFAULT FALSE,
    is_secret        BOOLEAN     NOT NULL DEFAULT FALSE,
    description      TEXT,
    updated_by       INTEGER     REFERENCES webui_users(id) ON DELETE SET NULL,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    change_reason    TEXT,
    CONSTRAINT app_settings_tenant_scope_consistency
        CHECK (
            (scope = 'system'  AND tenant_id IS NULL) OR
            (scope = 'tenant'  AND tenant_id IS NOT NULL) OR
            (scope = 'per_key' AND tenant_id IS NULL)
        )
);

-- One system row per key (Postgres NULL-aware unique via partial index).
CREATE UNIQUE INDEX IF NOT EXISTS uq_app_settings_system_key
    ON app_settings(key)
    WHERE scope = 'system' AND tenant_id IS NULL;

-- One tenant row per (key, tenant_id).
CREATE UNIQUE INDEX IF NOT EXISTS uq_app_settings_tenant_key
    ON app_settings(key, tenant_id)
    WHERE scope = 'tenant' AND tenant_id IS NOT NULL;

-- Per-key scope (Phase 2): one row per key, no tenant.
CREATE UNIQUE INDEX IF NOT EXISTS uq_app_settings_per_key
    ON app_settings(key)
    WHERE scope = 'per_key';

CREATE INDEX IF NOT EXISTS idx_app_settings_category
    ON app_settings(category);

CREATE INDEX IF NOT EXISTS idx_app_settings_scope_tenant
    ON app_settings(scope, tenant_id);

-- ===========================================================================
-- 2. app_settings_history
-- ===========================================================================

-- NOTE: setting_key intentionally has NO FK REFERENCES app_settings(key).
-- History rows must survive deletion of the parent setting row so forensic
-- analysis of a removed setting is still possible.  Orphaned history rows
-- (setting deleted from catalogue) are useful and expected.

CREATE TABLE IF NOT EXISTS app_settings_history (
    id            BIGSERIAL   PRIMARY KEY,
    setting_key   TEXT        NOT NULL,
    tenant_id     INTEGER     REFERENCES tenants(id) ON DELETE CASCADE,
    old_value     JSONB,
    new_value     JSONB       NOT NULL,
    changed_by    INTEGER     REFERENCES webui_users(id) ON DELETE SET NULL,
    changed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    change_reason TEXT,
    audit_log_id  BIGINT
);

CREATE INDEX IF NOT EXISTS idx_app_settings_history_key_time
    ON app_settings_history (setting_key, changed_at DESC);

-- ===========================================================================
-- 3. Optional FK to admin_audit_log (defensive — table may not exist yet)
-- ===========================================================================
-- admin_audit_log is created by m9_003; in a fresh deploy migrations run in
-- filename order, so m13_010 can safely reference it.  The DO block handles
-- re-entrant runs where the FK already exists and environments where the M9
-- migration has not yet been applied (e.g. a minimal test database).

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
         WHERE table_name = 'admin_audit_log'
    ) THEN
        BEGIN
            ALTER TABLE app_settings_history
                ADD CONSTRAINT app_settings_history_audit_log_fk
                FOREIGN KEY (audit_log_id)
                REFERENCES admin_audit_log(id) ON DELETE SET NULL;
        EXCEPTION
            WHEN duplicate_object THEN NULL;
        END;
    END IF;
END $$;

-- ===========================================================================
-- 4. Read-role grants for osm_reader (ADR-0034 RLS read split / ADR-0042)
-- ===========================================================================
-- These tables are GLOBAL config (not tenant-scoped) → no RLS policy on them,
-- but the MCP service connects as the non-owner role `osm_reader` and reads
-- app_settings / app_settings_history at runtime via get_setting(). Without
-- these grants the reads hit permission-denied, which get_setting() SWALLOWS
-- → silent fallback to in-process code defaults (the operator-tunable layer
-- goes dead with no 500 and no log). `python -m src.db.migrate` does NOT run
-- ops/rls_create_osm_reader.sql, so the grants MUST be self-contained here to
-- avoid a silent-degrade on every fresh deploy / CI / test DB.
--
-- app_settings needs INSERT (not just SELECT) because bootstrap_settings_safe()
-- UPSERTs catalogue rows (ON CONFLICT DO NOTHING) on MCP startup.
-- These GRANTs are idempotent (re-running a GRANT is a no-op) and match exactly
-- what ops/rls_create_osm_reader.sql grants (kept as the SSOT for the role's
-- password + full grant set; this block is the deploy-safety duplicate).
-- The pg_roles guard makes the migration safe on a DB without osm_reader
-- (e.g. minimal test databases that never create the read role).

DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'osm_reader') THEN
        GRANT SELECT, INSERT ON TABLE app_settings          TO osm_reader;
        GRANT SELECT          ON TABLE app_settings_history TO osm_reader;
    END IF;
END $$;
