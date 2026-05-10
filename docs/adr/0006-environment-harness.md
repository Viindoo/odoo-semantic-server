# ADR-0006 — Environment Harness (M6 Wave 1)

**Status:** Accepted (2026-05-10)
**Context:** M6 "Scale Wow" — single source of truth cho version pinning + Python 3.12 enforcement + concurrency hardening.

## Context

Trước M6 Wave 1, repo có hai dạng drift:

1. **Image version drift** — `.env.example:NEO4J_IMAGE=neo4j:5.26.25` declare là "nguồn sự thật" nhưng `docker-compose.yml:32` hardcode `pgvector/pgvector:0.8.2-pg16` (không có env var slot), và `.github/workflows/nightly-smoke.yml` có 4 image refs hardcode khác. Bump version một chỗ quên chỗ khác → CI green nhưng prod khác.

2. **Runtime version drift** — không check Neo4j/PostgreSQL/pgvector version lúc startup. User chạy MCP server trên Neo4j 4.x → query `COUNT { ... }` raise `CypherSyntaxError` (Neo4j 5.x syntax) — fail muộn, error obscure.

3. **Python 3.12 hygiene drift** — `pyproject.toml requires-python=">=3.12"` đã pinned nhưng codebase còn `try/except ImportError around psycopg2.extensions` (dead code), `datetime.now()` không tz cho `TIMESTAMPTZ` columns, ruff không enforce `UP` rules.

4. **Concurrency primitives** — singleton `_pg_conn` + global `_PG_LOCK` serialize TẤT CẢ PG access trong MCP (auth/log/health/find_examples). Indexer global `_LOCK_ID` chặn 2 profile khác nhau dù không chia sẻ data.

## Decision

### 1. Image versions: extend `.env.example` (not `[tool.versions]`)

`.env.example` declare cả `NEO4J_IMAGE` và `PG_IMAGE`. `docker-compose.yml` đọc qua `${NEO4J_IMAGE:-...}` / `${PG_IMAGE:-...}` slot pattern. CI workflow `ci.yml` correctly uses `docker compose up -d` (transparently inherits).

**Architectural exception:** `nightly-smoke.yml` (GitHub Actions service containers) parse-time cannot read `.env.example` — image refs MUST be hardcoded inside the workflow. Comment header documented + anti-drift test `tests/test_env_versions_sync.py` regex-asserts `nightly-smoke.yml` chứa cùng image strings như `.env.example`. Bump → both files updated → test passes.

**Rejected alternatives:**
- `[tool.versions]` trong `pyproject.toml`: non-standard, cần custom reader, cao friction so với extending `.env.example` pattern đã có.
- `versions.toml` riêng: thêm 1 file phải maintain, không có Python tooling tự đọc.

### 2. Runtime version checks: fail-fast at startup

- **Neo4j**: `src/mcp/server.py _get_driver()` chạy `CALL dbms.components()` lần đầu lifetime, parse major version, `RuntimeError` nếu < 5. Skip nếu `os.getenv("CI") == "true"` (CI service container đã pinned, tránh circular fail). Module-level `_version_checked` flag tránh re-query.
- **PostgreSQL**: `src/db/migrate.py run_migrations()` đầu function chạy `SELECT current_setting('server_version_num')::int`, fail nếu < 160000.
- **pgvector**: `_ensure_extension()` chạy `SELECT extversion FROM pg_extension WHERE extname='vector'`, parse semver, fail nếu major.minor < (0, 8).

Tất cả error message gợi ý "Update docker-compose.yml NEO4J_IMAGE/PG_IMAGE and re-run" để dẫn user về source of truth.

### 3. Python 3.12 enforcement

