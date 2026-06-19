-- migrations/0002_add_category_to_patterns.sql
-- Add category column to patterns table (ADR-0042, #331).
--
-- Adds a nullable TEXT column with a CHECK constraint limiting values to
-- 'test' (test-writing patterns) or 'production' (production coding patterns).
-- NULL means uncategorised (backward-compatible with existing rows).
--
-- IDEMPOTENCY:
--   ADD COLUMN IF NOT EXISTS and CREATE INDEX IF NOT EXISTS are safe to re-run.

ALTER TABLE patterns
    ADD COLUMN IF NOT EXISTS category TEXT
        CHECK (category IN ('test', 'production'));

CREATE INDEX IF NOT EXISTS idx_patterns_category ON patterns (category);
