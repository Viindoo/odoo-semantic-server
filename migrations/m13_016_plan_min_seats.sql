-- migrations/m13_016_plan_min_seats.sql
-- WI-1 follow-up "per-plan min_seats" — adds min_seats display SSOT to plans table.
--
-- Background:
--   m13_015 added pricing_model (flat | per_seat).  The pricing page previously
--   inferred "which per_seat plan has a seat minimum" by comparing seat_limit > 5,
--   a brittle heuristic.  This column provides an explicit, per-plan SSOT for the
--   DISPLAY copy ("min. N seats — from $X/mo").
--
-- Relationship with billing.team_min_seats setting:
--   plans.min_seats   = display SSOT consumed by the pricing page (this column).
--   billing.team_min_seats = enforcement SSOT used at checkout in activation.py
--                            (ADR-0042; NOT changed here, out of scope).
--   The two values are kept in sync manually: seed sets min_seats=3 for team to
--   match the catalogue default of billing.team_min_seats=3.  If you change one,
--   change the other too (or the pricing page will show different copy than what
--   is enforced at checkout).
--
-- Adds:
--   1. plans.min_seats  — nullable integer; NULL = no per-seat minimum (Free, flat plans).
--   2. Seed: team → 3, pro → 1, free → NULL (no minimum).
--
-- Idempotent — safe to re-run.

-- ===== 1. Add min_seats column =====
ALTER TABLE plans
    ADD COLUMN IF NOT EXISTS min_seats INTEGER;

-- Add CHECK constraint if not already present (idempotent guard).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'plans_min_seats_check'
           AND conrelid = 'plans'::regclass
    ) THEN
        ALTER TABLE plans ADD CONSTRAINT plans_min_seats_check
            CHECK (min_seats IS NULL OR min_seats >= 1);
    END IF;
END $$;

-- ===== 2. Seed: set per-plan minimums =====
-- team: min 3 seats (matches billing.team_min_seats catalogue default = 3).
-- pro:  min 1 seat (effectively "billed per seat", no enforced minimum).
-- free: left NULL intentionally (flat plan, no seat minimum concept).
-- Guard: only update when NULL so an admin override is not reverted.
UPDATE plans SET min_seats = 3
 WHERE slug = 'team'
   AND min_seats IS NULL;

UPDATE plans SET min_seats = 1
 WHERE slug = 'pro'
   AND min_seats IS NULL;
