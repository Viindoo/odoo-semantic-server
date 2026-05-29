-- migrations/m13_012_patterns.sql
-- Admin Settings: Pattern Catalogue (115 curated patterns) migrate JSON -> DB.
-- Backfill via ops/backfill_patterns.py (separate Python script, not inline SQL).
-- Static JSON kept as bootstrap fallback in src/data/patterns.json (WI-8 will
-- update seed_patterns.py to read from DB instead of file when DB rows exist).
--
-- JSON shape (patterns.schema.json):
--   Required: pattern_id, intent_keywords, file_ref, snippet_text, gotchas,
--             odoo_version_min, language
--   Optional: core_symbol_names, odoo_version_max
--   language enum: python / xml / js
--
-- ADR-0007 sentinel SHA: existing _SeedMeta Neo4j node mechanism is preserved
-- (seed_patterns.py tracks sha256 of patterns.json). WI-8 will update sentinel
-- write logic to recompute from DB rows instead of file content. No new
-- sentinel table needed in this migration.
--
-- ADR-0009 minimum: test_backfill_patterns.py enforces >= 80 entries.
-- ADR-0041: Admin Settings architecture.
--
-- Idempotent -- safe to re-run.

CREATE TABLE IF NOT EXISTS patterns (
    -- Primary identifier: kebab-case unique string (e.g. "computed-field-cross-model")
    pattern_id          TEXT        PRIMARY KEY,

    -- Semantic search / intent matching keywords (array for GIN index)
    intent_keywords     TEXT[]      NOT NULL DEFAULT '{}',

    -- Source file reference "addons/sale/models/sale_order.py:120"
    file_ref            TEXT        NOT NULL,

    -- Code snippet illustrating the pattern
    snippet_text        TEXT        NOT NULL,

    -- At least 3 common mistakes or caveats (ADR-0009 rule 5)
    gotchas             JSONB       NOT NULL DEFAULT '[]'::jsonb,

    -- Version range (odoo_version_min required; odoo_version_max optional)
    odoo_version_min    TEXT        NOT NULL,
    odoo_version_max    TEXT,                            -- NULL = no upper bound

    -- Programming language: python / xml / js  (matches schema enum)
    language            TEXT        NOT NULL
                                    CHECK (language IN ('python', 'xml', 'js')),

    -- Optional list of Odoo core symbol FQNs (e.g. ['odoo.api.depends'])
    core_symbol_names   TEXT[]      NOT NULL DEFAULT '{}',

    -- Audit / soft-delete
    metadata            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by          INTEGER     REFERENCES webui_users(id) ON DELETE SET NULL,
    soft_deleted        BOOLEAN     NOT NULL DEFAULT FALSE
);

-- GIN index on intent_keywords for fast keyword-overlap queries
CREATE INDEX IF NOT EXISTS idx_patterns_intent_keywords_gin
    ON patterns USING GIN(intent_keywords);

-- Btree index on language for filtering by python/xml/js
CREATE INDEX IF NOT EXISTS idx_patterns_language
    ON patterns(language);

-- Btree index on odoo_version_min for version-range filtering
CREATE INDEX IF NOT EXISTS idx_patterns_version_min
    ON patterns(odoo_version_min);
