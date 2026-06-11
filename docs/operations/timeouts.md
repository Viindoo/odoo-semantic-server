# Timeout & Performance Knobs

All timeout/batch constants are env-configurable. Set them in your `.env` or systemd `EnvironmentFile`.

## Why this matters

Odoo v17 alone has 605 modules and ~46k embeddings. v19+ is 30–50% larger. A full reindex of v8→v19 across all profiles takes many hours. Insufficient timeouts kill the run silently and lose progress.

**Boil-the-lake principle:** it's cheaper to set safe defaults now than discover a 4h reindex was killed at the 2h mark.

---

## Constants Reference

All defaults are defined in `src/constants.py` and read via `os.getenv()`.

### Git operations

| Env var | Default | Was | File:line | Reasoning |
|---------|---------|-----|-----------|-----------|
| `TIMEOUT_GIT_CLONE` | 3600s | 600s | `src/constants.py` | odoo/odoo v17+ has 1M+ commits; SSH clone on slow link takes 30+ min. 600s reliably kills it. |
| `TIMEOUT_GIT_DIFF` | 30s | 10s | `src/constants.py` | git diff on a 600-module repo with slow NFS/network disk can exceed 10s. |
| `TIMEOUT_GIT_SCAN` | 30s | 10s | `src/constants.py` | git rev-parse HEAD during scan; raised symmetrically with `TIMEOUT_GIT_DIFF`. |

### Embedder (Ollama)

| Env var | Default | Was | File:line | Reasoning |
|---------|---------|-----|-----------|-----------|
| `EMBEDDER_TIMEOUT` | 1200s | 600s | `src/constants.py` | A 50-text batch on qwen3-embedding-q5km takes ~22s on fast hardware, but can exceed 90s on CPU-only servers when the Ollama queue is busy (e.g., parallel profile workers). 1200s (20 min) gives ample headroom. |
| `EMBEDDER_MAX_BATCH` | 50 | 50 | `src/constants.py` | Unchanged — 50 keeps each batch under 30s on fast hardware, well within any reverse-proxy `proxy_read_timeout`. Increase only on fast local Ollama. |
| `EMBEDDER_RETRY_BACKOFF_BASE` | 2.0s | 2.0s | `src/constants.py` | Base delay for `Qwen3Embedder._embed_one` exponential backoff (delay = min(base * 2**i, max)). Lower on fast local Ollama (e.g. 0.5) to fail fast; raise on flaky LAN to avoid hammering. |
| `EMBEDDER_RETRY_BACKOFF_MAX` | 30.0s | 30.0s | `src/constants.py` | Cap on a single retry sleep so a slow Ollama box doesn't stall the indexer for minutes between attempts. Raise on chronically overloaded GPU hosts. |

### Neo4j writer

| Env var | Default | Was | File:line | Reasoning |
|---------|---------|-----|-----------|-----------|
| `NEO4J_WRITE_BATCH_SIZE` | 500 | 500 | `src/constants.py` | 500 rows/tx is the proven sweet spot. Decrease if Neo4j GC pauses cause transaction timeouts on underpowered hardware. |

### Neo4j ORM read (MCP tools — ADR-0048)

