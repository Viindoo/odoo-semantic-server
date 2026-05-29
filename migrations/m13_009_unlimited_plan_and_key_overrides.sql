-- migrations/m13_009_unlimited_plan_and_key_overrides.sql
-- M10B P0-ext: per-key quota/rpm overrides + unlimited admin-granted plan.
--
-- Adds:
--   1. api_keys.rate_limit_override  — nullable INT, NULL = use plan default
--   2. api_keys.quota_override       — nullable INT, NULL = use plan default
--   3. plans row: 'unlimited'        — sentinel for admin-granted access
--
-- Ref: ADR-0041 (W-9 will land the ADR text; ok to forward-reference here).
--
-- Idempotent: safe to re-run.

-- 1. Add override columns to api_keys (idempotent via information_schema guard)
--
-- NULL = use plan default. CHECK >=0; 0 = zero-allowed (NOT unlimited).
-- Unlimited ONLY via slug = 'unlimited' (ADR-0041 D5 SSOT).
--
-- ADD COLUMN IF NOT EXISTS is idempotent for the column itself, but the inline
-- CHECK constraint is NOT idempotent (Postgres would add a second constraint on
-- each re-run). We therefore use a DO block that checks information_schema before
-- adding, then attaches the CHECK constraint via a named ALTER TABLE ADD CONSTRAINT
-- (named constraints are visible in pg_constraint and easy to guard against).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'api_keys' AND column_name = 'rate_limit_override'
           AND table_schema = 'public'
    ) THEN
        ALTER TABLE api_keys ADD COLUMN rate_limit_override INT;
        ALTER TABLE api_keys ADD CONSTRAINT api_keys_rate_limit_override_nonneg
            CHECK (rate_limit_override IS NULL OR rate_limit_override >= 0);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'api_keys' AND column_name = 'quota_override'
           AND table_schema = 'public'
    ) THEN
        ALTER TABLE api_keys ADD COLUMN quota_override INT;
        ALTER TABLE api_keys ADD CONSTRAINT api_keys_quota_override_nonneg
            CHECK (quota_override IS NULL OR quota_override >= 0);
    END IF;
END
$$;

-- 2. Seed the 'unlimited' plan (idempotent INSERT ... ON CONFLICT DO NOTHING)
--
-- 0/0 sentinel = unlimited per ADR-0041 D5. RPM=0 currently triggers a
-- silent-block in middleware (m13_006 path); W-2 ships the bypass guard.
-- Do not assign this plan to any key until W-2 lands.
INSERT INTO plans (slug, display_name, quota_calls_per_month, rate_limit_rpm, seat_limit, is_public)
VALUES ('unlimited', 'Unlimited (admin-granted)', 0, 0, 99, FALSE)
ON CONFLICT (slug) DO NOTHING;
