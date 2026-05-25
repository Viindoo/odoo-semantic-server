-- m13_004_embeddings_rls.sql
-- WI-A (ADR-0034 D6/A2 + ADR-0034 WI-7): Enable RLS on the embeddings table
-- in "armed-but-dormant" mode.
--
-- DESIGN — ENABLE without FORCE (owner bypass = safe no-op):
--   The app role (odoo_semantic) is the table owner.  PostgreSQL skips RLS
--   policies for owner connections UNLESS FORCE ROW LEVEL SECURITY is also set.
--   By using ENABLE ROW LEVEL SECURITY *without* FORCE, all existing code paths
--   continue to operate exactly as before — the policy is installed but never
--   evaluated against the owner.  This migration is therefore safe to deploy
--   and rollback without any application changes.
--
-- ENFORCEMENT deferred to ops runbook (not this migration):
--   When the operator is ready to enforce isolation (after a non-owner read role
--   is created and the app is switched to it), they run:
--       ALTER TABLE embeddings FORCE ROW LEVEL SECURITY;
--   No code change is required at that point — the GUC wiring (app.allowed_profiles
--   set via _rls_read_tx) is already in place in the application layer.
--
-- POLICY semantics (app.allowed_profiles GUC):
--   '*'        — admin sentinel; policy returns TRUE → unrestricted access.
--   ''         — tenant with no profiles (empty string_to_array = {}) → deny-all
--                (profile_name = ANY('{}') = FALSE; profile_name IS NULL = FALSE
--                 for tenant rows; only global shared rows with NULL profile pass
--                 when the tenant owns no profiles — which is handled by the DB
--                 returning them through the IS NULL branch for shared catalogue).
--   'a,b,...'  — comma-separated profile_name list; rows pass when profile_name
--                matches any member or profile_name IS NULL (global shared rows,
--                ADR-0034 D3 — pattern catalogue, legacy unscoped rows).
--
-- The GUC is set transaction-locally by _rls_read_tx() in src/mcp/server.py.
-- If the GUC is NOT set (e.g. a direct psql session by an admin), the policy
-- falls back to current_setting(..., true) returning NULL; string_to_array(NULL,...)
-- = NULL; ANY(NULL) = NULL (not TRUE) — so only NULL profile_name rows (shared)
-- pass, giving an unprivileged default rather than a silent fail-open.  This is
-- intentional and documented in the runbook.
--
-- Idempotent: safe to re-run.  ENABLE RLS is a no-op if already enabled.
-- DROP POLICY IF EXISTS + CREATE handles re-runs without error.

DO $$
BEGIN
  -- Guard: skip everything when embeddings table does not exist (no pgvector).
  IF to_regclass('public.embeddings') IS NULL THEN
    RETURN;
  END IF;

  -- 1. Enable RLS — WITHOUT FORCE so owner (app role) bypasses → no-op on
  --    current code paths.  Safe to run when already enabled.
  ALTER TABLE embeddings ENABLE ROW LEVEL SECURITY;

  -- 2. Install tenant isolation policy.
  --    DROP + CREATE pattern used because CREATE POLICY does not support
  --    IF NOT EXISTS in PostgreSQL 16.
  DROP POLICY IF EXISTS embeddings_tenant ON embeddings;

  CREATE POLICY embeddings_tenant ON embeddings
  USING (
      -- Admin sentinel: app sets GUC to '*' to bypass per-tenant scoping.
      current_setting('app.allowed_profiles', true) = '*'
      -- Shared / global catalogue rows (pattern chunks, legacy unscoped rows,
      -- ADR-0034 D3): visible to ALL tenants unconditionally.
      OR profile_name IS NULL
      -- Normal tenant read: GUC carries comma-separated allowed profile_names.
      OR profile_name = ANY (
           string_to_array(current_setting('app.allowed_profiles', true), ',')
      )
  );

END $$;
