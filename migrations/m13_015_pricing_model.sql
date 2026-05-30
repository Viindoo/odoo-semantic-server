-- migrations/m13_015_pricing_model.sql
-- WI-1 "Per-seat pricing data layer" — adds pricing_model to plans table.
--
-- Adds:
--   1. plans.pricing_model  — enum-checked column (flat | per_seat), default 'flat'
--   2. Seed: pro + team plans tagged per_seat
--
-- Idempotent — safe to re-run.

-- ===== 1. Add pricing_model column =====
ALTER TABLE plans
    ADD COLUMN IF NOT EXISTS pricing_model TEXT NOT NULL DEFAULT 'flat';

-- Add CHECK constraint if not already present (idempotent guard).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'plans_pricing_model_check'
           AND conrelid = 'plans'::regclass
    ) THEN
        ALTER TABLE plans ADD CONSTRAINT plans_pricing_model_check
            CHECK (pricing_model IN ('flat', 'per_seat'));
    END IF;
END $$;

-- ===== 2. Seed: tag per-seat plans =====
-- Guard: only update when still at 'flat' default so an admin override is not reverted.
UPDATE plans SET pricing_model = 'per_seat'
 WHERE slug IN ('pro', 'team')
   AND pricing_model = 'flat';
