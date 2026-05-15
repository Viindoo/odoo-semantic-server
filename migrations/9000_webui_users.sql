-- migrations/9000_webui_users.sql
-- Web UI admin users table (M7 W16 — session auth).
-- Number 9000: deliberately high so it integrates last relative to W15 yoyo migrations.
-- Use CREATE TABLE IF NOT EXISTS for idempotency.

CREATE TABLE IF NOT EXISTS webui_users (
    username     VARCHAR(64)  PRIMARY KEY,
    password_hash VARCHAR(255) NOT NULL,
    is_admin     BOOLEAN      DEFAULT FALSE,
    is_active    BOOLEAN      DEFAULT TRUE,
    created_at   TIMESTAMP    DEFAULT NOW()
);

-- Add columns if they don't exist (for backward compat with old DB)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'webui_users' AND column_name = 'is_admin'
    ) THEN
        ALTER TABLE webui_users ADD COLUMN is_admin BOOLEAN DEFAULT FALSE;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'webui_users' AND column_name = 'is_active'
    ) THEN
        ALTER TABLE webui_users ADD COLUMN is_active BOOLEAN DEFAULT TRUE;
    END IF;
END $$;
