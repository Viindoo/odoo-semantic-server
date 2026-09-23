-- migrations/0003_module_presence.sql
-- Module lifecycle ledger (ADR-0056, OSM #378).
--
-- module_presence is the SSOT of each module's lifecycle state per
-- (repo, technical name). One row per (repo_id, name); the registry's
-- per-name winner is the row, same-name losers inside the same repo (the
-- posbox point_of_sale stub beside the real module in odoo12-18) are kept
-- in shadowed_paths, never as rows.
--
-- States:
--   present   manifest tracked at HEAD, parsed, installable, not license-skipped
--   excluded  manifest tracked at HEAD but not indexed (exclusion_reason)
--   retired   no tracked manifest for the name any more (retire_reason); the
--             row is kept forever as history
-- retire_pending marks a present/excluded row whose retirement is decided but
-- not yet executed (gate trip, --no-retire, crash): the row flips to retired
-- only after the graph and embedding delete succeeded (two-phase ledger).
--
-- History survives repo/profile deletion: repo_id is ON DELETE SET NULL and
-- the repo identity (url, basename, branch) and the owning profile_name are
-- denormalized on the row. profile_name is kept in sync on profile rename by
-- ModulePresenceStore.rename_profile.
--
-- RLS: same shape as embeddings_tenant (app.allowed_profiles GUC, '*' admin
-- sentinel), ENABLE not FORCE. Ledger rows are never global, so the policy has
-- no '__global__' branch.
--
-- IDEMPOTENCY:
--   Every statement uses IF NOT EXISTS or is wrapped in a DO block with an
--   existence check. The FK is added in its own guarded block so that a table
--   which survived a DROP TABLE repos CASCADE gets its FK back on re-run.
--   DROP TABLE ... CASCADE only drops the FK constraint object -- it does not
--   fire the constraint's ON DELETE SET NULL action row-by-row -- so a repo
--   hard-deleted this way (or a partial restore that recreated repos without
--   the old rows) can leave module_presence.repo_id pointing at ids that no
--   longer exist in repos. The guarded ADD CONSTRAINT below would then abort
--   the whole migration with ForeignKeyViolation. The UPDATE right before it
--   nulls exactly those dangling ids first, replaying what ON DELETE SET NULL
--   would have done had it fired; history is unaffected because repo identity
--   (repo_url/repo_basename/repo_branch) and profile_name are denormalized on
--   the row (see "History survives ..." above), so nulling repo_id loses
--   nothing.

CREATE TABLE IF NOT EXISTS module_presence (
    id                      BIGSERIAL   PRIMARY KEY,
    repo_id                 INTEGER,
    repo_url                TEXT        NOT NULL,
    repo_basename           TEXT        NOT NULL,
    repo_branch             TEXT        NOT NULL,
    profile_name            TEXT        NOT NULL,
    odoo_version            TEXT        NOT NULL,
    name                    TEXT        NOT NULL,
    path                    TEXT        NOT NULL,
    manifest_file           TEXT        NOT NULL,
    shadowed_paths          TEXT[]      NOT NULL DEFAULT '{}',
    state                   TEXT        NOT NULL,
    exclusion_reason        TEXT,
    retire_reason           TEXT,
    retire_pending          BOOLEAN     NOT NULL DEFAULT FALSE,
    retire_pending_reason   TEXT,
    retire_pending_at       TIMESTAMPTZ,
    retire_blocked_by       TEXT,
    needs_rewrite           BOOLEAN     NOT NULL DEFAULT FALSE,
    version_raw             TEXT,
    version_mismatch        BOOLEAN     NOT NULL DEFAULT FALSE,
    first_seen_sha          TEXT        NOT NULL,
    first_seen_at           TIMESTAMPTZ NOT NULL,
    last_seen_sha           TEXT        NOT NULL,
    last_seen_at            TIMESTAMPTZ NOT NULL,
    state_changed_sha       TEXT,
    state_changed_at        TIMESTAMPTZ,
    removing_commit_sha     TEXT,
    removing_commit_date    TIMESTAMPTZ,
    removing_commit_subject TEXT,
    successor_names         TEXT[],
    successor_source        TEXT,
    resurrection_count      INTEGER     NOT NULL DEFAULT 0,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT module_presence_repo_name_key UNIQUE (repo_id, name),
    CONSTRAINT ck_module_presence_state
        CHECK (state IN ('present', 'excluded', 'retired')),
    CONSTRAINT ck_module_presence_exclusion_reason
        CHECK (exclusion_reason IN ('installable_false', 'license_skip', 'unparseable')),
    CONSTRAINT ck_module_presence_excluded_has_reason
        CHECK ((state = 'excluded') = (exclusion_reason IS NOT NULL)),
    CONSTRAINT ck_module_presence_retire_reason
        CHECK (retire_reason IN ('absent', 'repo_removed', 'orphan_sweep')),
    CONSTRAINT ck_module_presence_retired_has_reason
        CHECK ((state = 'retired') = (retire_reason IS NOT NULL)),
    CONSTRAINT ck_module_presence_retire_pending_reason
        CHECK (retire_pending_reason IN ('absent', 'repo_removed', 'orphan_sweep')),
    CONSTRAINT ck_module_presence_pending_not_retired
        CHECK (NOT (retire_pending AND state = 'retired')),
    CONSTRAINT ck_module_presence_pending_has_reason
        CHECK (retire_pending = (retire_pending_reason IS NOT NULL)),
    CONSTRAINT ck_module_presence_successor_source
        CHECK (successor_source IN ('git_rename', 'old_technical_name')),
    CONSTRAINT ck_module_presence_successor_pair
        CHECK ((successor_names IS NULL) = (successor_source IS NULL)),
    CONSTRAINT ck_module_presence_resurrection_count
        CHECK (resurrection_count >= 0)
);

UPDATE module_presence mp
   SET repo_id = NULL
 WHERE mp.repo_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM repos r WHERE r.id = mp.repo_id);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conname = 'module_presence_repo_id_fkey'
       AND conrelid = 'public.module_presence'::regclass
  ) THEN
    ALTER TABLE module_presence
      ADD CONSTRAINT module_presence_repo_id_fkey
      FOREIGN KEY (repo_id) REFERENCES repos(id) ON DELETE SET NULL;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_module_presence_version_name
    ON module_presence (odoo_version, name, state);
