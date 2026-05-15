# Phase 1 Diagnose Report

**Date:** 2026-05-15
**Branch:** wt-diagnose
**Worktree:** /home/<user>/git/odoo-semantic-mcp-wt-diagnose

---

## Branch-guard verification

```
$ cd /home/<user>/git/odoo-semantic-mcp-wt-diagnose && pwd
/home/<user>/git/odoo-semantic-mcp-wt-diagnose

$ git rev-parse --show-toplevel
/home/<user>/git/odoo-semantic-mcp-wt-diagnose

$ git branch --show-current
wt-diagnose
```

GUARD PASSED. pwd is the wt-diagnose worktree, branch is `wt-diagnose`.

---

## Verdict matrix

| ID | Root cause | Fix location | Complexity | Confidence |
|----|-----------|--------------|------------|------------|
| F1 | code-bug | `writer_neo4j.py:72` (ON MATCH SET overwrites is_definition unconditionally) | small | high |
| F2 | operational (stale sentinel) | `seed_patterns.py:427-436` (--no-embed path updates sentinel without pgvector write) | small | high |
| F3 | stale-data | N/A — child-profile repos were never indexed; current code correct | N/A (reindex will fix it) | high |
| F4 | stale-data + code-gap | `writer_neo4j.py:85-93` (WHERE NOT EXISTS blocks backfill of old self-INHERITS edges) | medium | high |
| F5 | code-bug (design mismatch) | `mcp/middleware.py:173` (reads X-Tool-Name header never sent by MCP clients) | medium | high |

---

## F1 — sale.order is_definition=FALSE

### Evidence

Live Neo4j query:
```
MATCH (m:Model {name:'sale.order', module:'sale'})
RETURN m.is_definition, m.had_explicit_name, m.odoo_version, m.profile
```
Output:
```
m.is_definition, m.had_explicit_name, m.odoo_version, m.profile
FALSE, FALSE, "17.0", NULL
TRUE, TRUE, "8.0", ["odoo_8"]
TRUE, TRUE, "9.0", ["odoo_9"]
TRUE, TRUE, "10.0", ["odoo_10"]
TRUE, TRUE, "11.0", ["odoo_11"]
```

The v17 node has `is_definition=FALSE, had_explicit_name=FALSE, profile=NULL`.

Overall distribution:
```
val=FALSE: 4529 Model nodes
val=TRUE: 4178 Model nodes
val=NULL: 50 Model nodes
```

The sale.order in `sale` v17 was indexed on `2026-05-14 11:59` (repo id=1).
ADR-0016 commit landed at `2026-05-14 18:03` — hence profile=NULL (indexed before ADR-0016). This is separate from the is_definition=FALSE bug.

### Code trace

`odoo_17.0/addons/sale/models/sale_order.py` line 31-32:
```python
_name = 'sale.order'
_inherit = ['portal.mixin', 'product.catalog.mixin', 'mail.thread', ...]
```
→ `had_explicit_name=TRUE`, `is_definition=TRUE` (name not in inherit_list)

BUT `odoo_17.0/addons/sale/populate/sale_order.py` line 11-12:
```python
class SaleOrder(models.Model):
    _inherit = "sale.order"   # NO _name!
```
→ `parser_python.py:454`: `had_explicit_name=False` (default)
→ `parser_python.py:759-770`: name derived from inherit[0] = "sale.order"
→ `had_explicit_name=FALSE, is_definition=FALSE`

`writer_neo4j.py:60-82` — the MERGE for both files uses the SAME composite key
`(name='sale.order', module='sale', odoo_version='17.0')`:

```python
# writer_neo4j.py:71-72 — ON MATCH SET overwrites unconditionally
ON MATCH  SET m.had_explicit_name = $had_explicit_name,
              m.is_definition = ($had_explicit_name AND NOT $name IN $inherit_list),
```

