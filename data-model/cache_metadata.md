---
status: draft
scope: data-model/cache_metadata
reads-with:
  - ../architecture/indexer.md
  - ../architecture/graph-store.md
---

# Table: `cache_metadata`

One row per indexed source file. Used by the indexer to skip unchanged files during delta re-indexing.

## Purpose

- Answer "has this file changed since last index" in O(1)
- Enable per-file content hash comparison without re-parsing
- Track git SHA per file for time-based queries

## Schema (draft)

| Column | Type | Nullable | Description |
| ------ | ---- | -------- | ----------- |
| `id` | bigserial | no | Primary key |
| `tenant` | text | no | DEFAULT current_schema() |
| `file_path` | text | no | Relative path from addon root |
| `module_name` | text | no | Owning module (denormalised for fast filter) |
| `content_hash` | text | no | Hash of file content at last index |
| `git_sha` | text | no | Git SHA of the commit the file was indexed at |
| `file_kind` | text | no | `manifest` / `python` / `xml` / `qweb` / `js` |
| `byte_size` | int | no | File size at index time — tripwire for hash collision detection |
| `indexed_at` | timestamptz | no | When the indexer wrote this row |

## Invariants

- `(tenant, file_path)` is unique
- `content_hash` is deterministic (same bytes → same hash) and uses a single algorithm across all file kinds (see graph-store.md)
- `git_sha` may lag behind HEAD if the file is unchanged across commits — that is intentional

## Indexing strategy

- btree on `(tenant, module_name)` for per-module re-index
- btree on `content_hash` for dedup detection across modules (rare, but catches copied files)

## Notes

- We do NOT store file content here. Vector store holds searchable chunks; this table only answers "has it changed".
- Failures to read a file (permission, disk) are logged, not persisted — retry on next indexer run
