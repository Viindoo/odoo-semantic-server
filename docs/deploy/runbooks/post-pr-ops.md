# Post-PR OPS Actions

> Three admin actions on production to materialize new indexes and correct stale embeddings. Run after the matching PR is merged and deployed. See ADR-0023 for tool output completeness policy that motivates these actions.

## Nguyên Lý

Code merge ≠ data materialization. Database schema migrations create tables and policies, but three types of data state require separate OPS workflows:

1. **Neo4j indexes** — graph indexes must be created after schema changes but are not automatically applied by code. Each new index definition in `writer_neo4j.py` requires an explicit `setup_indexes()` call.
2. **Parser logic changes** — a new parser feature (e.g., era1 `comodel_name` extraction for v8/v9) only applies to repos processed *after* the code ships. Existing Field nodes remain stale until a full reindex.
3. **Embeddings** — expensive compute operation that only runs on demand during `index-repo` or explicit `embed` commands. New versions may lack embeddings entirely if never indexed, causing semantic search to return empty.

These three commands bring the data layer into parity with shipped code logic.

## Precondition

- PR mentioned in each action has been merged into `master` and deployed to production.
- Verify: `git log --oneline -1` on prod server = matching commit SHA.
- Both Postgres and Neo4j are healthy:
  ```bash
  curl -s <HEALTH_URL>/health | jq '.status'  # expect "healthy"
  ```
