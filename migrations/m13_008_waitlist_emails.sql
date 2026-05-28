-- migrations/m13_008_waitlist_emails.sql
-- Waitlist email capture for pricing-page signups (ADR-0039 P1 precursor).
--
-- Adds:
--   1. waitlist_emails  — stores email, plan tier interest, and source tag
--   2. Index on created_at for admin reporting queries
--
-- Idempotent: safe to re-run (CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS).

-- 1. waitlist_emails table
CREATE TABLE IF NOT EXISTS waitlist_emails (
    id         SERIAL      PRIMARY KEY,
    email      TEXT        NOT NULL UNIQUE,
    plan       TEXT,                           -- 'free' / 'pro' / 'team' / NULL if generic
    source     TEXT        NOT NULL DEFAULT 'pricing-page',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2. Index for admin "recent signups" queries (descending by created_at)
CREATE INDEX IF NOT EXISTS waitlist_emails_created_at_idx
    ON waitlist_emails (created_at DESC);
