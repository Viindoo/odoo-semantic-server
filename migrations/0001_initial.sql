-- migrations/0001_initial.sql
-- SQUASHED BASELINE — replaces the original 39-file migration chain.
--
-- This file is the authoritative single-pass schema for every new installation.
-- On an existing production database, yoyo tracks migrations by SHA256(migration_id).
-- Because this file KEEPS the name "0001_initial", its migration_id "0001_initial"
-- matches the existing _yoyo_migration record, so yoyo skips re-applying it on prod
-- (the migration_hash may differ but migrate.py's baseline-marking logic takes
-- precedence for fresh yoyo-not-yet-run detection).
--
-- EMBEDDINGS TABLE NOTE:
--   The embeddings base table (columns id through embedding_dim, HNSW index,
--   ux_embeddings_chunk constraint, and idx_embeddings_filter index) is created
--   by src/db/migrate.py::_EMBEDDINGS_SQL before yoyo runs. This file extends
--   that pre-existing table with the columns, constraints, indexes, and RLS
--   added by the former m13_001/003/004/018/021 migrations -- all guarded with
--   IF NOT EXISTS / DO-block checks so they are idempotent.
--
-- GRANT NOTE:
--   GRANTs to osm_reader are wrapped in pg_roles guards (IF EXISTS osm_reader).
--   They are no-ops on DBs without the role (CI, minimal test DBs). The
--   ops/rls_create_osm_reader.sql script is the SSOT for role creation + full
--   grant set; the in-migration grants are the deploy-safety duplicates.
--
-- IDEMPOTENCY:
--   Every DDL statement uses IF NOT EXISTS or is wrapped in a DO block with
--   an existence check. Safe to re-run.

-- ===========================================================================
-- 1. APPLICATION TABLES
-- ===========================================================================

-- profiles: indexing profile registry (one per Odoo version / repo set)
CREATE TABLE IF NOT EXISTS profiles (
    id                SERIAL    PRIMARY KEY,
    name              TEXT      NOT NULL UNIQUE,
    odoo_version      TEXT      NOT NULL,
    description       TEXT,
    created_at        TIMESTAMP DEFAULT NOW(),
    parent_profile_id INTEGER   REFERENCES profiles(id) ON DELETE RESTRICT,
    tenant_id         INTEGER,
    CONSTRAINT profiles_name_no_comma  CHECK (name NOT LIKE '%,%'),
    CONSTRAINT profiles_name_no_dunder CHECK (name NOT LIKE like_escape('\_\_%', '\'))
);

CREATE INDEX IF NOT EXISTS idx_profiles_parent    ON profiles (parent_profile_id);
CREATE INDEX IF NOT EXISTS idx_profiles_tenant_id ON profiles (tenant_id);

COMMENT ON COLUMN profiles.parent_profile_id IS
    'Self-FK for delta-repo hierarchy. ON DELETE RESTRICT. Application enforces cycle-free + version-match.';

-- repos: source repo registry per profile
CREATE TABLE IF NOT EXISTS repos (
    id              SERIAL    PRIMARY KEY,
    profile_id      INTEGER   REFERENCES profiles(id) ON DELETE CASCADE,
    url             TEXT      NOT NULL,
    branch          TEXT      NOT NULL,
    local_path      TEXT      NOT NULL,
    status          TEXT      DEFAULT 'pending',
    last_indexed_at TIMESTAMP,
    head_sha        TEXT,
    error_msg       TEXT,
    created_at      TIMESTAMP DEFAULT NOW(),
    ssh_key_id      INTEGER,
    clone_status    TEXT      NOT NULL DEFAULT 'manual',
    clone_error_msg TEXT,
    tenant_id       INTEGER,
    CONSTRAINT repos_url_branch_profile_key UNIQUE (url, branch, profile_id)
);

CREATE INDEX IF NOT EXISTS idx_repos_profile_id ON repos (profile_id);
CREATE INDEX IF NOT EXISTS idx_repos_tenant_id  ON repos (tenant_id);

-- tenants: authorization boundary for multi-tenant pooling (ADR-0034)
CREATE TABLE IF NOT EXISTS tenants (
    id                  SERIAL      PRIMARY KEY,
    name                TEXT        NOT NULL UNIQUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    active              BOOLEAN     NOT NULL DEFAULT TRUE,
    owner_user_id       INTEGER,
    billing_email       TEXT,
    seat_limit_override INTEGER
);

-- ssh_key_pairs: encrypted SSH key storage per tenant / per repo
CREATE TABLE IF NOT EXISTS ssh_key_pairs (
    id                    SERIAL    PRIMARY KEY,
    name                  TEXT      NOT NULL,
    public_key            TEXT      NOT NULL,
    private_key_encrypted TEXT      NOT NULL,
    key_version           INTEGER   NOT NULL DEFAULT 1,
    created_at            TIMESTAMP DEFAULT NOW(),
    tenant_id             INTEGER   REFERENCES tenants(id) ON DELETE CASCADE,
    key_type              TEXT      NOT NULL DEFAULT 'access_key'
                              CHECK (key_type IN ('deploy_key', 'access_key'))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_ssh_deploy_key_per_tenant
    ON ssh_key_pairs (tenant_id)
    WHERE key_type = 'deploy_key';

CREATE INDEX IF NOT EXISTS idx_ssh_key_pairs_tenant ON ssh_key_pairs (tenant_id);

-- Add deferred FKs from repos/profiles to tenants/ssh_key_pairs (tables now exist)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'repos_ssh_key_id_fkey' AND conrelid = 'repos'::regclass
    ) THEN
        ALTER TABLE repos ADD CONSTRAINT repos_ssh_key_id_fkey
            FOREIGN KEY (ssh_key_id) REFERENCES ssh_key_pairs(id) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'repos_tenant_id_fkey' AND conrelid = 'repos'::regclass
    ) THEN
        ALTER TABLE repos ADD CONSTRAINT repos_tenant_id_fkey
            FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'profiles_tenant_id_fkey' AND conrelid = 'profiles'::regclass
    ) THEN
        ALTER TABLE profiles ADD CONSTRAINT profiles_tenant_id_fkey
            FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE;
    END IF;
