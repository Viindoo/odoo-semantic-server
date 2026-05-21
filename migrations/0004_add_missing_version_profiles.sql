-- migrations/0004_add_missing_version_profiles.sql
-- Self-contained SQL rescue path for all 12 root profiles v8-v19.
-- (Viindoo addon profiles — standard_profile_* and internal_profile_* — require
--  a 2-pass INSERT + FK update and are handled exclusively by the Python seeder.)
--
-- Context: production DBs seeded before OBS-1 may only have a subset of root
-- profiles because the Python seeder (src/db/seed_master_data.py) was re-run
-- after each milestone that added a new version.  Running
-- ``python -m src.db.migrate`` post-OBS-1 automatically re-invokes
-- ``seed_all()`` which is idempotent, so this migration is belt-and-suspenders
-- for the profile rows only.
--
-- Repos rows are NOT inserted here because:
--   1. repos.local_path is NOT NULL and depends on runtime Path.home() (cannot
--      hardcode in portable SQL).
--   2. The Python seeder seed_repos() handles all repo rows with
--      ON CONFLICT DO NOTHING.
-- Admins who want explicit local-path overrides (e.g. manually cloned repos at
-- /home/<user>/git/odoo_<N>.0/) should register them via the web UI or
-- ``python -m src.manager register-repo``.
--
-- Idempotency: INSERT … ON CONFLICT (name) DO NOTHING — safe to re-run.
-- Parent FK (parent_profile_id) is set by the Python seeder's Pass 2 phase;
-- this SQL migration only owns the root profile rows.

INSERT INTO profiles (name, odoo_version, description)
VALUES
    ('odoo_8',  '8.0',  'Odoo CE 8.0'),
    ('odoo_9',  '9.0',  'Odoo CE 9.0'),
    ('odoo_10', '10.0', 'Odoo CE 10.0'),
    ('odoo_11', '11.0', 'Odoo CE 11.0'),
    ('odoo_12', '12.0', 'Odoo CE 12.0'),
    ('odoo_13', '13.0', 'Odoo CE 13.0'),
    ('odoo_14', '14.0', 'Odoo CE 14.0'),
    ('odoo_15', '15.0', 'Odoo CE 15.0'),
    ('odoo_16', '16.0', 'Odoo CE 16.0'),
    ('odoo_17', '17.0', 'Odoo CE 17.0'),
    ('odoo_18', '18.0', 'Odoo CE 18.0'),
    ('odoo_19', '19.0', 'Odoo CE 19.0')
ON CONFLICT (name) DO NOTHING;