The `populate/sale_order.py` parse result runs AFTER `models/sale_order.py` (files processed alphabetically: models/ before populate/). The MERGE finds the existing node and **unconditionally overwrites** `is_definition` with `FALSE`.

### Verdict

**CODE BUG** at `writer_neo4j.py:72`. The `ON MATCH SET` clause overwrites `is_definition` regardless of whether the incoming data is from an extension class (no `_name`) or a definition class. Once set to `TRUE`, it should never be overwritten to `FALSE`.

The formula should use `m.is_definition = m.is_definition OR ($had_explicit_name AND NOT $name IN $inherit_list)` (i.e., is_definition is monotonic TRUE once set).

Note: The 366 nodes with `had_explicit_name=TRUE, is_definition=FALSE` are CORRECT behavior (self-extension pattern: `_name='X'` AND `_inherit=['X']`). Only the populate/report/wizard extension files that have no `_name` but inherit the same model name cause incorrect clobbering.

**Recommendation for Phase 2A:** Fix `writer_neo4j.py:71-72` to use OR logic. Small change, high risk if wrong — needs integration test with a sale-like scenario (definition file + extension file in same module). Also add test covering populate/wizard pattern.

---

## F2 — Pattern dual-store divergence: Neo4j=92, pgvector=0

### Evidence

```
# Neo4j PatternExample count:
MATCH (p:PatternExample) RETURN count(p) as count
→ 92

# pgvector embeddings for patterns:
SELECT COUNT(*) FROM embeddings WHERE chunk_type='pattern_example';
→ 0

# _SeedMeta sentinel:
MATCH (n:_SeedMeta) RETURN n.key, n.sha256, n.updated_at, n.hash
→ "patterns", "96357e8629e5de6c...", 2026-05-11T13:36:41.064Z, NULL

# Current patterns.json sha256:
sha256sum src/data/patterns.json
→ 96357e8629e5de6c744277e14d7b6179ffdbdea26d321d9e405e17455d41c733
```

The sentinel sha256 MATCHES the current file → every auto-reseed call says "patterns unchanged — skipping". pgvector has 0 pattern embeddings while Neo4j has 92 PatternExample nodes.

From the reindex log (2026-05-15):
```
INFO Patterns unchanged (sha=96357e8629e5) — skipping reseed
INFO Auto-reseed: patterns unchanged — skipping
```

### Code trace

`seed_patterns.py` `main()` CLI path — lines 427-436:
```python
if args.no_embed:
    _logger.info("Skipping pgvector embed step (--no-embed)")
    # Still update sentinel even if no embed (only Neo4j written).
    writer = _get_neo4j_writer()
    if writer:
        try:
            _set_stored_patterns_sha(writer.driver, current_sha)
        finally:
            writer.close()
    _mark_done()
    return 0
```

The sentinel was updated on `2026-05-11T13:36:41` with the CURRENT sha256 hash.
This timestamp matches a period when seed_patterns was run (likely via CLI or web UI trigger)
**with `--no-embed` flag** (or with the pgvector step failing silently and sentinel still updated).

Looking at git log for key dates:
- `2026-05-10 20:44` — sentinel hash gating added (commit `4546e78`)
- `2026-05-11 13:36` — sentinel set with current sha (when and how: likely `--no-embed` CLI run)
- `2026-05-11 18:34` — web UI seed-patterns trigger added (commit `c8a26a2`)

The web UI trigger (post-13:36) uses the `run()` function path (not `main()` CLI), which correctly requires `embedder != None` to write pgvector. But since the sentinel was already set by the 13:36 run, every subsequent call (via auto-reseed or CLI) is gated out.

### Root cause breakdown

Option **(b) writing both but pgvector failed silently and sentinel updated** is the most likely. The CLI `main()` path at `seed_patterns.py:427-436` explicitly documents: "Still update sentinel even if no embed". This is a partial-failure rule violation: the sentinel is updated even when pgvector was intentionally skipped via `--no-embed`.

