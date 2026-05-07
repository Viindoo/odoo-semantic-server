-- Bootstrap pgvector extension (requires superuser).
-- Mounted into postgres container via docker-compose initdb volume.
-- Must run before run_migrations() which creates the embeddings table.
CREATE EXTENSION IF NOT EXISTS vector;
