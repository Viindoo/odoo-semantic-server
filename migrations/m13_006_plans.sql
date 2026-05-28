-- migrations/m13_006_plans.sql
-- M10B P0 "Commercialization" — control-plane DDL (ADR-0039).
--
-- Adds:
--   1. plans               — billing plan catalog
--   2. api_keys.plan_id    — FK, NOT NULL after backfill
--   3. tenants extensions  — owner_user_id, billing_email, seat_limit_override
--   4. usage_counter       — monthly call counter (fast quota check)
--   5. Seed 4 plans        — free-grandfathered, free, pro, team
--   6. Backfill existing api_keys → free-grandfathered
--
-- Idempotent — safe to re-run.

-- 1. plans
CREATE TABLE IF NOT EXISTS plans (
    id                     SERIAL      PRIMARY KEY,
    slug                   TEXT        NOT NULL UNIQUE,
    display_name           TEXT        NOT NULL,
    quota_calls_per_month  INTEGER     NOT NULL,         -- 0 = unlimited (admin-only)
    rate_limit_rpm         INTEGER     NOT NULL,
    seat_limit             INTEGER     NOT NULL DEFAULT 1,
    is_public              BOOLEAN     NOT NULL DEFAULT FALSE,
    metadata               JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2. api_keys.plan_id (nullable lúc add, sẽ backfill rồi SET NOT NULL)
ALTER TABLE api_keys
    ADD COLUMN IF NOT EXISTS plan_id INTEGER REFERENCES plans(id);

-- 3. tenants extensions
-- owner_user_id: FK references webui_users(id) added defensively via DO block below
-- (webui_users created by 9000_webui_users.sql which may run after this migration
--  in some environments; the DO block makes the FK conditional on table existence).
ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS owner_user_id INTEGER,
    ADD COLUMN IF NOT EXISTS billing_email TEXT,
    ADD COLUMN IF NOT EXISTS seat_limit_override INTEGER;

-- Add the FK constraint only if webui_users table exists (defensive, idempotent)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'webui_users')
       AND NOT EXISTS (
           SELECT 1 FROM information_schema.table_constraints
           WHERE table_name = 'tenants'
             AND constraint_name = 'tenants_owner_user_id_fkey'
       )
    THEN
        ALTER TABLE tenants
            ADD CONSTRAINT tenants_owner_user_id_fkey
            FOREIGN KEY (owner_user_id) REFERENCES webui_users(id);
    END IF;
END$$;

-- 4. usage_counter — atomic INSERT ... ON CONFLICT DO UPDATE friendly
CREATE TABLE IF NOT EXISTS usage_counter (
    api_key_id     INTEGER     NOT NULL REFERENCES api_keys(id),
    period_yyyymm  TEXT        NOT NULL,
    call_count     INTEGER     NOT NULL DEFAULT 0,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (api_key_id, period_yyyymm)
);
CREATE INDEX IF NOT EXISTS usage_counter_period_idx ON usage_counter (period_yyyymm);

-- 5. Seed 4 plans (idempotent INSERT ... ON CONFLICT DO NOTHING)
INSERT INTO plans (slug, display_name, quota_calls_per_month, rate_limit_rpm, seat_limit, is_public)
VALUES
  ('free-grandfathered', 'Free (Grandfathered)', 1000,   60,  1, FALSE),
  ('free',               'Free',                 100,    30,  1, TRUE),
  ('pro',                'Pro',                  10000,  120, 5, TRUE),
  ('team',               'Team',                 100000, 300, 20, TRUE)
ON CONFLICT (slug) DO NOTHING;

-- 6. Backfill existing api_keys with NULL plan_id → free-grandfathered
UPDATE api_keys
   SET plan_id = (SELECT id FROM plans WHERE slug = 'free-grandfathered')
 WHERE plan_id IS NULL;

-- 7. Make plan_id NOT NULL after backfill (guard rail for new keys)
ALTER TABLE api_keys
    ALTER COLUMN plan_id SET NOT NULL;
