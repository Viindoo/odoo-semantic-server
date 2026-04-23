-- WP-2 initial schema. Idempotent: safe to re-run.
-- Runs under `SET LOCAL search_path TO "<schema>", public` from scripts/migrate.py,
-- so all DDL is schema-neutral. Every table carries a `tenant` column defaulted
-- to current_schema(), enabling cross-schema UNION queries to tag row origin
-- without hard-coded literals. Cross-schema refs are stored as bare bigint with
-- NO REFERENCES (see architecture/graph-store.md "no cross-schema hard foreign keys").

CREATE TABLE IF NOT EXISTS modules (
    id bigserial PRIMARY KEY,
    tenant text NOT NULL DEFAULT current_schema(),
    name text NOT NULL,
    manifest_path text NOT NULL,
    version text,
    depends text[] NOT NULL DEFAULT '{}'::text[],
    auto_install boolean NOT NULL DEFAULT false,
    installable boolean NOT NULL DEFAULT true,
    source_repo text,
    load_order integer,
    content_hash text NOT NULL,
    indexed_at_sha text NOT NULL,
    UNIQUE (tenant, source_repo, name)
);

CREATE TABLE IF NOT EXISTS models (
    id bigserial PRIMARY KEY,
    tenant text NOT NULL DEFAULT current_schema(),
    name text NOT NULL,
    module_id bigint NOT NULL REFERENCES modules (id) ON DELETE CASCADE,
    is_primary_declaration boolean NOT NULL DEFAULT false,
    inherits_from text[] NOT NULL DEFAULT '{}'::text[],
    delegates_to jsonb NOT NULL DEFAULT '{}'::jsonb,
    "table" text,
    rec_name text,
    "order" text,
    abstract boolean NOT NULL DEFAULT false,
    transient boolean NOT NULL DEFAULT false,
    file_path text NOT NULL,
    start_line integer NOT NULL,
    end_line integer NOT NULL,
    content_hash text NOT NULL,
    indexed_at_sha text NOT NULL,
    indexer_notes jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (module_id, name),
    CHECK (NOT (abstract AND transient))
);

CREATE INDEX IF NOT EXISTS models_module_id_name_idx ON models (module_id, name);
CREATE INDEX IF NOT EXISTS models_name_idx ON models (name);

-- fields.override_of: soft ref. Within a single schema it points at a local
-- fields.id; across schemas (tenant field overriding a public field) it points
-- at a public.fields.id. REFERENCES omitted to keep the edge soft (see
-- architecture/graph-store.md). override_of column is authoritative even though
-- data-model/fields.md omits the tenancy caveat -- plan §2 WP-2 explicitly
-- requires it and an index on it.
CREATE TABLE IF NOT EXISTS fields (
    id bigserial PRIMARY KEY,
    tenant text NOT NULL DEFAULT current_schema(),
    model_id bigint NOT NULL REFERENCES models (id) ON DELETE CASCADE,
    field_name text NOT NULL,
    field_type text NOT NULL,
    related_model text,
    related_field text,
    compute text,
    inverse text,
    search text,
    store boolean,
    required boolean,
    readonly boolean,
    "default" text,
    related_path text,
    depends text[],
    override_of bigint,
    file_path text NOT NULL,
    start_line integer NOT NULL,
    end_line integer NOT NULL,
    content_hash text NOT NULL,
    indexed_at_sha text NOT NULL,
    UNIQUE (model_id, field_name)
);

CREATE INDEX IF NOT EXISTS fields_model_id_field_name_idx ON fields (model_id, field_name);
CREATE INDEX IF NOT EXISTS fields_override_of_idx ON fields (override_of);

CREATE TABLE IF NOT EXISTS methods (
    id bigserial PRIMARY KEY,
    tenant text NOT NULL DEFAULT current_schema(),
    model_id bigint NOT NULL REFERENCES models (id) ON DELETE CASCADE,
    method_name text NOT NULL,
    signature text NOT NULL,
    decorators text[] NOT NULL DEFAULT '{}'::text[],
    calls_super boolean NOT NULL DEFAULT false,
    override_of bigint,
    file_path text NOT NULL,
    start_line integer NOT NULL,
    end_line integer NOT NULL,
    content_hash text NOT NULL,
    indexed_at_sha text NOT NULL,
    UNIQUE (model_id, method_name)
);

