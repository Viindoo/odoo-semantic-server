-- migrations/0003_profile_hierarchy.sql
-- Adds parent_profile_id self-FK to the profiles table for delta-repo hierarchy.
--
-- Option Y (ADR-0014): the application layer resolves the full ancestor chain
-- (get_ancestor_profile_names) and writes a `profile` list property on every
-- Neo4j node during indexing. MCP tools gain an optional `profile_name` filter.
--
-- ON DELETE RESTRICT is intentional: removing a parent profile while children
-- exist must be an explicit admin decision (delete / re-parent children first).
-- Application code validates cycle-free + version-match before updating the FK.

ALTER TABLE profiles
    ADD COLUMN IF NOT EXISTS parent_profile_id INTEGER
    REFERENCES profiles(id) ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS idx_profiles_parent ON profiles(parent_profile_id);

COMMENT ON COLUMN profiles.parent_profile_id IS
    'Self-FK for delta-repo hierarchy. ON DELETE RESTRICT. Application enforces cycle-free + version-match.';