END $$;

-- plans: billing plan catalog (ADR-0039)
CREATE TABLE IF NOT EXISTS plans (
    id                    SERIAL      PRIMARY KEY,
    slug                  TEXT        NOT NULL UNIQUE,
    display_name          TEXT        NOT NULL,
    quota_calls_per_month INTEGER     NOT NULL,
    rate_limit_rpm        INTEGER     NOT NULL,
    seat_limit            INTEGER     NOT NULL DEFAULT 1,
    is_public             BOOLEAN     NOT NULL DEFAULT FALSE,
    metadata              JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    price_cents           BIGINT      NOT NULL DEFAULT 0,
    currency              TEXT        NOT NULL DEFAULT 'USD',
    billing_interval      TEXT        NOT NULL DEFAULT 'free',
    trial_days            INTEGER     NOT NULL DEFAULT 0,
    is_archived           BOOLEAN     NOT NULL DEFAULT FALSE,
    prices                JSONB       NOT NULL DEFAULT '{}'::jsonb,
    pricing_model         TEXT        NOT NULL DEFAULT 'flat',
    min_seats             INTEGER,
    CONSTRAINT plans_billing_interval_check CHECK (billing_interval IN ('free', 'monthly', 'annual', 'one_time')),
    CONSTRAINT plans_currency_iso4217      CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT plans_min_seats_check       CHECK (min_seats IS NULL OR min_seats >= 1),
    CONSTRAINT plans_price_cents_nonneg    CHECK (price_cents >= 0),
    CONSTRAINT plans_pricing_model_check   CHECK (pricing_model IN ('flat', 'per_seat')),
    CONSTRAINT plans_trial_days_nonneg     CHECK (trial_days >= 0)
);

-- api_keys: MCP access tokens
CREATE TABLE IF NOT EXISTS api_keys (
    id                   SERIAL    PRIMARY KEY,
    name                 TEXT      NOT NULL,
    key_hash             TEXT      NOT NULL UNIQUE,
    key_prefix           TEXT      NOT NULL,
    active               BOOLEAN   DEFAULT TRUE,
    created_at           TIMESTAMP DEFAULT NOW(),
    last_used_at         TIMESTAMP,
    tenant_id            INTEGER   REFERENCES tenants(id) ON DELETE CASCADE,
    plan_id              INTEGER   NOT NULL REFERENCES plans(id),
    rate_limit_override  INTEGER,
    quota_override       INTEGER,
    user_id              INTEGER,
    expires_at           TIMESTAMPTZ,
    CONSTRAINT api_keys_quota_override_nonneg      CHECK (quota_override IS NULL OR quota_override >= 0),
    CONSTRAINT api_keys_rate_limit_override_nonneg CHECK (rate_limit_override IS NULL OR rate_limit_override >= 0)
);