CREATE INDEX IF NOT EXISTS methods_model_id_method_name_idx ON methods (model_id, method_name);
CREATE INDEX IF NOT EXISTS methods_override_of_idx ON methods (override_of);
CREATE INDEX IF NOT EXISTS methods_decorators_gin ON methods USING GIN (decorators);

CREATE TABLE IF NOT EXISTS views (
    id bigserial PRIMARY KEY,
    tenant text NOT NULL DEFAULT current_schema(),
    xmlid text NOT NULL,
    module_id bigint NOT NULL REFERENCES modules (id) ON DELETE CASCADE,
    model text NOT NULL,
    view_type text NOT NULL,
    inherit_id bigint,
    priority integer NOT NULL DEFAULT 16,
    mode text NOT NULL,
    arch_hash text NOT NULL,
    file_path text NOT NULL,
    start_line integer NOT NULL,
    end_line integer NOT NULL,
    indexed_at_sha text NOT NULL,
    UNIQUE (module_id, xmlid),
    CHECK (mode IN ('primary', 'extension')),
    -- Strict biconditional kept for historical parity with the WP-2 shape;
    -- migration 003 relaxes this to accommodate the second-pass xmlid→id
    -- resolution in the WP-15 view driver (see migrations/003 for rationale).
    CHECK ((mode = 'extension') = (inherit_id IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS views_module_id_xmlid_idx ON views (module_id, xmlid);
CREATE INDEX IF NOT EXISTS views_inherit_id_idx ON views (inherit_id);
CREATE INDEX IF NOT EXISTS views_model_idx ON views (model);

CREATE TABLE IF NOT EXISTS view_patches (
    id bigserial PRIMARY KEY,
    tenant text NOT NULL DEFAULT current_schema(),
    view_id bigint NOT NULL REFERENCES views (id) ON DELETE CASCADE,
    ordinal integer NOT NULL,
    expr text NOT NULL,
    position text NOT NULL,
    content text NOT NULL,
    UNIQUE (view_id, ordinal),
    CHECK (position IN ('after', 'before', 'inside', 'replace', 'attributes'))
);

CREATE INDEX IF NOT EXISTS view_patches_view_id_idx ON view_patches (view_id);

CREATE TABLE IF NOT EXISTS cache_metadata (
    id bigserial PRIMARY KEY,
    tenant text NOT NULL DEFAULT current_schema(),
    file_path text NOT NULL,
    module_name text NOT NULL,
    content_hash text NOT NULL,
    git_sha text NOT NULL,
    file_kind text NOT NULL,
    byte_size integer NOT NULL,
    indexed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant, file_path)
);

CREATE INDEX IF NOT EXISTS cache_metadata_tenant_module_name_idx ON cache_metadata (tenant, module_name);
CREATE INDEX IF NOT EXISTS cache_metadata_content_hash_idx ON cache_metadata (content_hash);

-- Vector stub. `embedding vector(?)` column deliberately omitted per plan §6.9:
-- dimension is provider-specific (voyage-code-3=1024, bge-code-v1=1536) and is
-- locked per tenant in P3's first migration. Keeping the table column-less for
-- embeddings avoids a later ALTER to widen/narrow the vector dimension.
CREATE TABLE IF NOT EXISTS code_chunks (
    id bigserial PRIMARY KEY,
    tenant text NOT NULL DEFAULT current_schema(),
    chunk_type text NOT NULL,
    ref_id bigint NOT NULL,
    content_hash text NOT NULL,
    indexed_at_sha text NOT NULL
);

CREATE INDEX IF NOT EXISTS code_chunks_chunk_type_ref_id_idx ON code_chunks (chunk_type, ref_id);
