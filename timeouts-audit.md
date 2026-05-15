# Timeout Audit — v17+ Heavy Reindex Readiness

Audit date: 2026-05-15  
Branch: `wt-timeouts`  
Auditor: P2D Sonnet

## Summary

- **12 timeout/limit constants** reviewed across all 12 audit areas.
- **6 values raised** (5 in `src/constants.py`, 1 in `src/web_ui/routes/operations.py`).
- **6 new env vars** added for runtime tunability without code change.
- **2 systemd templates** updated with `TimeoutStartSec`/`TimeoutStopSec`.
- **1 nginx config** updated (`/api/` proxy_read_timeout: 60s → 300s).
- **1 new test file**: `tests/test_timeout_defaults.py` (9 unit tests, no Docker).
- **1 existing test** updated: `test_subprocess_run_timeout_60` → `test_subprocess_run_timeout_120`.

---

## Audit Table

| # | Area | File:line | Constant / setting | Old value | New value | Env var | Action | Reasoning |
|---|------|-----------|-------------------|-----------|-----------|---------|--------|-----------|
| 1 | Embedder Ollama | `src/constants.py:113` | `TIMEOUT_EMBEDDER_REQUEST` | 600s | **1200s** | `EMBEDDER_TIMEOUT` | RAISED + env-configurable | CPU-only Ollama with busy queue can exceed 90s/batch. 600s was only 6.7× headroom; 1200s is 13×. |
| 2 | Embedder batch | `src/constants.py:87` | `EMBEDDER_MAX_BATCH` | 50 | 50 (unchanged) | `EMBEDDER_MAX_BATCH` | env-configurable only | 50 items/batch is safe (~22s on fast hw). Kept default, added env override. |
| 3 | HTTP/requests | `src/web_ui/routes/operations.py:193` | `apply-preset timeout=` | 60s | **120s** | `APPLY_PRESET_TIMEOUT` | RAISED + env-configurable | apply-preset synchronously registers repos in PG; large profiles need >60s. |
| 4 | psycopg2 / SQL | (no `statement_timeout` in codebase) | — | n/a | n/a | — | NO CHANGE | Indexer writes are bounded by batch size + advisory lock. SQL timeouts belong in postgresql.conf, not app code. |
| 5 | Neo4j driver | `src/indexer/writer_neo4j.py:497` | `GraphDatabase.driver()` (default driver config) | defaults | defaults | — | NO CHANGE | Neo4j Python driver defaults: `connection_acquisition_timeout=60s`, `max_transaction_retry_time=30s`. These are acceptable for 500-row batches. GC pauses are addressed via `NEO4J_WRITE_BATCH_SIZE`. |
| 6 | Neo4j batch | `src/constants.py:81` | `NEO4J_WRITE_BATCH_SIZE` | 500 | 500 (unchanged) | `NEO4J_WRITE_BATCH_SIZE` | env-configurable only | 500/tx is proven. Added env override so ops can reduce to 250 if GC pauses cause tx timeouts. |
| 7 | Indexer pipeline | `src/indexer/pipeline.py` | no watchdog timer | — | — | — | NO CHANGE | No watchdog exists. Indexer runs as a detached subprocess (`spawn_indexer_subcommand`); the OS never kills it. systemd TimeoutStopSec=30 only affects SIGTERM on service stop. |
| 8 | Subprocess git clone | `src/constants.py:96` | `TIMEOUT_GIT_CLONE` | 600s | **3600s** | `TIMEOUT_GIT_CLONE` | RAISED + env-configurable | odoo/odoo v17+ has 1M+ commits. SSH clone on slow link reliably exceeds 600s. 3600s (1h) is safe. |
| 9 | git diff/rev-parse | `src/constants.py:101` | `TIMEOUT_GIT_DIFF` | 10s | **30s** | `TIMEOUT_GIT_DIFF` | RAISED + env-configurable | git diff on 600-module repo with slow NFS disk can exceed 10s. 30s is safe while still failing fast on hung git. |
| 10 | git scan | `src/constants.py:105` | `TIMEOUT_GIT_SCAN` | 10s | **30s** | `TIMEOUT_GIT_SCAN` | RAISED + env-configurable | Symmetric with TIMEOUT_GIT_DIFF. |
| 11 | SSH clone subprocess | `src/git_utils.py:79` | `clone_repo(timeout=TIMEOUT_GIT_CLONE)` | 600s | **3600s** (inherits #8) | `TIMEOUT_GIT_CLONE` | RAISED (via constant) | Directly uses TIMEOUT_GIT_CLONE constant — fixed by raising the constant. |
| 12 | Testcontainers | `tests/conftest.py` | `@wait_container_is_ready` | upstream | upstream | — | NO CHANGE | Per CLAUDE.md: upstream issue in testcontainers 4.x. Do not suppress or patch. |
| 13 | Systemd MCP | `docs/deploy/odoo-semantic-mcp.service` | `TimeoutStartSec` / `TimeoutStopSec` | absent | **60s / 30s** | — | ADDED | Missing timeouts mean systemd uses default 90s for start and 90s for stop. Explicit values document intent. |
| 14 | Systemd WebUI | `docs/deploy/odoo-semantic-webui.service` | `TimeoutStartSec` / `TimeoutStopSec` | absent | **60s / 30s** | — | ADDED | Same as above. |
| 15 | Systemd Astro | `docs/deploy/odoo-semantic-astro.service` | `TimeoutStartSec` / `TimeoutStopSec` | absent | **60s / 30s** | — | ADDED | Same as above. |
| 16 | FastMCP/uvicorn | `src/mcp/server.py:2427` | `timeout_graceful_shutdown=0` | 0 | 0 (unchanged) | — | NO CHANGE | Intentional: SSE streams must not be held open during shutdown. |
| 17 | CLI `--timeout` | `src/indexer/__main__.py` | no `--timeout` flag | absent | absent | — | NO CHANGE | Indexer has no per-job wall-clock timeout by design — it runs until done. Ops use `setsid nohup` for long runs. |
| 18 | Make targets | `Makefile` | no `pytest --timeout=` | absent | absent | — | NO CHANGE | No pytest-timeout configured; unit tests finish in <1s each. |
| 19 | nginx `/api/` | `docs/deploy/nginx-m8.conf:49` | `proxy_read_timeout` | 60s | **300s** | — | RAISED | apply-preset can take up to 120s (default) + server-side queue; 60s would prematurely cut it. |

---

## Files Changed

| File | Change |
|------|--------|
| `src/constants.py` | Added `import os`; raised + env-configured 6 constants |
| `src/web_ui/routes/operations.py` | Added `import os`; apply-preset timeout 60s → 120s + env-configurable |
| `docs/deploy/odoo-semantic-mcp.service` | Added `TimeoutStartSec=60`, `TimeoutStopSec=30` |
| `docs/deploy/odoo-semantic-webui.service` | Added `TimeoutStartSec=60`, `TimeoutStopSec=30` |
| `docs/deploy/odoo-semantic-astro.service` | Added `TimeoutStartSec=60`, `TimeoutStopSec=30` |
| `docs/deploy/nginx-m8.conf` | `/api/` proxy_read_timeout 60s → 300s |
| `.env.example` | Added 7 new env var stubs with comments |
| `docs/operations/timeouts.md` | NEW: full reference for all timeout knobs |
| `tests/test_timeout_defaults.py` | NEW: 9 unit tests (defaults + env-var overrides) |
| `tests/test_web_ui_apply_preset.py` | Updated `test_subprocess_run_timeout_60` → `test_subprocess_run_timeout_120` |
| `timeouts-audit.md` | NEW: this file |

---

## Not Changed (and Why)

- **No SQL `statement_timeout`** added: indexer write ops are bounded by batch size. Adding app-level SQL timeouts would mask legitimate slow queries instead of fixing them.
- **Neo4j driver connection config**: defaults (`connection_acquisition_timeout=60s`, `max_transaction_retry_time=30s`) are fine for 500-row batches. GC pauses are addressed via the `NEO4J_WRITE_BATCH_SIZE` env var.
- **No indexer watchdog**: the indexer runs as a detached subprocess; it runs until completion by design. `setsid nohup` is the ops pattern for long runs (see memory notes).
- **`timeout_graceful_shutdown=0`** in MCP server: intentional — SSE streams must close immediately on shutdown.
- **No pytest-timeout**: unit tests finish in <1s; adding a global timeout would create flaky CI if testcontainer startup is slow.