CREATE INDEX IF NOT EXISTS idx_api_keys_tenant_id ON api_keys (tenant_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_user_id   ON api_keys (user_id);

-- usage_log: per-call billing / analytics log
CREATE TABLE IF NOT EXISTS usage_log (
    id          BIGSERIAL PRIMARY KEY,
    api_key_id  INTEGER   REFERENCES api_keys(id) ON DELETE SET NULL,
    tool_name   TEXT      NOT NULL,
    called_at   TIMESTAMP DEFAULT NOW(),
    response_ms INTEGER
);

CREATE INDEX IF NOT EXISTS idx_usage_log_api_key   ON usage_log (api_key_id);
CREATE INDEX IF NOT EXISTS idx_usage_log_called_at ON usage_log (called_at);

-- usage_counter: atomic monthly quota counter (ADR-0039)
CREATE TABLE IF NOT EXISTS usage_counter (
    api_key_id    INTEGER     NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
    period_yyyymm TEXT        NOT NULL,
    call_count    INTEGER     NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (api_key_id, period_yyyymm)
);

CREATE INDEX IF NOT EXISTS usage_counter_period_idx ON usage_counter (period_yyyymm);

-- api_key_session_state: per-key sticky context (ADR-0029)
CREATE TABLE IF NOT EXISTS api_key_session_state (
    api_key_id   INTEGER PRIMARY KEY REFERENCES api_keys(id) ON DELETE CASCADE,
    odoo_version TEXT,
    profile_name TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_api_key_session_state_updated_at
    ON api_key_session_state (updated_at);

COMMENT ON TABLE api_key_session_state IS
    'Per-API-key sticky session state — odoo_version + profile_name '
    'set via set_active_version()/set_active_profile() MCP tools. '
    '24h sliding TTL: updated_at older than 24h triggers fallback to '
    '_latest_version(). One row per api_key_id (PK). See ADR-0029.';
COMMENT ON COLUMN api_key_session_state.api_key_id IS
    'Foreign key to api_keys(id). ON DELETE CASCADE ensures automatic cleanup.';
COMMENT ON COLUMN api_key_session_state.odoo_version IS
    'Currently-active Odoo version context (e.g., "17.0", "16.0"). '
    'NULL = not yet set; fallback to _latest_version().';
COMMENT ON COLUMN api_key_session_state.profile_name IS
    'Currently-active profile name (e.g., "my-erp-prod", "custom-addon-lib"). '
    'NULL = not yet set; fallback to user''s default profile.';
COMMENT ON COLUMN api_key_session_state.updated_at IS
    'Timestamp of last state update. Used for 24h sliding TTL: '
    'if updated_at < NOW() - interval ''24h'', application treats state as expired.';

-- pattern_feedback: thumbs up/down per pattern per API key
CREATE TABLE IF NOT EXISTS pattern_feedback (
    id              SERIAL      PRIMARY KEY,
    pattern_node_id TEXT        NOT NULL,
    api_key_id      INTEGER     REFERENCES api_keys(id) ON DELETE SET NULL,
    rating          TEXT        NOT NULL CHECK (rating IN ('up', 'down')),
    comment         TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pattern_feedback_node ON pattern_feedback (pattern_node_id);

-- indexer_jobs: async indexer job queue
CREATE TABLE IF NOT EXISTS indexer_jobs (
    id           SERIAL      PRIMARY KEY,
    profile_name TEXT        NOT NULL,
    status       TEXT        NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'running', 'done', 'error')),
    pid          INTEGER,
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ,
    error_msg    TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_indexer_jobs_profile ON indexer_jobs (profile_name);
CREATE INDEX IF NOT EXISTS ix_indexer_jobs_status  ON indexer_jobs (status);
CREATE INDEX IF NOT EXISTS ix_indexer_jobs_created ON indexer_jobs (created_at DESC);

-- waitlist_emails: pre-launch email capture (no plan CHECK -- removed by m13_017)
CREATE TABLE IF NOT EXISTS waitlist_emails (
    id         SERIAL      PRIMARY KEY,
    email      TEXT        NOT NULL UNIQUE,
    plan       TEXT,
    source     TEXT        NOT NULL DEFAULT 'pricing-page',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS waitlist_emails_created_at_idx ON waitlist_emails (created_at DESC);

-- ===========================================================================
-- 2. WEB UI + AUTH TABLES
-- ===========================================================================

-- webui_users: admin web UI accounts (ADR-0011)
CREATE TABLE IF NOT EXISTS webui_users (
    username          VARCHAR(64)  PRIMARY KEY,
    password_hash     VARCHAR(255),
    is_admin          BOOLEAN      DEFAULT FALSE,
    is_active         BOOLEAN      DEFAULT TRUE,
    created_at        TIMESTAMP    DEFAULT NOW(),
    id                INTEGER      NOT NULL,
    email             TEXT,
    mfa_enabled       BOOLEAN      NOT NULL DEFAULT FALSE,
    email_verified    BOOLEAN      NOT NULL DEFAULT FALSE,
    terms_accepted_at TIMESTAMPTZ,
    oauth_provider    TEXT,
    oauth_id          TEXT,
    role              TEXT         NOT NULL DEFAULT 'admin'
                          CHECK (role IN ('admin', 'viewer'))
);

CREATE SEQUENCE IF NOT EXISTS webui_users_id_seq AS INTEGER;
ALTER TABLE webui_users ALTER COLUMN id SET DEFAULT nextval('webui_users_id_seq');
ALTER SEQUENCE webui_users_id_seq OWNED BY webui_users.id;

CREATE UNIQUE INDEX IF NOT EXISTS ux_webui_users_id    ON webui_users (id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_webui_users_email ON webui_users (email)
    WHERE email IS NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
         WHERE constraint_name = 'webui_users_email_unique'
           AND table_name = 'webui_users'
    ) THEN
        ALTER TABLE webui_users ADD CONSTRAINT webui_users_email_unique UNIQUE (email);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
         WHERE constraint_name = 'webui_users_id_unique'
           AND table_name = 'webui_users'
    ) THEN
        ALTER TABLE webui_users ADD CONSTRAINT webui_users_id_unique UNIQUE (id);
    END IF;
END $$;

-- Deferred FKs from api_keys.user_id and tenants.owner_user_id -> webui_users(id)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'api_keys_user_id_fkey' AND conrelid = 'api_keys'::regclass
    ) THEN
        ALTER TABLE api_keys ADD CONSTRAINT api_keys_user_id_fkey
            FOREIGN KEY (user_id) REFERENCES webui_users(id) ON DELETE CASCADE;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'tenants_owner_user_id_fkey' AND conrelid = 'tenants'::regclass
    ) THEN
        ALTER TABLE tenants ADD CONSTRAINT tenants_owner_user_id_fkey
            FOREIGN KEY (owner_user_id) REFERENCES webui_users(id);
    END IF;
