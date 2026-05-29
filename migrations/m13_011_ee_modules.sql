-- migrations/m13_011_ee_modules.sql
-- Admin Settings: EE Modules guard catalogue.
-- Migrate src/data/ee_modules.py static list → DB table for admin CRUD.
-- Static list giữ làm fallback in code (src/data/ee_modules.py _FALLBACK_EE_MODULES).
--
-- ADR-0041. Idempotent — safe to re-run.

CREATE TABLE IF NOT EXISTS ee_modules (
    id              SERIAL      PRIMARY KEY,
    name            TEXT        NOT NULL UNIQUE,
    since_version   TEXT,                                  -- "17.0" etc. nullable
    vt_equivalent   TEXT,                                  -- Viindoo module slug nếu có
    description     TEXT,
    deprecated      BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by      INTEGER     REFERENCES webui_users(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_ee_modules_name ON ee_modules(name);

-- Backfill from src/data/ee_modules.py EE_CONFUSION dict (16 entries, surveyed 2026-05-08).
-- vt_equivalent: Viindoo module slug (NULL = no equivalent).
-- since_version: NULL — not tracked in original static list.
INSERT INTO ee_modules (name, vt_equivalent) VALUES
    ('knowledge',             NULL),
    ('documents',             'viin_document'),
    ('helpdesk',              'viin_helpdesk'),
    ('marketing_automation',  NULL),
    ('quality',               'to_quality'),
    ('industry_fsm',          NULL),
    ('appointment',           'viin_appointment'),
    ('planning',              NULL),
    ('sign',                  'viin_sign'),
    ('social',                'viin_social'),
    ('voip',                  NULL),
    ('whatsapp',              NULL),
    ('mrp_plm',               'to_mrp_plm'),
    ('accountant',            'to_account_accountant'),
    ('web_studio',            NULL),
    ('web_enterprise',        NULL)
ON CONFLICT (name) DO NOTHING;