This is an **operational issue** (sentinel was set before pgvector was populated) combined with a **design policy issue** (`--no-embed` updating the sentinel means pgvector will never be seeded unless `--force` is used).

### Verdict

**OPERATIONAL** root cause: sentinel set by `--no-embed` CLI run at 2026-05-11T13:36 before pgvector was ever populated with pattern embeddings. The sentinel now blocks every subsequent reseed attempt.

The fix is two-part:
1. Immediate operational fix: run `python -m src.indexer.seed_patterns --force` (bypasses sentinel), which will write both Neo4j and pgvector.
2. Policy fix: `--no-embed` should NOT update the sha256 sentinel (leave it for when pgvector is successfully written). The sentinel at `seed_patterns.py:430-434` should be conditional.

**Recommendation for Phase 2:** This is a Haiku-tier fix (operational recovery command + 3-line code change). Spawn one WT to fix the sentinel policy; operational recovery can run immediately.

---

## F3 — 45,049 / 107,766 (41.8%) code nodes missing `profile` array

### Evidence

Distribution by node type:
```
label, missing_profile, has_profile
"Field", 14472, 26454
"Method", 18260, 19742
"Model", 3565, 5192
"Module", 809, 1175
```

Nodes with profile IS NOT NULL all have single-element arrays like `["odoo_8"]`:
```
MATCH (m:Model) WHERE m.profile IS NOT NULL
RETURN size(m.profile) as profile_size, count(*) as cnt
→ profile_size=1, cnt=5192
```

Zero nodes have multi-profile (hierarchical) arrays.

### Postgres state

Profiles with `parent_profile_id` set:
```
standard_viindoo_8 → parent: odoo_8
standard_viindoo_9 → parent: odoo_9
...viindoo_internal_17 → parent: standard_viindoo_17 → parent: odoo_17
```

Repos belonging to child profiles (`standard_viindoo_*`, `viindoo_internal_*`):
```
SELECT r.id, p.name as profile, r.last_indexed_at FROM repos r
JOIN profiles p ON r.profile_id = p.id WHERE p.parent_profile_id IS NOT NULL
→ ALL 36 rows have last_indexed_at = NULL
```

**None of the child-profile repos have ever been indexed.**

Repos that WERE indexed (`last_indexed_at != NULL`) all belong to root profiles:
`odoo_8`, `odoo_9`, `odoo_10`, `odoo_11`, `odoo_17`.

Root profiles have NO parent (`parent_profile_id IS NULL`), so `get_ancestor_profile_names()` returns `[profile_name]` (self only). Hence profile arrays = `["odoo_8"]` (single element — no hierarchy to propagate).

### Code trace

`pipeline.py:505-511`:
```python
ancestor_profiles = repo_store().get_ancestor_profile_names(profile_name)
if not ancestor_profiles:
    ancestor_profiles = [profile_name]
```

`repo_registry.py:183-206`: `get_ancestor_profile_names()` uses recursive CTE walking `parent_profile_id` upward — correctly implemented.

`writer_neo4j.py:39-41`, `68-74`, `149-151`, `165-167`, `199-201`, `253-255`, `295-297`, `330-332`:
All node types have profile SET in both ON CREATE and ON MATCH. Code is correct.

ADR-0016 commit landed `2026-05-14 18:03`. Repos indexed AFTER this date:
- `odoo_10` repo (id=24): indexed `2026-05-14 18:20` — profile = `["odoo_10"]` (single)
- `odoo_11` repo (id=27): indexed `2026-05-14 19:02` — profile = `["odoo_11"]` (single)

Both are root profiles (no parent), so single-element profile is CORRECT.

### Verdict

**STALE DATA** — no code bug. The 45k nodes with `profile=NULL` were indexed before ADR-0016 was implemented (`2026-05-14 18:03`). The "0 hierarchical" finding means no child-profile repos have been indexed yet (all `standard_viindoo_*` and `viindoo_internal_*` repos have `last_indexed_at=NULL`).

