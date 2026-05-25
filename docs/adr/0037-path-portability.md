# ADR-0037 — Path Portability (Repo-Relative Storage + Output, Migration-Safe)

**Date:** 2026-05-25
**Status:** Proposed — M13

> Read-side companion to [ADR-0034](0034-multi-tenant-pooled-isolation.md) (who
> may read) and [ADR-0035](0035-git-access-model.md) (how repos are cloned).
> This ADR governs **what file path the client sees and how it survives a host move**.

## Context

OSM indexed source paths as **server-absolute** strings everywhere:

- **Storage:** pgvector `embeddings.file_path`; Neo4j `Module.path`,
  `OWLComp/JSPatch/CoreSymbol/CLICommand.file_path`, and `Stylesheet.file_path`
  + `LintViolation.file_path` (the last two sit *inside* the composite MERGE key).
- **Output:** 8 MCP render sites emitted those absolute paths verbatim
  (`find_examples`, `lookup_core_api`, `describe_module`, `module_inspect` JS,
  `resolve_stylesheet`, `find_style_override`, + import/override chains).

Three problems followed:

1. **Client unusability.** An AI client runs on a *different* machine; a path
   like `/home/tuan/git/odoo_17.0/addons/sale/models/sale_order.py:245` does not
   exist on its disk. The portable fact is the **repo-relative** tail
   (`addons/sale/models/sale_order.py`) which the client maps onto its own checkout.
2. **Server migration breaks reads.** Absolute paths are baked into MERGE keys,
   embeddings, GC (`Module.path IN live_paths`) and `resources.py open()`. Moving
   the server (new host / new `$HOME` / re-clone path) invalidates all of them.
3. **No SSOT for the on-disk location.** The repo's on-disk root already has a
   single source of truth — `repos.local_path` in Postgres. Duplicating it inside
   every stored path (and serving it) violates SSOT and is the root of (1)+(2).

`PatternExample.file_ref` was already relative (`addons/sale/models/sale_order.py:245`)
— the model the rest of the system should follow.

## Decision

### D1 — Stored paths are repo-relative; `repos.local_path` is the only anchor

The indexer stores every file path **relative to the repo checkout root**
(`addons/web/static/src/scss/foo.scss`), never absolute. `repos.local_path`
(Postgres) remains the single absolute anchor. Any absolute path needed at serve
time is reconstructed dynamically as `local_path / <relative>`.

Consequence — **server migration is a `local_path` re-point, no reindex**: the
relative paths in Neo4j/pgvector stay valid across hosts; only the one anchor row
changes. Update `local_path` (re-clone recomputes it, or `manager apply-preset
--repo-map`, or direct SQL) and restart the service (which clears the in-memory
resource cache). See `docs/deploy/disaster-recovery.md §Migration to New Host`.

### D2 — Relativize at the writer boundary (parser stays absolute)

Parsers keep reading files via absolute `rglob` paths (unchanged). Conversion to
relative happens at the **persistence boundary** using `ModuleInfo.repo_root`
(a transient field set by `build_registry`, never persisted):

- `writer_neo4j`: `Module.path`, `OWLComp/JSPatch.file_path`, and (via a
  `repo_root` arg) `Stylesheet.file_path` + each `@import` target.
- `writer_pgvector`: `make_chunks` + `make_css/scss/less_chunks` relativize
  `file_path` (the css/scss/less builders also now receive `ModuleInfo` so their
  chunks carry `repo`/`repo_id` — closing the provenance gap; see D4).

`to_repo_relative()` / `ModuleInfo.relative_path()` are **idempotent**: a path
already relative (or not under repo_root) is returned unchanged.

### D3 — CoreSymbol & CLICommand relativize in their parser (source-root anchor)

CoreSymbol and CLICommand come from the Odoo **core source tree**, which has no
`repos` row to anchor against. They relativize against the source root in
`parser_odoo_core` / `parser_cli` (e.g. `odoo/orm/models.py`,
`odoo/cli/server.py`) — matching the existing static `cli_flags_*.json` form.
They are never served as raw on-disk files, so no reconstruction is needed.

### D4 — Provenance is never lost

Every node/chunk retains `module` + `odoo_version`, and `repo`/`repo_id` where a
repo applies. The css/scss/less embedding chunks previously omitted `repo`/`repo_id`;
they now carry them. So dropping the absolute path loses no identifying
information — `(repo_url/repo, module, relative_path, version)` fully replaces it.
(CoreSymbol carries no repo by design — it is core, not a tenant repo.)

