-- M9 W-FE: FERNET key rotation audit table.
--
-- Records every successful rotate-fernet run for non-repudiation.
-- old_key_id / new_key_id are SHA-256 fingerprints of the first 8 bytes of
-- each key — enough to identify which key generation was involved without
-- storing or revealing any key material.

CREATE TABLE IF NOT EXISTS key_rotation_log (
    id          BIGSERIAL    PRIMARY KEY,
    rotated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    actor       TEXT         NOT NULL,
    row_count   INTEGER      NOT NULL,
    old_key_id  TEXT         NOT NULL,
    new_key_id  TEXT         NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_key_rotation_log_time
    ON key_rotation_log (rotated_at DESC);
