-- migrations/m13_021_embeddings_global_sentinel.sql
-- FUFU-2 (root fix, supersedes FU-2's ck_embeddings_null_profile_scope CHECK):
-- Replace the NULL-as-global overloading in embeddings.profile_name with an
-- explicit '__global__' sentinel and make the column NOT NULL.
--
-- WHY: m13_004's RLS policy makes EVERY profile_name IS NULL row globally visible
-- to all tenants. NULL was doing double duty: "absent/unknown" AND "intentionally
-- global". FU-2's CHECK only guarded the symptom (a future accidental NULL write).
-- The root fix removes the overloading: global rows carry a real value
-- '__global__', the column is NOT NULL, and the RLS policy matches the sentinel
-- explicitly. NULL can no longer mean anything.
--
-- ORDER MATTERS (single migration, executed serially — each step justified):
--   1. Backfill NULL -> '__global__' FIRST. Must precede the RLS swap (else a
--      window exists where the new policy looks for '__global__' but rows are
--      still NULL -> suggest_pattern returns 0) and precede SET NOT NULL (else
--      the constraint add fails on the 121 NULL rows).
--   2. SET NOT NULL: metadata + brief AccessExclusive validate-scan; safe now
--      that 0 NULLs remain. Closes the NULL write path permanently (stronger
--      than the dropped CHECK).
--   3. Drop FU-2's now-vacuous CHECK if a dev box applied the old m13_021
--      (it was not in prod, but idempotent guard).
--   4. Narrowed sentinel CHECK: only the pattern catalogue may be '__global__'.
--   5. DROP+CREATE the RLS policy: replace "OR profile_name IS NULL" with
--      "OR profile_name = '__global__'". CREATE POLICY has no IF NOT EXISTS in
--      PG16, so DROP IF EXISTS + CREATE.
--   6. profiles.name dunder-block CHECK: reserve '__*__' for system sentinels so
--      an admin cannot create a profile literally named '__global__' (which would
--      make its embeddings globally visible — same leak class as NULL).
--
-- osm_reader grant note: DROP/CREATE POLICY does NOT change table privileges.
-- osm_reader's SELECT grant on embeddings is unaffected; no re-GRANT needed. RLS
-- policies are evaluated independent of GRANTs. FORCE RLS stays in effect.
--
-- Idempotent: backfill is a no-op on re-run (0 NULLs); DROP/CREATE POLICY is
-- self-healing; SET NOT NULL is a no-op if already set; CHECK adds are guarded.
-- Dual to_regclass guards skip cleanly on a no-pgvector DB.
--
-- Rollback (manual): re-add IS NULL branch to policy, DROP NOT NULL,
-- UPDATE '__global__' -> NULL, drop the two new CHECKs.

-- === Block 1: backfill + NOT NULL + sentinel CHECK on embeddings ===========
DO $$
BEGIN
  IF to_regclass('public.embeddings') IS NULL THEN
    RETURN;  -- no pgvector on this DB
  END IF;

  -- Pre-flight guard: fail with a clear message if non-pattern NULL rows exist.
  -- Prod pre-flight verified 0 such rows; this guard makes the migration self-protecting
  -- for other environments. Pattern catalogue NULLs (chunk_type='pattern_example' AND
  -- module='__patterns__') are the only expected NULLs and are handled by Step 1 below.
  IF EXISTS (
    SELECT 1 FROM embeddings
     WHERE profile_name IS NULL
       AND NOT (chunk_type = 'pattern_example' AND module = '__patterns__')
  ) THEN
    RAISE EXCEPTION 'm13_021: found non-pattern rows with NULL profile_name — resolve '
      '(delete or assign a profile) before migrating; they would violate '
      'ck_embeddings_global_sentinel_scope after backfill';
  END IF;

  -- Step 1: backfill (runs as table owner -> RLS owner-bypass, no GUC needed).
  UPDATE embeddings
     SET profile_name = '__global__'
   WHERE profile_name IS NULL;

  -- Step 2: column NOT NULL (0 NULLs remain after Step 1).
  ALTER TABLE embeddings ALTER COLUMN profile_name SET NOT NULL;

  -- Step 3: drop FU-2's superseded CHECK if a dev box applied the old m13_021.
  ALTER TABLE embeddings
    DROP CONSTRAINT IF EXISTS ck_embeddings_null_profile_scope;

  -- Step 4: narrowed sentinel CHECK — only the pattern catalogue may be global.
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

-- VALIDATE the sentinel CHECK under SHARE UPDATE EXCLUSIVE (own DO block so the
-- no-pgvector path skips). Safe to re-run.
DO $$
BEGIN
  IF to_regclass('public.embeddings') IS NULL THEN
    RETURN;
  END IF;
  ALTER TABLE embeddings VALIDATE CONSTRAINT ck_embeddings_global_sentinel_scope;
END $$;

-- === Block 2: swap the RLS policy (IS NULL branch -> sentinel) ==============
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

-- === Block 3: reserve dunder-prefixed profile names (collision guard) =======
-- profiles always exists (created by 0001_initial); no to_regclass guard needed,
-- but keep one for symmetry / partial-schema dev DBs.
DO $$
BEGIN
  IF to_regclass('public.profiles') IS NULL THEN
    RETURN;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conname = 'profiles_name_no_dunder'
       AND conrelid = 'public.profiles'::regclass
  ) THEN
    -- Block names beginning with '__' (two literal underscores).
    -- Uses a regular string with single-backslash ESCAPE (one char, PG16-safe).
    -- '\_' escapes the LIKE wildcard '_' to a literal underscore; '%' matches the rest.
    -- FOLLOW-UP: empty-string profile_name ('' visibility via string_to_array) is
    --   pre-existing and not introduced here; a length>0 CHECK risks VALIDATE abort
    --   on existing rows — do NOT add it in this migration.
    ALTER TABLE profiles
      ADD CONSTRAINT profiles_name_no_dunder
      CHECK (name NOT LIKE '\_\_%' ESCAPE '\') NOT VALID;
  END IF;
END $$;

DO $$
BEGIN
  IF to_regclass('public.profiles') IS NULL THEN
    RETURN;
  END IF;
  ALTER TABLE profiles VALIDATE CONSTRAINT profiles_name_no_dunder;
END $$;
