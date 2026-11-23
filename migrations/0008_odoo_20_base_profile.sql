-- migrations/0008_odoo_20_base_profile.sql
-- The shared Odoo CE base profile for 20.0, next to the 8.0-19.0 ones that
-- 0001_initial.sql seeds (section 6b).
--
-- Every supported Odoo series has one shared base profile (tenant_id NULL)
-- named odoo_<major>; admins attach the Odoo CE repo of that series to it and
-- child profiles (customer or Viindoo stacks) declare it as their parent.
-- 0001 seeds 8.0-19.0 only, so a fresh install had no 20.0 base profile.
--
-- IDEMPOTENT: ON CONFLICT (name) DO NOTHING. A deployment that already created
-- odoo_20 from the admin UI keeps its row untouched (repos, children, tenant).
--
-- No rollback file: the row may predate this migration (created by hand), so
-- an automatic DELETE could remove a profile in use. To undo on a fresh
-- install, delete the odoo_20 row by hand only while it has no repos and no
-- child profiles.
--
-- Grants / RLS: data only; osm_reader keeps SELECT on profiles
-- (ops/rls_create_osm_reader.sql).

INSERT INTO profiles (name, odoo_version, description)
VALUES ('odoo_20', '20.0', 'Odoo CE 20.0')
ON CONFLICT (name) DO NOTHING;