CREATE INDEX IF NOT EXISTS ix_module_presence_profile
    ON module_presence (profile_name);
CREATE INDEX IF NOT EXISTS ix_module_presence_repo_state
    ON module_presence (repo_id, state);
CREATE INDEX IF NOT EXISTS ix_module_presence_pending
    ON module_presence (odoo_version)
    WHERE retire_pending;
CREATE INDEX IF NOT EXISTS ix_module_presence_needs_rewrite
    ON module_presence (repo_id)
    WHERE needs_rewrite;
CREATE INDEX IF NOT EXISTS ix_module_presence_successors
    ON module_presence USING gin (successor_names);

ALTER TABLE repos ADD COLUMN IF NOT EXISTS presence_head_sha      TEXT;
ALTER TABLE repos ADD COLUMN IF NOT EXISTS lifecycle_attention    TEXT;
ALTER TABLE repos ADD COLUMN IF NOT EXISTS lifecycle_attention_at TIMESTAMPTZ;

ALTER TABLE module_presence ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  DROP POLICY IF EXISTS module_presence_tenant ON module_presence;
  CREATE POLICY module_presence_tenant ON module_presence
  USING (
      current_setting('app.allowed_profiles', true) = '*'
      OR profile_name = ANY (
           string_to_array(current_setting('app.allowed_profiles', true), ',')
      )
  );
END $$;

-- Deploy-safety duplicate of the ops/rls_create_osm_reader.sql grant (house
-- convention, see 0001_initial.sql GRANT NOTE). The MCP read tier only reads.
DO $$
BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'osm_reader') THEN
    GRANT SELECT ON TABLE module_presence TO osm_reader;
  END IF;
END $$;
