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

### Neo4j writer

| Env var | Default | Was | File:line | Reasoning |
|---------|---------|-----|-----------|-----------|
| `NEO4J_WRITE_BATCH_SIZE` | 500 | 500 | `src/constants.py` | 500 rows/tx is the proven sweet spot. Decrease if Neo4j GC pauses cause transaction timeouts on underpowered hardware. |

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