Current code correctly implements ADR-0016. A **full reindex of all repos will fix the missing profiles**. For hierarchical profiles to appear, child-profile repos must be indexed.

**Recommendation for Phase 2:** Skip dedicated WT — operational reindex will fix it. Document the "reindex all" operational step in the go-live runbook. Confidence: high.

---

## F4 — 765 / 8146 (9.4%) INHERITS edges missing `order` property

### Evidence

```
MATCH ()-[r:INHERITS]->() RETURN count(case when r.order IS NULL then 1 end) as missing, count(case when r.order IS NOT NULL then 1 end) as has_order
→ missing=765, has_order=7381

MATCH (m:Model)-[r:INHERITS]->(p:Model) WHERE r.order IS NULL
RETURN m.odoo_version as ver, count(*) as cnt
→ ver="8.0", cnt=765
```

ALL 765 missing-order edges are `odoo_version=8.0`. None are unresolved:
```
MATCH ()-[r:INHERITS]->() WHERE r.order IS NULL
RETURN r.unresolved, count(*)
→ NULL, 765
```

Sample of affected edges:
```
m.name, p.name, m.module, m.odoo_version
"res.groups", "res.groups", "base", "8.0"
"res.users", "res.users", "base", "8.0"
"res.users", "res.users", "auth_crypt", "8.0"
"res.company", "res.company", "auth_ldap", "8.0"
```

Confirming the pattern — `res.company/auth_ldap` has mixed edges:
```
MATCH (m {name:'res.company', module:'auth_ldap', odoo_version:'8.0'})-[r:INHERITS]->(p)
RETURN p.module, r.order
→ "web_favicon", 0
→ "l10n_be_intrastat", NULL
→ "base", NULL
```

### Code trace

`writer_neo4j.py:84-93` — the self-inherit code path (when `parent_name == model.name`):

```python
for idx, parent_name in enumerate(model.inherit):
    if parent_name == model.name:
        tx.run(f"""
            MATCH (ext:Model {{name: $name, module: $mod, odoo_version: $v}})
            MATCH (tip:Model {{name: $name, odoo_version: $v}})
            WHERE tip.module <> $mod
              AND NOT (:Model {{name: $name, odoo_version: $v}})-[:{REL_INHERITS}]->(tip)
            MERGE (ext)-[r:{REL_INHERITS}]->(tip)
            SET r.order = $order
        """, ...)
```

The `WHERE NOT ... EXISTS` guard prevents MERGE from matching if ANY Model with that name already has an INHERITS edge to `tip`. This is designed to prevent duplicate edges from multiple extension modules pointing to the same tip.

But the side effect: if an INHERITS edge was created WITHOUT `r.order` (by a pre-ADR-0013 indexer run), subsequent re-index runs CANNOT update it because the MERGE never fires (the edge exists → WHERE NOT EXISTS is FALSE → MATCH returns 0 rows → SET never executes).

The non-self-inherit path (`writer_neo4j.py:96-103`) uses plain `MERGE ... SET r.order = $order` which DOES update existing edges. That's why 7381 edges have correct order and only 765 (self-inherit edges from 8.0) are missing.

### Root cause timeline

ADR-0013 code (`r.order` property) landed: `2026-05-10 10:26` (commit `04af2fc`).
odoo_8 repos registered in postgres: `2026-05-11 14:40`.
odoo_8 repos indexed (first postgres-tracked run): `2026-05-12 04:26`.

The 765 NULL-order edges were created **before 2026-05-10** by a CLI indexer run directly against Neo4j (before postgres tracking was set up). The Docker volume preserved this data. When the post-ADR-0013 indexer ran on 2026-05-12, the WHERE NOT EXISTS guard prevented backfilling `r.order` on those pre-existing edges.

Evidence: `auth_ldap/res.company` has edge to `web_favicon/res.company` with `order=0` (set on 2026-05-12 when web_favicon tip had no incoming INHERITS) and edges to `l10n_be_intrastat` and `base` with `order=NULL` (created pre-ADR-0013, blocked from update).

