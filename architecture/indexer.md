---
status: confirmed
confirmed_date: 2026-04-22
scope: architecture/indexer
reads-with:
  - overview.md
  - graph-store.md
  - ../data-model/modules.md
  - ../data-model/cache_metadata.md
  - ../research/odoo-internals.md
---

# Indexer

Parses Odoo source (Python, XML, QWeb, JS) and produces structured output for the graph + vector stores. Idempotent, git-aware, incremental.

## Responsibilities

- Walk one or more Odoo addon paths
- Parse `__manifest__.py` for each addon (dependencies, data files, flags)
- Simulate Odoo's own module load order → produce canonical ordering
- Parse Python files (`libcst`) → extract models, fields, methods, `_inherit` / `_inherits`
- Parse XML files (`lxml`) → extract views, records, actions, inheritance pointers
- Parse QWeb + JS (P4) → extract templates, OWL components
- Compute per-chunk content hash for idempotent upserts
- Determine inheritance edges and override chains
- Enqueue changed chunks for embedding

## Non-responsibilities

- Running Odoo (we are static-only for MVP — runtime introspection is an L2 future layer per `moat` design)
- Embedding — that lives in the embedder component
- Serving queries — MCP server handles reads
- Git operations — caller arranges checkout; indexer reads the filesystem

## Inputs

- One or more addon paths (local filesystem)
- Current git SHA (for cache key)
- Optional: previous indexing result for diff-based re-indexing

## Outputs

- Upserts into `modules`, `models`, `fields`, `methods`, `views` tables
- Inheritance edges into their respective `*_inherits` edge tables
- Cache metadata rows keyed by `(tenant, file_path)` + SHA — see [`../data-model/cache_metadata.md`](../data-model/cache_metadata.md)
- A queue of chunks needing (re-)embedding

## Key design choices (to be promoted to ADRs)

- **`libcst` over `ast`** — preserves whitespace/comments so returned snippets are byte-accurate
- **Manifest simulation** — we reimplement Odoo's load-order logic rather than booting Odoo. Accepts the accuracy risk for P1; runtime-verification considered for P3+
- **Per-chunk content hash** — not per-file. Method-level granularity means one method change re-embeds one chunk, not the whole file

## Parser strategy — `_inherit` is authoritative

A grep across CE 17.0 (`odoo/` + `addons/`) finds **zero** runtime assignments to `_inherit` after class creation (`../research/odoo-internals.md` §5). `MetaModel.__new__` reads it once at class definition time (`odoo/models.py:199-220`); `_build_model` reads it once at registry build (`odoo/models.py:713`). Any later mutation is ignored by the loading path.

**Default parser stance: treat AST `_inherit = [...]` literals as authoritative.** Do not introduce speculative `resolution: unknown` for multi-inherit lists, third-party addons, or complex dependency chains. These are all fully deterministic from static analysis.

**`resolution: unknown` applies only to three specific edge cases:**

1. **Conditional import guard** — class imported inside `try/except ImportError` (optional dependency). Emit `resolution: conditional`.
2. **`_register = False` chain** — class opts out of registry insertion; subclass may re-enable it (`odoo/addons/base/models/ir_qweb.py:2702`). Cannot confirm registration without full subclass traversal.
3. **DB-origin manual fields / models** — `ir.model.fields` rows with `state='manual'` injected via `ir.model.fields._add_manual_fields` at `odoo/models.py:3374`. Invisible to static AST; document in warnings. Offer live-DB introspection as optional L2 upgrade path.

**`studio_customization`** is filtered out of the dependency graph by Odoo itself (`odoo/modules/graph.py:18`). Safe to ignore.

## Known risks

- Manifest `depends` cycles — detect and surface as indexer error, abort module rather than infinite-loop
- Studio views (DB-stored) — explicitly out of scope; document in warnings when detected
- Python import failure — if a module's package fails to import (missing optional lib), log as warning rather than silently dropping the module; trust its AST up to the failing import point
- Methods and fields use **different** resolution rules — C3 MRO for methods, `_base_fields` stack for fields. The indexer must apply them separately; see `../specs/resolve_method.md` §5b and `../specs/resolve_field.md` §5b

## Where to look next

- Output schema: [`graph-store.md`](graph-store.md) + [`../data-model/`](../data-model/)
- MCP tools that consume this output: [`../specs/`](../specs/)
- Embedding pipeline (downstream): [`vector-store.md`](vector-store.md)