### D5 — Read-side normalization is a permanent safety-net

`_portable_path()` in `server.py` strips any absolute prefix at render time
(anchored on the `repo`/`module` segment, or `/odoo/`·`/openerp/` for core).
It is applied at all 8 render sites and is **idempotent** on relative input. This:

- makes output portable **immediately**, even before/around the reindex window;
- defends against any future code path that stores an absolute string.

### D7 — Repo identity in output is the portable git URL, not the server dirname

The path is only half the portability story: the `[repo]` label that prefixes it
was `Module.repo` = `Path(local_path).name` = the **server checkout directory name**
(`odoo_17.0`, `acme_addons17`) — host-specific detail an AI client cannot map to its
own checkout. Output now shows `repo_url` (`github.com/odoo/odoo`) as the repo
identity, falling back to the dirname only when no URL is known (locally-registered
repos). Implementation:

- **Neo4j-sourced tools** project `coalesce(mod.repo_url, mod.repo) AS repo` in-query,
  so every existing `[repo]` render site becomes portable with **zero render edits**.
  (The dirname is still needed as a path-strip anchor only in the few sites that strip
  legacy absolute paths — those anchor on `module` instead.)
- **`find_examples`** (pgvector, no Module join) selects `repo_id` and resolves
  `repo_id → repos.url` at render via a small process cache (`_repo_url_for_id`).
- **`describe_module`** shows `Repo URL:` when present and suppresses the redundant
  `Repo:` dirname line (dirname shown only as the no-URL fallback).

`repo_url` is **not** denormalized onto embedding chunks (SSOT: it lives on
`repos`/`Module`); it is resolved at read time.

### D8 — Stylesheet `:IMPORTS` target MATCH is repo-scoped

Relativization removes the old global uniqueness of `Stylesheet.file_path`: two
repos at the same `odoo_version` (e.g. community + an enterprise overlay) can now
hold the SAME relative path
(`addons/web/static/src/scss/variables.scss`). The `:IMPORTS` writer's target
MATCH was keyed only on `(file_path, odoo_version)`, so it would match BOTH and
create spurious cross-repo import edges. A SCSS `@import` always resolves within
the importing repo, so the writer now stamps a `repo_id` property on each
`:Stylesheet` (threaded from the pipeline's `repo` dict, mirroring `repo_root`)
and scopes BOTH the `src` and `tgt` MATCH by `repo_id` (null-safe equality —
Cypher has no `IS NOT DISTINCT FROM`). The new property is free: the reindex is
already full. Read-side stylesheet queries are unchanged (they traverse existing
`:IMPORTS` edges; the scoping is write-side only).

### D6 — Stylesheet `odoo://` resource reconstructs absolute dynamically

`resources.py _render_stylesheet` resolves the owning Module's `repo_id`
(`(ss)-[:DEFINED_IN]->(Module)`), looks up `repos.local_path`, and opens
`local_path / relative`. Legacy absolute rows open verbatim; the query matches
both `$fp` (relative) and `$fp_abs` (legacy) for back-compat during the reindex.

## Migration & MERGE-key impact

`Stylesheet.file_path` and `LintViolation.file_path` are MERGE-key components, so
a reindex creates new relative-keyed nodes while the old absolute-keyed nodes
linger as orphans. After a full `--full` reindex v8→v19, run
`ops/cleanup_absolute_path_nodes.cypher` to `DETACH DELETE` any node whose
`file_path STARTS WITH '/'`, then verify both Neo4j counts and
`SELECT count(*) FROM embeddings WHERE file_path LIKE '/%'` are 0. The pgvector
delete-then-insert write needs no SQL migration. **Gate:** the reindex must be
full and cover all repos — a mixed absolute/relative graph would make GC see every
module as stale and delete it. `gc_stale_modules` enforces this in code: before
deleting it counts Module nodes with an absolute (`STARTS WITH '/'`) path for the
repo+version, and if any exist it SKIPS GC, logs a warning, and returns 0 — so an
incremental `--gc` run against a not-yet-migrated graph can never blast the repo.

## Consequences

- Output is portable and self-describing; clients map paths onto their checkout.
- Server migration no longer requires a reindex (re-point `local_path` + restart).
- One extra Neo4j hop + a cached Postgres lookup when serving a stylesheet resource.
- Tests that asserted absolute output/storage are updated to the relative contract
  (the business rule changed — this is not loosening tests to fit code).