### Verdict

**STALE DATA** from pre-postgres-tracking Neo4j volume — combined with a **code design gap**: the self-inherit MERGE cannot backfill `order` on existing edges because the WHERE NOT EXISTS guard makes the MERGE a no-op when the edge exists.

The WHERE NOT EXISTS is necessary to prevent fanout (multiple modules all creating INHERITS to the same tip), but it should use a different strategy to allow backfill when `r.order IS NULL`.

Fix options for Phase 2:
1. One-time Cypher migration: `MATCH ()-[r:INHERITS]->() WHERE r.order IS NULL SET r.order = 0` (approximate, loses real order).
2. Fix the WHERE NOT EXISTS to also allow MATCH when `r.order IS NULL` on the existing edge (writer code change).
3. Full reindex with `--full` flag for odoo_8 repos (also re-creates the data that's otherwise blocked).

**Recommendation for Phase 2:** Sonnet-tier WT. Fix involves two sub-tasks: (a) code fix for the WHERE NOT EXISTS backfill gap, (b) one-time migration to set order=0 on the 765 stale edges. Risk: medium (the guard exists for a reason; the fix must not introduce fanout).

---

## F5 — `usage_log.tool_name='unknown'` for all 8,439 rows

### Evidence

```sql
SELECT tool_name, count(*) as cnt FROM usage_log GROUP BY tool_name ORDER BY cnt DESC LIMIT 10;
→ unknown | 8439
```

### Code trace

`mcp/middleware.py:173` — the ASGI `AuthMiddleware._log_usage_async`:
```python
async def _log_usage_async(key_id: int, request: Request, ms: int) -> None:
    try:
        from src.db.pg import auth_store
        tool = request.headers.get("X-Tool-Name", "unknown")   # ← line 173
        _logger.info("mcp_tool tool=%s key_id=%s ms=%d", tool, key_id, ms)
        await asyncio.to_thread(lambda: auth_store().log_usage(key_id, tool, ms))
    except Exception:
        pass
```

The `X-Tool-Name` HTTP header is **never set by any standard MCP client**. The MCP protocol (JSON-RPC 2.0) encodes tool calls in the request body:
```json
{"jsonrpc":"2.0","method":"tools/call","params":{"name":"resolve_model","arguments":{...}}}
```

The tool name is in `params.name` — only available AFTER the JSON-RPC body is parsed by FastMCP, which happens AFTER the ASGI middleware layer runs.

FastMCP provides a proper `Middleware` class with an `on_call_tool` hook:
```python
# fastmcp/server/middleware/middleware.py:157-162
async def on_call_tool(
    self,
    context: MiddlewareContext[mt.CallToolRequestParams],
    call_next: CallNext[mt.CallToolRequestParams, ToolResult],
) -> ToolResult:
    return await call_next(context)
```

`context.message` is `CallToolRequestParams` which has a `.name: str` field (verified: `mcp/types.py:1348-1351`). This gives the tool name correctly — but it's only available inside a FastMCP-level middleware, not in the ASGI-level `AuthMiddleware`.

There are no other `X-Tool-Name` references anywhere in the codebase — confirmed by grep.

### Why ASGI middleware can't read the body

`AuthMiddleware` is a `BaseHTTPMiddleware` (Starlette). At the time it runs, the HTTP body is an async stream. Reading it would consume the stream before FastMCP can process the JSON-RPC payload. Reconstructing the stream is fragile and non-standard.

### Verdict

**CODE BUG** — design mismatch between logging layer (ASGI) and tool name availability (FastMCP layer). The `X-Tool-Name` header approach assumes clients will set this header, but no MCP client does this (it's not part of the MCP spec).

The correct fix is to move usage logging into a **FastMCP-level middleware** where `context.message.name` is available. This requires:
1. Implement a `class UsageLogMiddleware(fastmcp.Middleware)` with `on_call_tool` hook.
2. Pass `api_key_id` from request state into the FastMCP context (FastMCP has a `Request` dependency available via `context.fastmcp_context`).
3. Remove or repurpose the `X-Tool-Name` header path in `AuthMiddleware`.

**Recommendation for Phase 2:** Sonnet-tier WT. Medium complexity — requires understanding FastMCP middleware integration. The current ASGI middleware correctly handles auth/rate-limiting; only the tool-name extraction needs to move to FastMCP layer. Risk: medium (auth middleware refactor).

---

## Cross-cutting observations

### 1. Sentinel update-before-pgvector-success is a recurring anti-pattern risk

`seed_patterns.py` `main()` CLI at lines 427-436 updates the sentinel after `--no-embed`, even though pgvector was intentionally skipped. This violates the "update sentinel only after full success" principle stated in ADR-0007. The pipeline `run()` function is correct (only updates sentinel after both writes succeed), but the CLI path has a different code path that can cause divergence.

### 2. is_definition clobber affects 17.0 data quality

The ON MATCH SET overwrite pattern in `writer_neo4j.py:72` likely affects more than just `sale.order`. Any module with both a definition class (`_name = 'X'`) AND a populate/wizard/report extension class (no `_name`, only `_inherit = 'X'`) in the SAME Odoo module will have `is_definition` clobbered. The severity depends on file processing order within the module.

### 3. Self-INHERITS WHERE NOT EXISTS guard is semantically incorrect

The guard `AND NOT (:Model {name:$name, odoo_version:$v})-[:INHERITS]->(tip)` prevents ANY Model with that name from having INHERITS to `tip`, not just the current `ext` module. This means as soon as ONE module creates a self-inherit edge to a tip, ALL other modules are blocked from creating their own edge to that tip. This may be causing under-counting of self-inherit relationships. Needs investigation in Phase 2.

### 4. Field.field_count is NULL on all sampled nodes

All queried Model nodes show `field_count=NULL`. This is the ADR-0013 T2 fallback for ranking. If field_count is never populated, T2 fallback is effectively disabled. Worth investigating whether field_count is ever set (separate from the 5 findings).

### 5. Profile=NULL and profile=["singleton"] are different problems

Nodes with `profile=NULL` are pre-ADR-0016 data (indexed before 2026-05-14 18:03). Nodes with single-element profile arrays are correctly indexed but with root profiles that have no parent. The "0 hierarchical" audit finding is misleading — it should say "0 child-profile repos indexed".

---

## Recommendation for Phase 2 dispatch

| Finding | WT needed? | Tier | Risk | Notes |
|---------|-----------|------|------|-------|
| F1 | YES | Sonnet | Medium | Fix `writer_neo4j.py:71-72` ON MATCH SET to OR logic; add integration test for same-module definition+extension pattern |
| F2 | YES (partial) | Haiku | Low | Two steps: (1) run `seed_patterns --force` immediately; (2) fix `--no-embed` sentinel update policy in `seed_patterns.py:427-436` |
| F3 | NO | N/A | N/A | Skip — reindex child-profile repos will populate profile arrays. Document in go-live runbook |
| F4 | YES | Sonnet | Medium | Fix self-INHERITS backfill logic + one-time Cypher migration for 765 stale edges. Investigate WHERE NOT EXISTS semantics |
| F5 | YES | Sonnet | Medium | Implement FastMCP-level `UsageLogMiddleware` with `on_call_tool` hook to extract tool name from `context.message.name` |

**Suggested dispatch order:**
1. F2 (Haiku — quick operational fix, unblocks find_examples semantic search immediately)
2. F1 (Sonnet — data quality fix for ranking, needs test)
3. F5 (Sonnet — logging quality, no data impact but needed for observability)
4. F4 (Sonnet — stale data + code gap, depends on understanding full impact of WHERE NOT EXISTS guard)
5. F3 (no WT — operational reindex)