- Disk space ≥ 5GB free on Postgres and Neo4j volumes (for temp state during reindex).
- Low-traffic window (actions #2 and #3 are CPU-bound, 30–60 min each).

## Placeholder Reference (ADR-0027 Portable Paths)

| Placeholder | Meaning | Example/Default |
|---|---|---|
| `<APP_USER>` | Unix user running Odoo Semantic MCP | `odoo-semantic` |
| `<VENV_PATH>` | Path to Python executable in app venv | `/home/<APP_USER>/.venv/odoo-semantic-mcp/bin/python` |
| `<PROFILE_8>` | Profile name for Odoo v8.0 repos | `odoo_8` (verify in `profiles` table) |
| `<PROFILE_9>` | Profile name for Odoo v9.0 repos | `odoo_9` |
| `<PROFILE_17>` | Profile name for Odoo v17.0 repos | `odoo_17` |
| `<HEALTH_URL>` | Base URL for /health endpoint | `http://localhost:8002` (or prod domain) |

---

## Action 0 — Apply Latest Postgres Migrations

### When to Run

After every PR that ships new migrations. Run BEFORE any other actions — schema changes are prerequisite for data-layer actions.

**Current migrations (PR #200):**
- `m13_006_plans_quota.sql` — `plans` table + `api_keys.plan_id` FK + `usage_counter` table (ADR-0039 control-plane DDL)
- `m13_007_usage_counter_cascade.sql` — ON DELETE CASCADE on `usage_counter.api_key_id` FK

### Command

```bash
sudo -u <APP_USER> <VENV_PATH> -m src.db.migrate
```

(The migrate runner is idempotent — safe to re-run; already-applied migrations are skipped.)

### Verify Success

```bash
# Confirm tables exist
psql -d $DB_NAME -c "\dt usage_counter"
# Expected: table "usage_counter" in schema "public"

psql -d $DB_NAME -c "\dt plans"
# Expected: table "plans" in schema "public"

# Confirm FK column added to api_keys
psql -d $DB_NAME -c "\d api_keys" | grep plan_id
# Expected: plan_id | character varying | not null default 'free'

# Confirm CASCADE on usage_counter
psql -d $DB_NAME -c "\d usage_counter" | grep api_key_id
# Expected: api_key_id | integer | not null (with REFERENCES api_keys(id) ON DELETE CASCADE)
```

### Expected Duration

<30 seconds. Migrations are DDL-only with no data transform.

---

## Action 1 — Materialize Method(model, odoo_version) Neo4j Index

### When to Run

After PR ships the `Method(model, odoo_version)` composite index (WI-A T1) — resolves Q3 timeout regression on models with 50+ extending modules (e.g., `sale.order` on Odoo 17.0).

### Command

```bash
sudo -u <APP_USER> <VENV_PATH> -m src.indexer index-repo \
  --profile <PROFILE_17> --no-embed
```

(Substitute any profile name; indexer will call `setup_indexes()` as part of its initialization. `--no-embed` skips the expensive embedding step since data is already indexed.)

### Expected Duration

~2 minutes, zero-downtime (graph index creation is online in Neo4j 5.x).

### Verify Success

```bash
# Neo4j browser or cypher-shell:
SHOW INDEXES YIELD name, labelsOrTypes, properties
WHERE 'Method' IN labelsOrTypes
RETURN name, properties;
```

Expected output: a composite index named `index_<hash>` with `properties: [model, odoo_version]`.

```bash
# Smoke test: query a high-degree model
curl -X POST <HEALTH_URL>/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {
      "name": "model_inspect",
      "arguments": {
        "model": "sale.order",
        "method": "methods",
        "odoo_version": "17.0"
      }
    }
  }' | jq '.result.completion' | head -5
```

Expected: response in <2 seconds (vs >30 seconds without the index).

---

## Action 2 — Reindex v8 and v9 to Materialize comodel_name

### When to Run

After PR ships era1 (v8/v9) `comodel_name` extraction in `parser_python.py` (WI-C C2). Without reindex, existing Field nodes for v8/v9 have NULL `comodel_name`, breaking `resolve_orm_chain` on those versions.

### Command

Run sequentially (do not parallelize) during low-traffic window:

```bash
sudo -u <APP_USER> <VENV_PATH> -m src.indexer index-repo \
  --profile <PROFILE_8> --full

sudo -u <APP_USER> <VENV_PATH> -m src.indexer index-repo \
  --profile <PROFILE_9> --full
```

(The `--full` flag forces reindex of all modules in the profile, bypassing the incremental skip-unchanged optimization.)

### Expected Duration

30–60 minutes CPU per profile, sequentially. Plan for 1–2 hours total.

### Verify Success

```sql
-- Postgres psql or query tool:
SELECT count(*) AS total_relational_fields,
       count(f.comodel_name) AS materialized
FROM embeddings e
JOIN modules m ON e.module_id = m.id
WHERE m.profile_name = '<PROFILE_8>'
  AND e.odoo_version = '8.0'
  AND e.chunk_type = 'field'
ORDER BY odoo_version DESC;
```

Expected: `materialized = total_relational_fields` (was 0 before reindex).

Alternatively, check Neo4j:

```cypher
MATCH (f:Field {odoo_version: '8.0'}) 
WHERE f.ttype IN ['many2one','one2many','many2many']
RETURN count(*) AS total, count(f.comodel_name) AS materialized;
```

Expected: `materialized = total`.

```bash
# Smoke test: resolve ORM chain on v8
curl -X POST <HEALTH_URL>/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {
      "name": "resolve_orm_chain",
      "arguments": {
        "model": "sale.order",
        "dotted_path": "partner_id.name",
        "odoo_version": "8.0"
      }
    }
  }' | jq '.result.completion' | grep -i broken
```

Expected: no "BROKEN" text in output (all hops resolved).

---

## Action 3 — Re-embed v9.0

### When to Run

After Action 2 completes, if `find_examples` for v9.0 still returns empty results. v9.0 embeddings are stale because the prior text extraction (before parser era1 fix) captured incomplete content. Re-embedding repopulates the embeddings table with fresh vectors.

### Command

Verify the exact CLI in `src/indexer/__main__.py` before running. Typical command:

```bash
# Option A: use built-in reembed-stubs subcommand
sudo -u <APP_USER> <VENV_PATH> -m src.indexer reembed-stubs \
  --profile <PROFILE_9>
```

(Or if a dedicated `embed` subcommand exists, use it per local code.)

### Expected Duration

15–30 minutes depending on embedding API (qwen-turbo) throughput.

### Verify Success

```sql
-- Postgres psql or query tool:
SELECT count(*) AS embedding_count
FROM embeddings
WHERE profile_name = '<PROFILE_9>' AND odoo_version = '9.0';
-- Expect: > count before action
```

```bash
# Smoke test: semantic search on v9.0
curl -X POST <HEALTH_URL>/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {
      "name": "find_examples",
      "arguments": {
        "query": "archive method",
        "odoo_version": "9.0",
        "limit": 3
      }
    }
  }' | jq '.result.completion' | grep -c '#'
```

Expected: ≥1 result (was 0 before action).

---

## Rollback & Recovery

### Action 1 — Index Creation

Neo4j indexes created by `CREATE INDEX IF NOT EXISTS` are idempotent. If a performance regression is detected after the index, drop it:

```cypher
DROP INDEX index_<name>;
```

(Get exact name from `SHOW INDEXES` output.) Re-creating the index is safe — restart with Action 1 again.

### Action 2 — Reindex

Data changes are additive (Field nodes' `comodel_name` is SET with a value). To rollback, set the field to NULL:

```cypher
MATCH (f:Field {odoo_version: '8.0'}) 
WHERE f.ttype IN ['many2one','one2many','many2many']
SET f.comodel_name = NULL;
```

Then re-run Action 2.

### Action 3 — Re-embed

Embeddings are stored in the `embeddings` table. To rollback, delete and re-run:

```sql
DELETE FROM embeddings WHERE profile_name = '<PROFILE_9>' AND odoo_version = '9.0';
```

Then re-run Action 3.

---

## Post-Action Sign-Off

After all three actions complete successfully, update the TASKS.md checklist:

```markdown
- [x] Re-run `setup_indexes()` on prod Neo4j (Action 1 done)
- [x] Re-index v8/v9 `--full` (Action 2 done)
- [x] Re-embed v9.0 (Action 3 done)
```

Commit and push the updated TASKS.md.

---

## Best-effort usage counter (M10B)

The MCP `AuthMiddleware` increments a per-process `_usage_buffer` (RAM only)
on every successful call. The buffer is flushed to the `usage_counter`
Postgres table by a fire-and-forget background task once the
**process-wide pending total** reaches `_USAGE_FLUSH_THRESHOLD` (default `10`,
defined in `src/mcp/middleware.py`).

This is a deliberate throughput vs. precision trade-off — the alternative
(flush per request) would add a DB round-trip to the hot path. Two
operational consequences:

- **Crash-drop risk.** If a worker process is killed (OOM, deploy restart,
  SIGKILL) with ≤9 buffered increments, those calls are lost. Across N
  worker processes the worst-case drop ceiling per crash is
  `N * (_USAGE_FLUSH_THRESHOLD - 1)`. Quota enforcement always reads from
  `usage_counter`, so a drop means the user gets slightly more calls than
  their plan grants — never less, never a billing over-charge.
- **Monitoring recipe.** Expose a Prometheus counter of *attempted* calls
  at the middleware layer and diff it against `SELECT sum(call_count) FROM
  usage_counter WHERE period_yyyymm = to_char(now() AT TIME ZONE 'UTC', 'YYYYMM')`.
  A persistent gap that doesn't close after the next flush indicates either
  a stuck flush task (check warning logs `usage_buffer flush error:`) or
  repeated worker crashes (check restart count).

Operators can verify a tenant's *actual* usage live via the customer
self-service portal at **`/account/usage`** — the page reads
`usage_counter` directly, so it reflects the last successful flush. Use
this to triage user-reported "quota looks wrong" tickets before reaching
for psql.

---

## Operator handover

When handing the system to a new operator, walk them through these
read-only verification surfaces in addition to the Actions above:

- **`/account/usage`** — per-user quota dashboard reading
  `usage_counter` directly (also used to verify buffer flushes — see the
  "Best-effort usage counter" section above).
- **`/admin/audit-log`** — admin-side audit viewer for mutating routes.
- **`<HEALTH_URL>/health`** — JSON health: Postgres pool status, Neo4j
  reachability, embedding service.

---

---

## Plan changes (M10B P0-ext)

Admin tooling now exposes plan upgrade + per-key overrides + reactivate via web UI.

> Reference: [ADR-0041](../../adr/0041-unlimited-plan-and-key-overrides.md) — unlimited plan +
> per-key override decisions (D1-D5). Migration m13_009 required before running these workflows.

### Set plan for one key

Admin → `/admin/api-keys` → pick plan in dropdown for the target row → Save.

Backend: `PATCH /api/admin/api-keys/{key_id}/plan` with body `{"plan_id": "<slug>"}`.

Side effect: `_cache_invalidate_by_key_id` called automatically in the worker that handles
the PATCH. New plan takes effect on the **next request in that worker**. Other workers converge
after `_CACHE_TTL` (300s) — see §Cache invalidation sanity below.

### Set per-key overrides

Admin → `/admin/api-keys` → click "Overrides..." button for the target row → set
`rate_limit_override` (RPM) and/or `quota_override` (monthly calls) → Save.

Both fields are nullable integers with `CHECK >= 0`:
- `NULL` = use the plan default.
- `0` = **zero is the limit** (blocks all calls). NOT unlimited.
- Unlimited = assign the plan with `slug='unlimited'` (ADR-0041 D5 SSOT).

### Cascade upgrade — set plan for all keys of a user

Admin → `/admin/users` → find the user row → pick new plan in the plan dropdown → click
"Apply to all keys".

Backend: `PATCH /api/admin/users/{user_id}/plan`. Cascades to ALL keys of that user (active +
inactive). Produces one audit log entry: `user.set_plan_cascade`.

### Reactivate API key

Admin path: Admin → `/admin/api-keys` (inactive keys table) → click Reactivate.
Owner path: user → `/account/api-keys` (inactive keys list) → click Reactivate.

Backend: `POST /api/api-keys/{key_id}/reactivate`.
- Admin: unconditional (any key).
- Non-admin owner: can only reactivate their own keys.

Audit action: `api_key.reactivate`.

### Cache invalidation sanity

After any `PATCH /api/admin/api-keys/{id}/plan` or `PATCH` that touches overrides:

- **Immediate** on the worker that handled the request: `_cache_invalidate_by_key_id` clears the
  in-memory LRU entry.
- **Other workers** (gunicorn/uvicorn multi-process): converge after `_CACHE_TTL = 300s` (5 min).
- **To force immediate cross-worker convergence**: gracefully restart workers
  (`systemctl reload odoo-semantic-mcp` or `kill -HUP <pid>`) — this drains in-flight requests
  and starts fresh workers with empty caches.
- There is no shared cache bus (Redis / PG-NOTIFY) at this time; the 300s eventual-consistency
  window is the accepted trade-off (ADR-0041 §Trade-offs).

### Audit log sanity

Verify after any admin plan operation:

```bash
psql -d $PG_DSN -c "
  SELECT action, target, detail, created_at
  FROM admin_audit_log
  ORDER BY id DESC LIMIT 5;"
```

Note: `target` (TEXT) holds the str-cast of the acted-upon resource id, as written by
`@audit_action`. `target_id` (INTEGER) is a legacy column that was dropped in migration
`m9_010_drop_audit_legacy_columns.sql` — it no longer exists. `detail` (JSONB, singular) carries
structured forensic context; the old `details` name was never a real column.

Expect rows with `action` in:
- `api_key.set_plan` — single-key plan or override change
- `user.set_plan_cascade` — cascade to all user keys
- `api_key.reactivate` — key reactivation

If a row is missing after the UI action, check that the `@audit_action` decorator is firing
(confirm no exception swallowed the request) and that the admin audit log table is reachable
(`psql -c "\dt admin_audit_log"`).

---

## References

- **Entry Point:** `src/indexer/__main__.py` — CLI subcommands (`index-repo`, `reembed-stubs`)
- **Index Definitions:** `src/indexer/writer_neo4j.py:790` — `setup_indexes()` method
- **Parser Logic:** `src/indexer/parser_python.py` — era1 `comodel_name` extraction (WI-C C2 commit)
- **Usage Buffer:** `src/mcp/middleware.py` — `_usage_buffer`, `_USAGE_FLUSH_THRESHOLD`, `_flush_usage_buffer_async`
- **Customer Portal:** `site/src/pages/account/usage.astro` — live usage dashboard reading `usage_counter`
- **Plan Cache:** `src/mcp/middleware.py` — `_PLAN_CACHE`, `_CACHE_TTL`, `_cache_invalidate_by_key_id`
- **TASKS.md:** Milestone 10 (M10A/M10.5/M10C/M10B) for more context on timing and interdependencies
- **ADR-0023:** MCP tool output completeness policy
- **ADR-0027:** System user deployment layout + portable paths
- **ADR-0039:** M10B commercialization platform (control plane / data plane)
- **ADR-0041:** Unlimited plan + per-key quota/rpm overrides (M10B P0-ext)