END $$;

-- tenant_members: RBAC M:N join (ADR-0038)
CREATE TABLE IF NOT EXISTS tenant_members (
    user_id   INTEGER     NOT NULL REFERENCES webui_users(id) ON DELETE CASCADE,
    tenant_id INTEGER     NOT NULL REFERENCES tenants(id)     ON DELETE CASCADE,
    role      TEXT        NOT NULL DEFAULT 'member'
                  CHECK (role IN ('member', 'tenant_admin')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, tenant_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_members_tenant ON tenant_members (tenant_id);

-- active_sessions: server-side session store (ADR-0011)
CREATE TABLE IF NOT EXISTS active_sessions (
    session_id      TEXT        PRIMARY KEY,
    user_id         INTEGER     NOT NULL REFERENCES webui_users(id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '8 hours'),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ip_address      INET,
    user_agent      TEXT,
    mfa_verified_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_active_sessions_user_id ON active_sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user_id        ON active_sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires        ON active_sessions (expires_at);

-- email_verifications: token store for email verify and password reset
CREATE TABLE IF NOT EXISTS email_verifications (
    token      TEXT        PRIMARY KEY,
    user_id    INTEGER     NOT NULL,
    purpose    TEXT        NOT NULL DEFAULT 'email_verify'
                   CHECK (purpose IN ('email_verify', 'password_reset')),
    token_hash TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at    TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_email_verif_user_id    ON email_verifications (user_id);
CREATE INDEX IF NOT EXISTS idx_email_verif_token_hash ON email_verifications (token_hash);
CREATE INDEX IF NOT EXISTS idx_email_verify_user      ON email_verifications (user_id);
CREATE INDEX IF NOT EXISTS idx_email_verify_expires   ON email_verifications (expires_at);
CREATE INDEX IF NOT EXISTS ix_email_verif_token       ON email_verifications (token);
CREATE INDEX IF NOT EXISTS ix_email_verif_user        ON email_verifications (user_id);
CREATE INDEX IF NOT EXISTS ix_email_verif_created     ON email_verifications (created_at DESC);

-- admin_audit_log: non-repudiation log (ADR-0021)
CREATE TABLE IF NOT EXISTS admin_audit_log (
    id         BIGSERIAL   PRIMARY KEY,
    actor      TEXT        NOT NULL,
    action     TEXT        NOT NULL,
    target     TEXT,
    success    BOOLEAN     NOT NULL DEFAULT TRUE,
    detail     JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_admin_audit_created  ON admin_audit_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_actor_created  ON admin_audit_log (actor, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action_created ON admin_audit_log (action, created_at DESC);

-- login_attempts: Postgres-backed rate limiting (ADR-0011)
CREATE TABLE IF NOT EXISTS login_attempts (
    id           BIGSERIAL   PRIMARY KEY,
    identifier   TEXT        NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    success      BOOLEAN     NOT NULL,
    ip_address   INET,
    user_agent   TEXT
);

CREATE INDEX IF NOT EXISTS idx_login_attempts_identifier_time
    ON login_attempts (identifier, attempted_at DESC);
CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_time
    ON login_attempts (ip_address, attempted_at DESC);

-- totp_secrets: Fernet-encrypted TOTP seed storage (ADR-0022)
CREATE TABLE IF NOT EXISTS totp_secrets (
    user_id           INTEGER     PRIMARY KEY REFERENCES webui_users(id) ON DELETE CASCADE,
    secret_encrypted  TEXT        NOT NULL,
    enabled           BOOLEAN     NOT NULL DEFAULT FALSE,
    enrolled_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    backup_codes_hash JSONB       NOT NULL DEFAULT '[]'::jsonb,
    last_used_at      TIMESTAMPTZ
);

-- key_rotation_log: FERNET key rotation audit trail
CREATE TABLE IF NOT EXISTS key_rotation_log (
    id         BIGSERIAL   PRIMARY KEY,
    rotated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor      TEXT        NOT NULL,
    row_count  INTEGER     NOT NULL,
    old_key_id TEXT        NOT NULL,
    new_key_id TEXT        NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_key_rotation_log_time ON key_rotation_log (rotated_at DESC);

-- ===========================================================================
-- 3. BILLING TABLES (ADR-0039)
-- ===========================================================================

-- subscriptions: entitlement records
CREATE TABLE IF NOT EXISTS subscriptions (
    id                            SERIAL      PRIMARY KEY,
    plan_id                       INTEGER     NOT NULL REFERENCES plans(id),
    claimed_user_id               INTEGER     REFERENCES webui_users(id) ON DELETE SET NULL,
    api_key_id                    INTEGER     REFERENCES api_keys(id)    ON DELETE SET NULL,
    tenant_id                     INTEGER     REFERENCES tenants(id)     ON DELETE SET NULL,
    buyer_email                   TEXT,
    status                        TEXT        NOT NULL DEFAULT 'pending',
    seats                         INTEGER     NOT NULL DEFAULT 1,
    source                        TEXT        NOT NULL DEFAULT 'polar',
    external_ref                  TEXT,
    amount_cents                  BIGINT,
    currency                      TEXT,
    billing_interval              TEXT,
    current_period_start          TIMESTAMPTZ,
    current_period_end            TIMESTAMPTZ,
    trial_ends_at                 TIMESTAMPTZ,
    cancelled_at                  TIMESTAMPTZ,
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_event_at                 TIMESTAMPTZ,
    cancel_at_period_end          BOOLEAN     NOT NULL DEFAULT FALSE,
    buyer_type                    TEXT,
    withdrawal_waiver_accepted_at TIMESTAMPTZ,
    CONSTRAINT subscriptions_billing_interval_check CHECK (billing_interval IS NULL OR billing_interval IN ('free', 'monthly', 'annual', 'one_time')),
    CONSTRAINT subscriptions_buyer_type_check       CHECK (buyer_type IN ('business', 'consumer')),
    CONSTRAINT subscriptions_currency_iso4217       CHECK (currency IS NULL OR currency ~ '^[A-Z]{3}$'),
    CONSTRAINT subscriptions_no_orphan_active       CHECK (
        status NOT IN ('active', 'trialing')
        OR claimed_user_id IS NOT NULL
        OR api_key_id IS NOT NULL
        OR tenant_id IS NOT NULL
        OR buyer_email IS NOT NULL
    ),
    CONSTRAINT subscriptions_seats_positive         CHECK (seats > 0),
    CONSTRAINT subscriptions_source_check           CHECK (source IN ('polar', 'erp', 'admin', 'promo')),
    CONSTRAINT subscriptions_status_check           CHECK (status IN ('pending', 'active', 'past_due', 'cancelled', 'expired', 'trialing', 'refunded')),
    CONSTRAINT subscriptions_source_external_ref_key UNIQUE (source, external_ref)
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id
    ON subscriptions (claimed_user_id) WHERE claimed_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_subscriptions_api_key_id
    ON subscriptions (api_key_id)      WHERE api_key_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_subscriptions_tenant_id
    ON subscriptions (tenant_id)       WHERE tenant_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_subscriptions_plan_status
    ON subscriptions (plan_id, status);
CREATE INDEX IF NOT EXISTS idx_subscriptions_buyer_email
    ON subscriptions (buyer_email)
    WHERE buyer_email IS NOT NULL AND claimed_user_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_subscriptions_waiver_consumer
    ON subscriptions (withdrawal_waiver_accepted_at)
    WHERE buyer_type = 'consumer' AND withdrawal_waiver_accepted_at IS NOT NULL;

-- billing_webhook_events: idempotency ledger for billing webhooks
CREATE TABLE IF NOT EXISTS billing_webhook_events (
    id               BIGSERIAL   PRIMARY KEY,
    vendor           TEXT        NOT NULL CHECK (vendor IN ('polar', 'erp', 'test')),
    event_id         TEXT        NOT NULL,
    event_type       TEXT        NOT NULL,
    signature_valid  BOOLEAN     NOT NULL DEFAULT FALSE,
    payload          JSONB       NOT NULL,
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at     TIMESTAMPTZ,
    processing_error TEXT,
    subscription_id  INTEGER     REFERENCES subscriptions(id) ON DELETE SET NULL,
    CONSTRAINT billing_webhook_events_vendor_event_unique UNIQUE (vendor, event_id)
);

CREATE INDEX IF NOT EXISTS idx_bwe_vendor_received
    ON billing_webhook_events (vendor, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_bwe_unprocessed
    ON billing_webhook_events (received_at) WHERE processed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_bwe_subscription
    ON billing_webhook_events (subscription_id) WHERE subscription_id IS NOT NULL;

-- ===========================================================================
-- 4. ADMIN SETTINGS TABLES (ADR-0042)
-- ===========================================================================

-- app_settings: runtime config overlay store
CREATE TABLE IF NOT EXISTS app_settings (
    id               BIGSERIAL   PRIMARY KEY,
    key              TEXT        NOT NULL,
    value_json       JSONB       NOT NULL,
    category         TEXT        NOT NULL,
    scope            TEXT        NOT NULL DEFAULT 'system'
                         CHECK (scope IN ('system', 'tenant', 'per_key')),
    tenant_id        INTEGER     REFERENCES tenants(id) ON DELETE CASCADE,
    data_type        TEXT        NOT NULL
                         CHECK (data_type IN ('int', 'float', 'str', 'bool', 'duration_seconds', 'list_str', 'struct')),
    validation_json  JSONB       NOT NULL DEFAULT '{}'::jsonb,
    default_value    JSONB       NOT NULL,
    requires_restart BOOLEAN     NOT NULL DEFAULT FALSE,
    requires_reseed  BOOLEAN     NOT NULL DEFAULT FALSE,
    is_secret        BOOLEAN     NOT NULL DEFAULT FALSE,
    description      TEXT,
    updated_by       INTEGER     REFERENCES webui_users(id) ON DELETE SET NULL,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    change_reason    TEXT,
    CONSTRAINT app_settings_tenant_scope_consistency CHECK (
        (scope = 'system'  AND tenant_id IS NULL) OR
        (scope = 'tenant'  AND tenant_id IS NOT NULL) OR
        (scope = 'per_key' AND tenant_id IS NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_app_settings_system_key
    ON app_settings (key)
    WHERE scope = 'system' AND tenant_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_app_settings_tenant_key
    ON app_settings (key, tenant_id)
    WHERE scope = 'tenant' AND tenant_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_app_settings_per_key
    ON app_settings (key)
    WHERE scope = 'per_key';
CREATE INDEX IF NOT EXISTS idx_app_settings_category
    ON app_settings (category);
CREATE INDEX IF NOT EXISTS idx_app_settings_scope_tenant
    ON app_settings (scope, tenant_id);

-- app_settings_history: immutable change log (orphan rows intentional for forensics)
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

-- FK from app_settings_history.audit_log_id -> admin_audit_log (both tables exist now)
DO $$
BEGIN
    BEGIN
        ALTER TABLE app_settings_history
            ADD CONSTRAINT app_settings_history_audit_log_fk
            FOREIGN KEY (audit_log_id) REFERENCES admin_audit_log(id) ON DELETE SET NULL;
    EXCEPTION WHEN duplicate_object THEN NULL;
    END;
END $$;

-- ee_modules: EE confusion guard catalogue (ADR-0042)
CREATE TABLE IF NOT EXISTS ee_modules (
    id            SERIAL      PRIMARY KEY,
    name          TEXT        NOT NULL UNIQUE,
    since_version TEXT,
    vt_equivalent TEXT,
    description   TEXT,
    deprecated    BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by    INTEGER     REFERENCES webui_users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_ee_modules_name ON ee_modules (name);

-- patterns: curated pattern catalogue (ADR-0009, ADR-0042)
CREATE TABLE IF NOT EXISTS patterns (
    pattern_id        TEXT        PRIMARY KEY,
    intent_keywords   TEXT[]      NOT NULL DEFAULT '{}',
    file_ref          TEXT        NOT NULL,
    snippet_text      TEXT        NOT NULL,
    gotchas           JSONB       NOT NULL DEFAULT '[]'::jsonb,
    odoo_version_min  TEXT        NOT NULL,
    odoo_version_max  TEXT,
    language          TEXT        NOT NULL CHECK (language IN ('python', 'xml', 'js')),
    core_symbol_names TEXT[]      NOT NULL DEFAULT '{}',
    metadata          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by        INTEGER     REFERENCES webui_users(id) ON DELETE SET NULL,
    soft_deleted      BOOLEAN     NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_patterns_intent_keywords_gin
    ON patterns USING GIN (intent_keywords);
CREATE INDEX IF NOT EXISTS idx_patterns_language    ON patterns (language);
CREATE INDEX IF NOT EXISTS idx_patterns_version_min ON patterns (odoo_version_min);

-- ===========================================================================
-- 5. EMBEDDINGS TABLE EXTENSIONS
--
--   migrate.py _EMBEDDINGS_SQL creates the embeddings table with columns:
--     id, chunk_type, module, odoo_version, entity_name, model_name,
--     file_path, chunk_idx, content, vec(1024), indexed_at,
--     embedding_model, embedding_dim
--   Plus: ux_embeddings_chunk UNIQUE (6-col), idx_embeddings_vec HNSW,
--         idx_embeddings_filter (3-col: odoo_version, chunk_type, module)
--
--   This section extends that base table to its final schema state.
-- ===========================================================================

DO $$
BEGIN
  -- Skip everything when embeddings table does not exist (no pgvector on this DB).
  IF to_regclass('public.embeddings') IS NULL THEN
    RETURN;
  END IF;

  -- 5a. Add profile_name column (m13_001: initially nullable for backfill).
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_name = 'embeddings' AND column_name = 'profile_name'
       AND table_schema = 'public'
  ) THEN
    ALTER TABLE embeddings ADD COLUMN profile_name TEXT;
  END IF;

  -- 5b. Rebuild ux_embeddings_chunk to include profile_name with NULLS NOT DISTINCT
  --     (m13_001). Drop old 6-col version, add 7-col version.
  IF EXISTS (
    SELECT 1 FROM information_schema.table_constraints
     WHERE constraint_name = 'ux_embeddings_chunk' AND table_name = 'embeddings'
  ) AND NOT EXISTS (
    SELECT 1 FROM information_schema.constraint_column_usage
     WHERE constraint_name = 'ux_embeddings_chunk' AND column_name = 'profile_name'
  ) THEN
    ALTER TABLE embeddings DROP CONSTRAINT ux_embeddings_chunk;
    ALTER TABLE embeddings ADD CONSTRAINT ux_embeddings_chunk
      UNIQUE NULLS NOT DISTINCT
      (chunk_type, module, odoo_version, entity_name, file_path, chunk_idx, profile_name);
  END IF;

  -- 5c. Rebuild idx_embeddings_filter to include profile_name (m13_001).
  --     The base table has (odoo_version, chunk_type, module).
  --     Final schema needs (odoo_version, chunk_type, module, profile_name).
  --     We check specifically whether idx_embeddings_filter covers profile_name
  --     by joining pg_indexes to pin the specific index, not just any index on embeddings.
  IF NOT EXISTS (
    SELECT 1
      FROM pg_indexes ix
      JOIN pg_class tc ON tc.relname = ix.tablename AND tc.relnamespace = 'public'::regnamespace
      JOIN pg_class ic ON ic.relname = ix.indexname AND ic.relnamespace = 'public'::regnamespace
      JOIN pg_index i ON i.indexrelid = ic.oid AND i.indrelid = tc.oid
      JOIN pg_attribute a ON a.attrelid = tc.oid AND a.attnum = ANY(i.indkey)
     WHERE ix.schemaname = 'public'
       AND ix.tablename = 'embeddings'
       AND ix.indexname = 'idx_embeddings_filter'
       AND a.attname = 'profile_name'
  ) THEN
    DROP INDEX IF EXISTS idx_embeddings_filter;
    CREATE INDEX IF NOT EXISTS idx_embeddings_filter
        ON embeddings (odoo_version, chunk_type, module, profile_name);
  END IF;

  -- 5d. Add provenance columns (m13_003).
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='embeddings' AND column_name='line_start' AND table_schema='public') THEN
    ALTER TABLE embeddings ADD COLUMN line_start INTEGER;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='embeddings' AND column_name='repo' AND table_schema='public') THEN
    ALTER TABLE embeddings ADD COLUMN repo TEXT;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='embeddings' AND column_name='repo_id' AND table_schema='public') THEN
    ALTER TABLE embeddings ADD COLUMN repo_id INTEGER;
  END IF;

  -- 5e. Enable RLS -- armed-but-dormant (owner bypass); FORCE deferred to ops runbook.
  ALTER TABLE embeddings ENABLE ROW LEVEL SECURITY;

  -- 5f. Backfill NULL profile_name -> '__global__' before setting NOT NULL (m13_021 step 1).
  --     On a fresh install: no rows, this is a no-op.
  UPDATE embeddings
     SET profile_name = '__global__'
   WHERE profile_name IS NULL;

  -- 5g. Set profile_name NOT NULL (m13_021 step 2).
  ALTER TABLE embeddings ALTER COLUMN profile_name SET NOT NULL;

  -- 5h. Sentinel CHECK: only pattern catalogue rows may carry '__global__' (m13_021 step 4).
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conname = 'ck_embeddings_global_sentinel_scope'
       AND conrelid = 'public.embeddings'::regclass
  ) THEN
    ALTER TABLE embeddings
      ADD CONSTRAINT ck_embeddings_global_sentinel_scope
      CHECK (
        profile_name <> '__global__'
        OR (chunk_type = 'pattern_example' AND module = '__patterns__')
      ) NOT VALID;
  END IF;
END $$;

-- Validate the sentinel CHECK (separate block so no-pgvector path is skipped cleanly).
DO $$
BEGIN
  IF to_regclass('public.embeddings') IS NULL THEN
    RETURN;
  END IF;
  ALTER TABLE embeddings VALIDATE CONSTRAINT ck_embeddings_global_sentinel_scope;
END $$;

-- idx_embeddings_model partial index (m13_018).
-- NOTE: m13_018 used CREATE INDEX CONCURRENTLY (requires autocommit). yoyo runs
-- this file inside a transaction, so we use regular (non-CONCURRENTLY) CREATE.
-- On a fresh empty table this is instant and equivalent; on prod m13_018 already
-- built the index concurrently so this is an idempotent IF NOT EXISTS no-op.
DO $$
BEGIN
  IF to_regclass('public.embeddings') IS NOT NULL THEN
    CREATE INDEX IF NOT EXISTS idx_embeddings_model
        ON embeddings (embedding_model)
        WHERE embedding_model IS NOT NULL;
  END IF;
END $$;

-- RLS policy: final sentinel form (m13_021 step 5 -- '__global__' not IS NULL).
DO $$
BEGIN
  IF to_regclass('public.embeddings') IS NULL THEN
    RETURN;
  END IF;
  DROP POLICY IF EXISTS embeddings_tenant ON embeddings;
  CREATE POLICY embeddings_tenant ON embeddings
  USING (
      current_setting('app.allowed_profiles', true) = '*'
      OR profile_name = '__global__'
      OR profile_name = ANY (
           string_to_array(current_setting('app.allowed_profiles', true), ',')
      )
  );
END $$;

-- ===========================================================================
-- 6. MASTER DATA SEED (idempotent)
-- ===========================================================================

-- 6a. Plans: final state (free-grandfathered was id=1, deleted by m13_013; remaining plans).
--
--     SEQUENCE NOTE: free-grandfathered (id=1) was inserted then deleted, leaving a gap.
--     On prod the remaining plans have ids: free=2, pro=3, team=4, unlimited=5.
--     On a fresh install we advance the sequence to 1 (consuming id=1 as the "ghost"
--     of the deleted plan) then insert the 4 plans which auto-get ids 2-5. This
--     preserves the prod canonical id assignment that api_keys.plan_id DEFAULT relies on.
--     ON CONFLICT (slug) DO NOTHING preserves admin edits on re-run.
DO $$
BEGIN
    -- Advance sequence past 1 so first INSERT gets id=2 (matches prod).
    -- setval with is_called=true means the NEXT nextval() returns 2.
    -- Only advance when the sequence is still at its initial value (no plans inserted yet).
    IF NOT EXISTS (SELECT 1 FROM plans WHERE slug = 'free') THEN
        PERFORM setval('plans_id_seq', 1, true);
    END IF;
END $$;

INSERT INTO plans (slug, display_name, quota_calls_per_month, rate_limit_rpm,
                   seat_limit, is_public, price_cents, currency, billing_interval,
                   pricing_model, min_seats, prices)
VALUES
  ('free',      'Free',                      200,    30,  1,  TRUE,  0,    'USD', 'free',    'flat',     NULL, '{"USD": 0}'::jsonb),
  ('pro',       'Pro',                      10000,  120,  5,  TRUE,  1900, 'USD', 'monthly', 'per_seat', 1,    '{"USD": 1900}'::jsonb),
  ('team',      'Team',                    100000,  300, 20,  TRUE,  3900, 'USD', 'monthly', 'per_seat', 3,    '{"USD": 3900}'::jsonb),
  ('unlimited', 'Unlimited (admin-granted)',    0,     0, 99, FALSE,  0,    'USD', 'free',    'flat',     NULL, '{"USD": 0}'::jsonb)
ON CONFLICT (slug) DO NOTHING;

-- Set api_keys.plan_id DEFAULT to id of 'free' plan (idempotent).
DO $$
DECLARE _free_id INTEGER;
BEGIN
    SELECT id INTO _free_id FROM plans WHERE slug = 'free';
    IF _free_id IS NOT NULL THEN
        EXECUTE format('ALTER TABLE api_keys ALTER COLUMN plan_id SET DEFAULT %s', _free_id);
    END IF;
END $$;

-- 6b. Profiles: 12 Odoo CE base profiles (v8-v19) from m13_004 / 0004 migration.
INSERT INTO profiles (name, odoo_version, description)
VALUES
    ('odoo_8',  '8.0',  'Odoo CE 8.0'),
    ('odoo_9',  '9.0',  'Odoo CE 9.0'),
    ('odoo_10', '10.0', 'Odoo CE 10.0'),
    ('odoo_11', '11.0', 'Odoo CE 11.0'),
    ('odoo_12', '12.0', 'Odoo CE 12.0'),
    ('odoo_13', '13.0', 'Odoo CE 13.0'),
    ('odoo_14', '14.0', 'Odoo CE 14.0'),
    ('odoo_15', '15.0', 'Odoo CE 15.0'),
    ('odoo_16', '16.0', 'Odoo CE 16.0'),
    ('odoo_17', '17.0', 'Odoo CE 17.0'),
    ('odoo_18', '18.0', 'Odoo CE 18.0'),
    ('odoo_19', '19.0', 'Odoo CE 19.0')
ON CONFLICT (name) DO NOTHING;

-- 6c. EE modules catalogue (m13_011 -- 16 entries).
INSERT INTO ee_modules (name, vt_equivalent) VALUES
    ('knowledge',             NULL),
    ('documents',             'viin_document'),
    ('helpdesk',              'viin_helpdesk'),
    ('marketing_automation',  NULL),
    ('quality',               'to_quality'),
    ('industry_fsm',          NULL),
    ('appointment',           'viin_appointment'),
    ('planning',              NULL),
    ('sign',                  'viin_sign'),
    ('social',                'viin_social'),
    ('voip',                  NULL),
    ('whatsapp',              NULL),
    ('mrp_plm',               'to_mrp_plm'),
    ('accountant',            'to_account_accountant'),
    ('web_studio',            NULL),
    ('web_enterprise',        NULL)
ON CONFLICT (name) DO NOTHING;

-- 6d. Tenants seeded by m13_019 (Viindoo + public sentinel).
INSERT INTO tenants (name) VALUES
    ('Viindoo Technology JSC'),
    ('public')
ON CONFLICT (name) DO NOTHING;

-- ===========================================================================
-- 7. osm_reader GRANTS (pg_roles-guarded, idempotent)
--
--   ops/rls_create_osm_reader.sql is the SSOT for role creation + full grant set.
--   These grants are the deploy-safety duplicates so migrate-only deploys work
--   correctly without running the ops file.
-- ===========================================================================

DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'osm_reader') THEN
        -- app_settings: SELECT + INSERT needed (bootstrap_settings_safe UPSERTs at startup)
        GRANT SELECT, INSERT ON TABLE    app_settings         TO osm_reader;
        GRANT USAGE, SELECT  ON SEQUENCE app_settings_id_seq  TO osm_reader;
        GRANT SELECT         ON TABLE    app_settings_history TO osm_reader;
        -- ee_modules: EE confusion guard reads
        GRANT SELECT ON TABLE ee_modules TO osm_reader;
        -- patterns: suggest_pattern / find_examples catalogue reads
        GRANT SELECT ON TABLE patterns   TO osm_reader;
        -- subscriptions + billing_webhook_events: account portal + admin viewer reads
        GRANT SELECT ON TABLE subscriptions          TO osm_reader;
        GRANT SELECT ON TABLE billing_webhook_events TO osm_reader;
        -- embeddings: vector search (RLS-enforced)
        GRANT SELECT ON TABLE embeddings TO osm_reader;
        -- webui_users: column-level only -- id + is_admin for verify_api_key_full LEFT JOIN.
        -- Revoke any pre-existing broad table grant first (prod was hot-fixed with full grant),
        -- then re-grant column-level to enforce least-privilege (ADR m13_020).
        -- password_hash, email, oauth_id stay unreadable by the read tier.
        REVOKE SELECT ON TABLE webui_users FROM osm_reader;
        GRANT SELECT (id, is_admin) ON TABLE webui_users TO osm_reader;
    END IF;
END $$;
