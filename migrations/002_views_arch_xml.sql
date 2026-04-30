-- Store serialized primary arch bytes alongside the `views` row so
-- `resolve_view` can run the DOM resolver without reading source files from
-- disk (MCP servers run detached from the indexed addon tree).
--
-- `arch_xml` is the raw UTF-8 serialization of the root child of
-- <field name="arch">. Extension views have no standalone arch (their payload
-- lives in view_patches.content) — we store '' for them so the column stays
-- NOT NULL without a nullable split. Idempotent: safe to re-run.

ALTER TABLE views
    ADD COLUMN IF NOT EXISTS arch_xml bytea NOT NULL DEFAULT ''::bytea;