- **Ruff `UP` rules**: `pyproject.toml [tool.ruff.lint] select = ["E", "F", "I", "UP"]`. 13 existing UP violations auto-fixed (≤ 20 threshold): `Optional[X]` → `X | None`, `timezone.utc` → `UTC` alias, unused imports cleaned.
- **CONTRIBUTING.md "Python 3.12 Code Style" section**: cấm `from __future__ import annotations`, `typing.Dict/List/Optional/Union[]`, `sys.version_info` guards. Reference `requires-python = ">=3.12"`.
- **Audit fixes**: `src/cli.py` xoá dead `try/except ImportError`. `tests/test_web_ui_repos.py` `datetime.now() → datetime.now(tz=UTC)` ở 2 call sites (column là `TIMESTAMPTZ`).

### 4. PEP 695 type alias cho `conn`

`src/db/_types.py` mới định nghĩa:
```python
import psycopg2.extensions
type PgConn = psycopg2.extensions.connection
```

PEP 695 native trong 3.12 — không cần `from __future__`. 22 functions trong `src/db/{job,auth,repo}_registry.py` + `migrate.py` + `src/manager/__main__.py` annotate `conn: PgConn`.

### 5. Concurrency primitives refactor

- **Per-profile advisory lock**: `_LOCK_ID` global constant thay bằng `_profile_lock_id(profile_name: str) -> int` (hash `f"odoo-semantic-{profile_name}"` mod 2^31). `indexer_is_running()` nhận thêm `profile_name` param. Hai profile khác nhau index parallel không block.
- **PostgreSQL connection pool**: singleton `_pg_conn` + `_PG_LOCK` thay bằng `psycopg2.pool.SimpleConnectionPool(minconn=1, maxconn=10)` + `_checkout_pg()` context manager. `register_vector()` per-checkout (idempotent, an toàn). `_PG_LOCK` xoá hoàn toàn khỏi codebase. `_find_examples` / `_suggest_pattern` nay chạy concurrent, không serialize sau auth/log/health.
- **ThreadPoolExecutor parallel repo scan**: `index_profile()` thêm `max_workers: int = 1` (default = sequential, no behavior change). Khi > 1 wrap `_index_repo()` bằng `ThreadPoolExecutor` — mỗi worker tự `open_production_pg()` (psycopg2 connection per-thread requirement). Neo4jWriter share-safe (per-method session pattern audited).

## Consequences

### Positive
- Bump Neo4j/PG version: chỉ sửa `.env.example` + `nightly-smoke.yml` → anti-drift test catches mismatch.
- Wrong Neo4j/PG version: fail-fast error message rõ ràng, không debug obscure Cypher/SQL errors.
- 30 concurrent users: PG connection pool khử serialization point, MCP latency dưới 50ms ngay cả khi 5+ semantic search calls song song.
- Indexer 2 profile parallel: foundation cho M6 multi-version (Wave 2+).
- Codebase Python 3.12 hygiene enforced — code reviews không phải catch `Optional[X]` bằng tay.

### Negative
- User trên Neo4j 4.x / PG 15 cũ: deploy abort. Mitigation: error message clear, README + deploy guide note min versions.
- Pool max=10: nếu burst > 10 concurrent calls, 11th caller block hoặc raise `psycopg2.pool.PoolError`. Mitigation: monitor `pg_stat_activity` count; bump `maxconn` nếu cần.
- ThreadPoolExecutor `progress=True` không tương thích với `max_workers > 1` (tqdm bars stomp). Mitigation: tự động force `progress=False` khi parallel.

### Neutral
- `nightly-smoke.yml` vẫn cần manual sync khi bump — architectural constraint của GitHub Actions service containers (parse-time before steps run). Không workaround được; document + test guard là approach đúng.

## Out of Scope (Wave 2+)

- Auto-clone qua SSH khi user add repo (M6 main feature)
- Incremental indexer (`src/indexer/incremental.py`)
- Multi-version preset (`version_presets.py`) + cross-version pattern diff
- EE_CONFUSION auto-detect + `viindoo_equivalent_qname` graph traversal
- Pattern catalogue auto-reseed + 50 → 200 expansion