| Env var | Default | Was | File:line | Reasoning |
|---------|---------|-----|-----------|-----------|
| `NEO4J_QUERY_TIMEOUT_SECONDS` | 30 | (new) | `src/constants.py` | Per-query driver timeout for the 5 ORM read call-sites in `src/mcp/orm.py`. Wraps Cypher text via `neo4j.Query(text, timeout=...)`. On timeout, surfaces `OrmQueryTimeout` to the tool handler - English error, no Cypher leaked. 30s is safe for per-hop name-dedup queries (measured p99 < 1s on dense graphs); raise only if you observe false timeouts on extremely large profiles. **WARNING: must be > 0. The neo4j driver treats 0 as no-timeout, which silently reverts the #273 zombie-transaction fix. The server refuses to start (SystemExit) if this value is 0.** |
| `ORM_QUERY_MAX_CONCURRENCY` | 8 | (new) | `src/constants.py` | Semaphore cap on concurrent ORM tool executions (resolve_orm_chain / validate_domain / validate_depends / validate_relation). Uses a `threading.BoundedSemaphore` held by the worker thread (not the coroutine) - slot is released only when the thread exits, not when the client disconnects. Prevents pool-drain (#276 pattern). Mirrors `EMBEDDER_MAX_CONCURRENCY` pattern (ADR-0046). **WARNING: must be > 0. A value of 0 makes every ORM-validation tool fast-reject forever. The server refuses to start (SystemExit) if this value is 0.** |
| `ORM_SLOT_ACQUIRE_TIMEOUT` | 5 | (new) | `src/constants.py` | Fast-reject if an ORM semaphore slot is not available within N seconds. Caller receives `OrmOverloaded` structured error (plain string, never `isError=true`). **Must be strictly less than `NEO4J_QUERY_TIMEOUT_SECONDS`** (constraint enforced at startup - server refuses to start on violation). |

**Startup validation:** `_validate_orm_env()` is called once at `__main__` entry (after `init_dotenv`,
not at import-time to avoid breaking pytest). It performs three checks and calls `SystemExit(1)` if
any fail: `NEO4J_QUERY_TIMEOUT_SECONDS <= 0`, `ORM_QUERY_MAX_CONCURRENCY <= 0`,
`ORM_SLOT_ACQUIRE_TIMEOUT >= NEO4J_QUERY_TIMEOUT_SECONDS`. This ensures the core #273 fix cannot
be silently disabled by a mis-set env var.

**Non-ORM reads (accepted posture):** Approximately 84 `session.run` calls in `src/mcp/server.py`
(e.g., `impact_analysis` ~9 queries, `_resolve_model` ranking) do NOT have a per-query timeout.
This is accepted: all run in `@offload` worker threads (no event-loop wedge); `db.transaction.timeout=600s`
backstops all. A slow non-ORM traversal can pin a `asyncio.to_thread` pool thread up to 600s under
fan-out, but this is degraded throughput, not a #273-class zombie. Extending `_bounded()` to hot
non-ORM paths is a follow-up item (TASKS.md).

**Neo4j `db.transaction.timeout` — now wired into IaC (ADR-0048 D7 / issue #276):**

`NEO4J_db_transaction_timeout=600s` is set in `docker-compose.yml` (`services.neo4j.environment`)
and mirrored in `.github/workflows/nightly-smoke.yml` (all three Neo4j service containers). Any
`docker compose up` or `docker compose recreate` now applies the backstop automatically — it is no
longer a manual pre-deploy ops step.

**Why 600s, not 60s:** The 30s per-query driver timeout (above) handles ORM tool runaway.
The global `db.transaction.timeout` must accommodate legitimate long-running indexer transactions:
- `delete_modules_scoped` is now batched with `CALL {} IN TRANSACTIONS OF 10000 ROWS` (this PR), so
  each INNER batch stays well under 600s. BUT the OUTER coordinating transaction of
  `CALL IN TRANSACTIONS` is itself subject to `db.transaction.timeout` (verified on Neo4j 5.26.25 —
  see ADR-0048 D10 / M6): a very large repo delete whose TOTAL elapsed exceeds the timeout still has
  its outer tx terminated part-way (recoverable + idempotent, but surfaces an error in the Web UI).
- `gc_stale_modules` DETACH DELETEs module nodes in one transaction; can spike after large renames.
- `_write_parse_result` is one transaction per ParseResult with hundreds of sequential `tx.run`
  calls; 60s is not safe under concurrent load.

600s kills zombie ORM transactions (which ran 19-24h before this fix) while leaving indexer
headroom. For an exceptionally large repo delete or the one-off mesh-cleanup script, temporarily
raise/disable the timeout (`CALL dbms.setConfigValue('db.transaction.timeout','0')`, re-enable
after) rather than relying on the 600s ceiling.

**Bare-metal / systemd deployments (no Docker Compose):** apply dynamically and persist:

```cypher
CALL dbms.setConfigValue('db.transaction.timeout', '600s')
```

Then add `db.transaction.timeout=600s` to `neo4j.conf` so the value survives a service restart.

### Web UI operations

| Env var | Default | Was | File:line | Reasoning |
|---------|---------|-----|-----------|-----------|
| `APPLY_PRESET_TIMEOUT` | 120s | 60s | `src/web_ui/routes/operations.py` | apply-preset registers many repos in PostgreSQL; 120s handles large profiles without blocking the API server. |

---

## Recommended values for heavy-load reindex (v8→v19)

```env
# Reindex knobs — tune on the indexer host, not the MCP server host
TIMEOUT_GIT_CLONE=7200       # 2h — for very large repos or slow network
TIMEOUT_GIT_DIFF=60          # 60s — for repos on NFS or very slow disk
TIMEOUT_GIT_SCAN=60          # 60s — symmetric with TIMEOUT_GIT_DIFF
EMBEDDER_TIMEOUT=1800        # 30 min — when Ollama is CPU-only + queue is busy
NEO4J_WRITE_BATCH_SIZE=250   # 250/tx — if Neo4j GC pauses exceed 30s
```

---

## Systemd unit timeouts

The service templates in `docs/deploy/` include:

- `TimeoutStartSec=60` — services start quickly; 60s is generous.
- `TimeoutStopSec=30` — allow in-flight requests to drain before force-kill.

The indexer runs as a **detached subprocess** (spawned by the Web UI via `spawn_indexer_subcommand`), not as its own systemd unit. It is not subject to systemd timeouts — it runs until completion (or until the OS kills the PID).

If you run the indexer manually via a systemd one-shot unit, add:

```ini
[Service]
Type=oneshot
TimeoutStartSec=infinity   # no timeout for long reindex jobs
RemainAfterExit=no
```

---

## Nginx proxy timeouts

See `docs/deploy/nginx-m8.conf` for the full config. Key locations:

| Location | `proxy_read_timeout` | Reasoning |
|----------|---------------------|-----------|
| `/api/` | 300s | apply-preset (120s default) + server-side queue time |
| `/` (Astro) | 120s | SSR renders; raised from 60s in original nginx.conf.example |
| `/mcp` | 3600s | MCP SSE sessions can be long-lived |
| `/health` | 5s | Fast check; fail fast |

---

## No SQL `statement_timeout`

The indexer does not set `statement_timeout` on PostgreSQL connections. All write operations are bounded by Neo4j transaction size (`NEO4J_WRITE_BATCH_SIZE`) and the Postgres advisory lock is non-blocking (`pg_try_advisory_lock`). If you observe long-running Postgres queries (e.g., from the pgvector ANN search), set `statement_timeout` in your Postgres config (`postgresql.conf`), not in application code.
