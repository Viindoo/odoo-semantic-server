-- migrations/9000_webui_users.sql
-- Web UI admin users table (M7 W16 — session auth).
-- Number 9000: deliberately high so it integrates last relative to W15 yoyo migrations.
-- Use CREATE TABLE IF NOT EXISTS for idempotency.

CREATE TABLE IF NOT EXISTS webui_users (
    username     VARCHAR(64)  PRIMARY KEY,
    password_hash VARCHAR(255) NOT NULL,
    created_at   TIMESTAMP    DEFAULT NOW()
);
