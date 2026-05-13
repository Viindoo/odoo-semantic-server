# Plan: Milestone 5 — "Product Wow" (rev 2 — post-Opus debate)

> **Status:** ✓ DONE — M5 shipped 2026-05-09

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans.
>
> **Bắt buộc khi implement:**
> - **Boil the Lake (ETHOS §4.1.1):** Test phải cover 100% path, không shortcut.
> - **Keep it simple (ETHOS §4.1.3):** Minimum code. Không thêm abstraction không được yêu cầu.
> - **Think Before Coding (ETHOS §4.1.2):** State assumptions. Surface tradeoffs. Hỏi nếu unclear.

## Context

M1–M4.6 đã hoàn thành: 14 MCP tools. M5 biến project thành "bất kỳ ai deploy được trong < 10 phút".
Plan này là **rev 2** sau debate Opus (2026-05-09): 3 BLOCKER đã fix, scope đã trim.

**Deferred ra khỏi M5:**
- Auto-clone qua SSH khi add repo → **M6** (feature, không phải hardening)
- CLI backup/restore → **M5.5** (document manual procedure thay)
- Pattern feedback loop (`POST /api/feedback`) → **M5.5** (feature M4.6 defer, không block M5)

---

## Tech Decisions (rev 2)

| Quyết định | Chọn | Lý do |
|---|---|---|
| Auth | API key via `X-API-Key` header (SHA-256 hash, no expiry M5) | Đơn giản, khớp MCP client config |
| Auth bypass | **KHÔNG có** `AUTH_DISABLED` env var | Opus: footgun silent bypass prod. Dev bind `127.0.0.1` là đủ |
| Web UI | FastAPI + Jinja2, port 8003 (tách MCP :8002) | Đã có FastAPI qua FastMCP; Jinja2 nhẹ |
| Web UI bind | Hard-code `127.0.0.1` | No Web UI auth M5 → must not expose publicly |
| Web UI auth | Không có M5 (local admin, same-server) | Defer M6 |
| Indexer lock | **PostgreSQL advisory lock** (`pg_try_advisory_lock`) | Opus: fcntl không protect async tasks same-process; Postgres đã có rồi |
| SSH key | Ed25519 qua `cryptography` | No new dep class |
| SSH private key storage | Fernet-encrypted + `key_version` column | Rotation story: backup FERNET_KEY separate; re-encrypt script khi rotate |
| FastMCP middleware | Starlette `BaseHTTPMiddleware` trong `src/mcp/middleware.py` | Tách khỏi server.py để parallel WIs không conflict |
| Auth middleware DB | **LRU cache** (5 min) + `asyncio.create_task` log | Opus BLOCKER: sync psycopg2 trong async handler block event loop |
| `mcp_tools` trong health | **FastMCP introspection** (không hardcode 14) | Opus: drift ngay khi add tool mới |
| SSH tmpfile | **`tempfile.mkstemp(mode=0o600)`** | Opus: race window giữa create và chmod |
| SSH clone | Defer M6 | Opus: feature, không phải hardening |
| `web_ui_url` footnote | **BỎ** khỏi `_suggest_pattern` output | Opus: wrong layer — MCP tool không nên biết về Web UI URL |
| FERNET_KEY validation | Fail-fast on startup nếu không set + `key_version` per row | Rotation safety |
| JWT / session | KHÔNG dùng M5 | Over-engineering |

---

## New Dependencies

```toml
"jinja2>=3.1,<4.0"          # Web UI templates
"python-multipart>=0.0.9"   # FastAPI form parsing
"cryptography>=42"           # Ed25519 SSH keygen (explicit; transitive via authlib)
```
Dev deps: `"httpx>=0.27"` (FastAPI TestClient).
**Removed:** `tqdm` → defer M5.5 (WI-A2 của M5.5 plan đã có sẵn).

---

## Worktree Strategy (rev 2 — 13 WIs, 5 waves)

```
trunk (master)
  │
  ├── WAVE 1 ── (6 WIs song song, toàn Haiku)
  │   ├── feat/m5-adr            WI-ADR: ADR-0004
  │   ├── feat/m5-db             WI-DB: migrations + auth_registry + add-api-key CLI
  │   ├── feat/m5-lock           WI-LOCK: Postgres advisory lock
  │   ├── feat/m5-server-refactor  WI-SERVER-REFACTOR: split server.py → health.py + middleware.py stub + health test
  │   ├── feat/m5-deps           WI-DEPS: pyproject + docker + Dockerfile + .env.example
  │   └── feat/m5-install        WI-INSTALL: install.sh + systemd templates
  │
  ├── CONSOLIDATE Wave 1 → trunk → make test-all
  │
  ├── WAVE 2 ── (3 WIs song song)
  │   ├── feat/m5-auth           WI-AUTH: src/auth.py + src/mcp/middleware.py (Sonnet)
  │   ├── feat/m5-web-scaffold   WI-WEB-SCAFFOLD: web_ui skeleton + dashboard (Sonnet)
  │   └── feat/m5-tests-health   WI-TESTS-HEALTH: tests/test_health_endpoint.py (Haiku)
  │
  ├── CONSOLIDATE Wave 2 → trunk → make test-all
  │
  ├── WAVE 3 ── (3 WIs song song)
  │   ├── feat/m5-web-repos      WI-WEB-REPOS: repos management UI (Sonnet)
  │   ├── feat/m5-web-keys       WI-WEB-KEYS: API keys UI (Haiku)
  │   └── feat/m5-web-ssh        WI-WEB-SSH: SSH keygen UI — store only, no auto-clone (Sonnet)
  │
  ├── CONSOLIDATE Wave 3 → trunk → make test-all
  │
  ├── WAVE 4 ── (2 WIs song song, Haiku)
  │   ├── feat/m5-tests-int      WI-TESTS-INT: cross-component integration tests
  │   └── feat/m5-smoke          WI-SMOKE: smoke tests + ci.yml update
  │
  ├── CONSOLIDATE Wave 4 → trunk → make test-all
  │
  ├── WAVE 5 ── (2 WIs song song, Haiku)
  │   ├── feat/m5-docs-readme    WI-DOCS-README: README + CONTRIBUTING
  │   └── feat/m5-docs-deploy    WI-DOCS-DEPLOY: deploy.md + TASKS.md + conf.example
  │
  └── CONSOLIDATE Wave 5 → trunk → FINAL make test-all → M5 DONE
```

**Conflict-free guarantee:** Mỗi WI trong cùng wave touch file riêng biệt. `server.py` chỉ bị WI-SERVER-REFACTOR (Wave 1) split và WI-AUTH (Wave 2) chỉ touch `middleware.py` + `auth.py` (files mới). Wave 3 Web UI WIs chỉ tạo file mới trong `src/web_ui/routes/` + `templates/`, không đụng `app.py` (đã scaffold đủ ở Wave 2).

---

## WAVE 1 — Foundation (6 WIs, toàn Haiku)

---

### WI-ADR — ADR-0004

**Branch:** `feat/m5-adr` | **Model:** Haiku | **~15 min**

**Files touched:** `docs/adr/0004-auth-web-ui-ssh-policy.md` (NEW)

**Task:** Tạo ADR theo format `docs/adr/0001-schema-evolution-policy.md`. Record lại 10 quyết định kiến trúc M5 rev 2 (bảng Tech Decisions trên) + 3 items deferred + rationale từ Opus debate.

**AC:** File có Status: Accepted, 10 quyết định, rationale ngắn gọn. `make lint` green.

---

### WI-DB — DB Migrations + CRUD + create-api-key CLI

**Branch:** `feat/m5-db` | **Model:** Haiku | **~35 min**

**Files touched:**
- MOD: `src/db/migrate.py`
- NEW: `src/db/auth_registry.py`
- MOD: `src/manager/__main__.py` (add `create-api-key` subcommand)
- NEW: `tests/test_db_auth_registry.py` (marker: `postgres`)

**Task A — `src/db/migrate.py`:** Thêm `_AUTH_SQL` sau `_EMBEDDINGS_SQL`:

```sql
CREATE TABLE IF NOT EXISTS api_keys (
    id           SERIAL PRIMARY KEY,
    name         TEXT NOT NULL,
    key_hash     TEXT UNIQUE NOT NULL,   -- SHA-256 hex của raw key
    key_prefix   TEXT NOT NULL,          -- 8 ký tự đầu để hiển thị
    active       BOOLEAN DEFAULT TRUE,
    created_at   TIMESTAMP DEFAULT NOW(),
    last_used_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ssh_key_pairs (
    id                    SERIAL PRIMARY KEY,
    name                  TEXT NOT NULL,
    public_key            TEXT NOT NULL,
    private_key_encrypted TEXT NOT NULL,  -- Fernet-encrypted base64
    key_version           INTEGER NOT NULL DEFAULT 1,  -- rotation tracking
    created_at            TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS usage_log (
    id           BIGSERIAL PRIMARY KEY,
    api_key_id   INTEGER REFERENCES api_keys(id) ON DELETE SET NULL,
    tool_name    TEXT NOT NULL,
    called_at    TIMESTAMP DEFAULT NOW(),
    response_ms  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_usage_log_api_key  ON usage_log(api_key_id);
CREATE INDEX IF NOT EXISTS idx_usage_log_called_at ON usage_log(called_at);
```

Gọi trong `run_migrations()`. Update `SCHEMA_SQL` alias.

**Task B — `src/db/auth_registry.py`:**
```python
def create_api_key(conn, name: str) -> tuple[str, str, int]:
    """Return (raw_key, key_prefix, id). raw_key shown once."""

def verify_api_key(conn, raw_key: str) -> int | None:
    """Return api_key_id if active + valid. Update last_used_at."""

def list_api_keys(conn) -> list[dict]:
def deactivate_api_key(conn, key_id: int) -> None:

def log_usage(conn, api_key_id: int | None, tool_name: str, response_ms: int) -> None:
    # Fire-and-forget. Caller wraps in asyncio.create_task where needed.

def list_ssh_keys(conn) -> list[dict]:
def save_ssh_key(conn, name: str, public_key: str, private_key_encrypted: str, key_version: int = 1) -> int:
```

**Task C — `src/manager/__main__.py`:** Thêm subcommand `create-api-key`:
```
python -m src.manager create-api-key <name>
→ Prints: "API key: osm_<raw>" (prefix osm_ để distinguish)
           "Key ID: 3"
           "WARNING: This key will not be shown again."
```

**Task D — `tests/test_db_auth_registry.py`:**
- `pytestmark = pytest.mark.postgres`
- create → verify correct → verify wrong (returns None) → verify inactive (returns None) → deactivate → list → log_usage no-op OK

**AC:** `pytest tests/test_db_auth_registry.py -m postgres` green. `python -m src.manager create-api-key test` prints key. `make lint` green.

---

### WI-LOCK — Postgres Advisory Lock

**Branch:** `feat/m5-lock` | **Model:** Haiku | **~20 min**

**Files touched:**
- MOD: `src/indexer/pipeline.py`
- NEW: `tests/test_indexer_lock.py` (marker: `postgres`)

**Task — `src/indexer/pipeline.py`:**

```python
import hashlib
from contextlib import contextmanager

_LOCK_ID = int(hashlib.md5(b"odoo-semantic-indexer").hexdigest(), 16) % (2**31)

@contextmanager
def _indexer_lock(pg_conn, profile_name: str):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_ID,))
        acquired = cur.fetchone()[0]
    if not acquired:
        raise RuntimeError(
            f"Indexer already running (Postgres advisory lock {_LOCK_ID} held). "
            "Wait for it to finish. Lock auto-releases on process exit."
        )
    try:
        yield
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID,))
```

Truyền `pg_conn` vào `index_profile()` (đã có sẵn trong signature). Wrap với `_indexer_lock(pg_conn, profile_name)`.

**Advantages over fcntl:** auto-release on process crash/restart, cross-container, no path issues in Docker, async-safe (Postgres level, không phải Python thread).

**Task — `tests/test_indexer_lock.py`:**
- `pytestmark = pytest.mark.postgres`
- Test 1: acquire → yield OK → release
- Test 2: acquire in test, then call index_profile → raises RuntimeError
- Test 3: lock releases even if exception inside context

**AC:** `pytest tests/test_indexer_lock.py -m postgres` green. `make lint` green.

---

### WI-SERVER-REFACTOR — Split server.py + Health Endpoint + Test

**Branch:** `feat/m5-server-refactor` | **Model:** Haiku | **~35 min**

**Files touched:**
- MOD: `src/mcp/server.py` (extract health + create middleware stub)
- NEW: `src/mcp/health.py`
- NEW: `src/mcp/middleware.py` (stub only — Wave 2 WI-AUTH fills it)
- NEW: `tests/test_health_endpoint.py` (marker: `neo4j + postgres`)

**Mục đích:** Tách server.py trước Wave 2/3 để không có merge conflict khi WI-AUTH (Wave 2) chỉ cần sửa `middleware.py`, và WI-WEB-FEEDBACK (M5.5) chỉ cần sửa `_suggest_pattern` trong `server.py`.

**Task A — `src/mcp/health.py`:**
```python
import importlib.metadata
from starlette.requests import Request
from starlette.responses import JSONResponse

async def health_handler(request: Request) -> JSONResponse:
    from src.mcp.server import _get_driver, _get_pg_conn

    neo4j_status = "ok"
    try:
        _get_driver().verify_connectivity()
    except Exception as e:
        neo4j_status = f"error:{str(e)[:100]}"

    pg_status = "ok"
    try:
        conn = _get_pg_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception as e:
        pg_status = f"error:{str(e)[:100]}"

    # Introspect tool count via FastMCP
    from src.mcp.server import mcp
    try:
        tool_count = len(mcp._tool_manager._tools)  # FastMCP v2 internal
    except Exception:
        tool_count = -1  # unknown, không hardcode

    both_ok = neo4j_status == "ok" and pg_status == "ok"
    one_ok = neo4j_status == "ok" or pg_status == "ok"
    status = "ok" if both_ok else ("degraded" if one_ok else "error")
    http_code = 503 if status == "error" else 200

    body = {
        "status": status,
        "neo4j": neo4j_status,
        "postgres": pg_status,
        "version": importlib.metadata.version("odoo-semantic-mcp"),
        "mcp_tools": tool_count,
    }
    return JSONResponse(body, status_code=http_code)
```

**Task B — `src/mcp/middleware.py`:** Stub chỉ có comment + empty class placeholder:
```python
"""Auth middleware — implemented by WI-AUTH (Wave 2)."""
# Placeholder for BaseHTTPMiddleware to be added by WI-AUTH
```

**Task C — `src/mcp/server.py`:** Đăng ký health route với FastMCP:
```python
# Approach A (preferred — verify FastMCP v2.3+ supports):
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    from src.mcp.health import health_handler
    return await health_handler(request)

# Approach B fallback nếu FastMCP không hỗ trợ custom_route:
# Trong __main__ hoặc server startup, access mcp._app và mount route
```

**Task D — `tests/test_health_endpoint.py`:**
```python
pytestmark = [pytest.mark.neo4j, pytest.mark.postgres]

class TestHealthEndpoint:
    def test_returns_required_keys(self, ...):
        # GET /health → 200, body has: status, neo4j, postgres, version, mcp_tools

    def test_neo4j_down_returns_degraded_or_error(self, monkeypatch, ...):
        # Mock _get_driver().verify_connectivity() to raise
        # → status != "ok", neo4j starts with "error:"

    def test_postgres_down_returns_degraded(self, monkeypatch, ...):
        # Mock _get_pg_conn() to raise
        # → status != "ok", postgres starts with "error:"

    def test_both_ok_returns_200(self, ...):
        # Normal state → status == "ok", HTTP 200

    def test_mcp_tools_count_is_positive_int(self, ...):
        # mcp_tools > 0 (at least 1 tool registered)
        # NOT assertEqual(14) — no hardcode
```

**AC:** `pytest tests/test_health_endpoint.py -m "neo4j and postgres"` green. `make lint` green.

---

### WI-DEPS — Dependencies + Docker Hardening

**Branch:** `feat/m5-deps` | **Model:** Haiku | **~20 min**

**Files touched:**
- MOD: `pyproject.toml`
- MOD: `docker-compose.yml`
- NEW: `Dockerfile`
- MOD: `.env.example`

**Task A — `pyproject.toml`:**
```toml
"jinja2>=3.1,<4.0",
"python-multipart>=0.0.9",
"cryptography>=42",
```
Dev: `"httpx>=0.27"`.

**Task B — `docker-compose.yml`:** Named volumes + `restart: unless-stopped` mỗi service.

**Task C — `Dockerfile`:**
```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y postgresql-client git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .
COPY src/ src/
EXPOSE 8002 8003
CMD ["python", "-m", "src.mcp.server"]
```
Note: `postgresql-client` included → `pg_dump` available for manual backup.

**Task D — `.env.example`:**
```
FERNET_KEY=     # generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
                # REQUIRED for SSH key storage. Back up separately from DB backups.
```
Remove `AUTH_DISABLED` (không còn dùng).

**AC:** `make install` OK. `docker compose config` valid. `make lint` green.

---

### WI-INSTALL — install.sh + Systemd Templates

**Branch:** `feat/m5-install` | **Model:** Haiku | **~25 min**

**Files touched:**
- NEW: `install.sh`
- NEW: `systemd/odoo-semantic-mcp.service.template`  <!-- [Note: PR #45 moved templates to docs/deploy/ as canonical .service files] -->
- NEW: `systemd/odoo-semantic-webui.service.template`  <!-- [Note: PR #45 moved templates to docs/deploy/ as canonical .service files] -->

**Task:** Bash script:
1. Check python3 >= 3.12
2. Create `~/.venv/odoo-semantic-mcp` nếu chưa có
3. `pip install -e ".[dev]"`
4. `mkdir -p ~/.odoo-semantic`
5. Copy configs nếu chưa có
6. Print: "Generate FERNET_KEY: python -c '...'"
7. `--systemd` flag → copy templates + print enable instructions

Systemd template dùng `EnvironmentFile=%h/.odoo-semantic/env` (chứa FERNET_KEY, NEO4J_PASSWORD, etc.).

**AC:** `bash install.sh --help` thoát 0. `make lint` green.

---

## Consolidate Wave 1 → trunk

```bash
git merge feat/m5-adr feat/m5-db feat/m5-lock feat/m5-server-refactor feat/m5-deps feat/m5-install
make test-all   # MUST green
```

---

## WAVE 2 — Backend Core (3 WIs song song)

---

### WI-AUTH — Auth Layer

**Branch:** `feat/m5-auth` | **Model:** Sonnet | **~40 min**

**Files touched:**
- NEW: `src/auth.py`
- MOD: `src/mcp/middleware.py` (fill stub từ Wave 1)
- NEW: `tests/test_auth.py` (no marker)

**Depends on:** WI-DB merged (auth_registry available), WI-SERVER-REFACTOR merged (middleware.py stub exists)

**Task A — `src/auth.py`:**
```python
import hashlib, secrets, functools
from typing import Optional

def generate_api_key() -> tuple[str, str]:
    raw = "osm_" + secrets.token_urlsafe(32)
    return raw, hash_key(raw)

def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()

@functools.lru_cache(maxsize=256)
def _cached_hash(raw: str) -> str:
    return hash_key(raw)
```

**Task B — `src/mcp/middleware.py`:** Implement auth middleware:

```python
import asyncio, time
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_KEY_CACHE: dict[str, int | None] = {}  # raw_key → api_key_id, TTL 5 min
_CACHE_TS: dict[str, float] = {}
_CACHE_TTL = 300.0

def _cache_get(raw_key: str) -> tuple[bool, int | None]:
    if raw_key in _CACHE_TS and time.monotonic() - _CACHE_TS[raw_key] < _CACHE_TTL:
        return True, _KEY_CACHE[raw_key]
    return False, None

def _cache_set(raw_key: str, key_id: int | None):
    _KEY_CACHE[raw_key] = key_id
    _CACHE_TS[raw_key] = time.monotonic()

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        # Public paths
        if request.url.path in ("/health",):
            return await call_next(request)

        raw_key = request.headers.get("X-API-Key")
        if not raw_key:
            return Response("Missing X-API-Key header", status_code=401)

        # Check cache first (avoid DB per request)
        hit, key_id = _cache_get(raw_key)
        if not hit:
            from src.mcp.server import _get_pg_conn
            from src.db.auth_registry import verify_api_key
            conn = _get_pg_conn()
            key_id = await asyncio.to_thread(verify_api_key, conn, raw_key)
            _cache_set(raw_key, key_id)

        if key_id is None:
            return Response("Invalid or inactive API key", status_code=401)

        request.state.api_key_id = key_id
        start = time.monotonic()
        response = await call_next(request)
        ms = int((time.monotonic() - start) * 1000)

        # Fire-and-forget usage log (không block response)
        asyncio.create_task(_log_usage(key_id, request, ms))
        return response

async def _log_usage(key_id: int, request: Request, ms: int):
    try:
        from src.mcp.server import _get_pg_conn
        from src.db.auth_registry import log_usage
        tool = request.headers.get("X-Tool-Name", "unknown")
        conn = _get_pg_conn()
        await asyncio.to_thread(log_usage, conn, key_id, tool, ms)
    except Exception:
        pass  # best-effort, không raise
```

**Cách attach middleware:** Trong `src/mcp/server.py` startup (hoặc `__main__.py`):
```python
from src.mcp.middleware import AuthMiddleware
# After getting ASGI app from FastMCP:
app = mcp.streamable_http_app()
app.add_middleware(AuthMiddleware)
uvicorn.run(app, host=host, port=port)
```

**Task C — `tests/test_auth.py`:**
- `generate_api_key()` → prefix "osm_", tuple of str
- `hash_key(raw)` deterministic, SHA-256
- `_cache_get/set` TTL behavior
- `AuthMiddleware` với mock: no key → 401; valid key → 200; invalid key → 401; `/health` path → bypass

**AC:** `pytest tests/test_auth.py` green. `make lint` green.

---

### WI-WEB-SCAFFOLD — Web UI App Skeleton

**Branch:** `feat/m5-web-scaffold` | **Model:** Sonnet | **~35 min**

**Files touched:**
- NEW: `src/web_ui/__init__.py`
- NEW: `src/web_ui/app.py`
- NEW: `src/web_ui/__main__.py`
- NEW: `src/web_ui/routes/__init__.py`
- NEW: `src/web_ui/routes/dashboard.py`
- NEW: `src/web_ui/templates/base.html`
- NEW: `src/web_ui/templates/dashboard.html`

**Depends on:** WI-DB merged, WI-DEPS merged (jinja2 installed)

**Key design:** `app.py` scaffold đầy đủ nav links (Dashboard / Repos / API Keys / SSH Keys) ngay trong Wave 2. Wave 3 WIs chỉ tạo route file + template mới — KHÔNG sửa `app.py` hay `base.html`.

**`src/web_ui/app.py`:**
```python
def create_app() -> FastAPI:
    app = FastAPI(...)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.templates = templates

    from src.web_ui.routes import dashboard
    app.include_router(dashboard.router)
    # from src.web_ui.routes import repos; app.include_router(repos.router)
    # from src.web_ui.routes import api_keys; app.include_router(api_keys.router)
    # from src.web_ui.routes import ssh_keys; app.include_router(ssh_keys.router)
    return app
```

**IMPORTANT:** 3 import lines dạng comment bên trên phải có sẵn trong Wave 2.
Wave 3 WIs chỉ uncomment dòng tương ứng — không có merge conflict.

`__main__.py` bind: `host = "127.0.0.1"` (hard-code, không config — per security decision).

**`base.html`** nav: Dashboard | Repos & Profiles | API Keys | SSH Keys (links cho cả 4 trang, Wave 3 trang sẽ 404 cho đến khi route land).

**AC:** `python -m src.web_ui` → port 8003, `GET /` → HTML với navbar. `make lint` green.

---

### WI-TESTS-HEALTH — Health Endpoint Tests

**Branch:** `feat/m5-tests-health` | **Model:** Haiku | **~20 min**

**Files touched:**
- NEW: `tests/test_health_endpoint.py` (marker: `neo4j + postgres`)

**Depends on:** WI-SERVER-REFACTOR merged (health.py + /health route exists)

**Note:** WI-SERVER-REFACTOR đã tạo file này trong Wave 1. WI-TESTS-HEALTH là backup nếu WI-SERVER-REFACTOR quên viết test. Nếu file đã tồn tại từ Wave 1 → WI-TESTS-HEALTH chỉ verify + extend coverage. Không conflict.

**5 test cases:** (như WI-SERVER-REFACTOR Task D mô tả)

**AC:** `pytest tests/test_health_endpoint.py -m "neo4j and postgres"` green.

---

## Consolidate Wave 2 → trunk

```bash
git merge feat/m5-auth feat/m5-web-scaffold feat/m5-tests-health
make test-all   # MUST green
```

---

## WAVE 3 — Web UI Pages (3 WIs song song)

**Conflict-free guarantee:** Mỗi WI tạo file route + template MỚI. `app.py` chỉ cần uncomment 1 line — mỗi WI uncomment dòng khác nhau (no conflict). `base.html` không sửa.

---

### WI-WEB-REPOS — Profiles & Repos Management UI

**Branch:** `feat/m5-web-repos` | **Model:** Sonnet | **~40 min**

**Files touched:**
- MOD: `src/web_ui/app.py` (uncomment repos router line)
- NEW: `src/web_ui/routes/repos.py`
- NEW: `src/web_ui/templates/repos.html`
- NEW: `tests/test_web_ui_repos.py` (marker: `postgres`)

**Routes:**
- `GET /repos` → list profiles + repos
- `POST /repos/profiles` → create profile (form)
- `POST /repos/repos` → add repo (form: profile, url, branch, local_path)
  - URL SSH pattern → note: "SSH auto-clone not yet available. Git clone manually then provide local_path."
- `POST /repos/repos/{repo_id}/index` → spawn subprocess non-blocking, redirect với flash

**AC:** `GET /repos` 200. `POST /repos/profiles` valid → redirect, profile created. `make lint` green.

---

### WI-WEB-KEYS — API Key Management UI

**Branch:** `feat/m5-web-keys` | **Model:** Haiku | **~30 min**

**Files touched:**
- MOD: `src/web_ui/app.py` (uncomment api_keys router line)
- NEW: `src/web_ui/routes/api_keys.py`
- NEW: `src/web_ui/templates/api_keys.html`
- NEW: `tests/test_web_ui_api_keys.py` (marker: `postgres`)

**Routes:**
- `GET /api-keys` → list
- `POST /api-keys` → create → flash raw_key ONCE (warning banner)
- `POST /api-keys/{key_id}/deactivate` → deactivate

**AC:** `POST /api-keys` trả HTML có raw_key 1 lần. `make lint` green.

---

### WI-WEB-SSH — SSH Key Management UI (Generate + Store Only)

**Branch:** `feat/m5-web-ssh` | **Model:** Sonnet | **~35 min**

**Files touched:**
- MOD: `src/web_ui/app.py` (uncomment ssh_keys router line)
- NEW: `src/web_ui/routes/ssh_keys.py`
- NEW: `src/web_ui/templates/ssh_keys.html`
- NEW: `tests/test_web_ui_ssh_keys.py` (no marker — unit test keygen)

**Task — `routes/ssh_keys.py`:**

```python
import os, logging
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, PrivateFormat, NoEncryption
from cryptography.fernet import Fernet

_logger = logging.getLogger(__name__)

def _get_fernet() -> Fernet:
    key = os.getenv("FERNET_KEY")
    if not key:
        _logger.error("FERNET_KEY not set — SSH private keys NOT securely encrypted. Set FERNET_KEY immediately.")
        raise RuntimeError("FERNET_KEY is required to store SSH keys securely. Set it in your environment.")
    return Fernet(key.encode())

def generate_ed25519_keypair() -> tuple[str, str]:
    """Return (public_key_openssh, private_key_fernet_encrypted). Raises if FERNET_KEY missing."""
    private = Ed25519PrivateKey.generate()
    pub = private.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode()
    priv_pem = private.private_bytes(Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption())
    encrypted = _get_fernet().encrypt(priv_pem).decode()
    return pub, encrypted

def decrypt_private_key(encrypted: str) -> bytes:
    return _get_fernet().decrypt(encrypted.encode())
```

**Note:** KHÔNG có ephemeral fallback — `_get_fernet()` raise nếu FERNET_KEY không set. Fail-fast per Opus recommendation.

**Startup validation:** Thêm vào `src/web_ui/__main__.py`:
```python
if not os.getenv("FERNET_KEY"):
    print("WARNING: FERNET_KEY not set. SSH key storage disabled.", file=sys.stderr)
    print("Set FERNET_KEY to enable SSH key management.", file=sys.stderr)
    # Continue running but SSH routes will return 503
```

**Routes:**
- `GET /ssh-keys` → list
- `POST /ssh-keys` → generate keypair → save to DB (key_version=1) → show public key + GitHub/GitLab deploy key instructions
- `POST /ssh-keys/{key_id}/delete` → delete

**Tests:** `generate_ed25519_keypair()` round-trip với mock FERNET_KEY. Không cần DB.

**AC:** `POST /ssh-keys` với FERNET_KEY set → keypair created. Without FERNET_KEY → 503 or error. `make lint` green.

---

## Consolidate Wave 3 → trunk

```bash
git merge feat/m5-web-repos feat/m5-web-keys feat/m5-web-ssh
make test-all
```

---

## WAVE 4 — Cross-Component Tests (2 WIs song song, Haiku)

---

### WI-TESTS-INT — Integration Tests Auth + DB

**Branch:** `feat/m5-tests-int` | **Model:** Haiku | **~25 min**

**Files touched:**
- NEW: `tests/test_auth_integration.py` (marker: `postgres`)

**Tests:**
- End-to-end: create key via DB → auth middleware verifies → log_usage recorded
- Auth bypass path: `/health` endpoint không require header
- Cache invalidation: deactivate key → cache TTL expires → verify returns None
- Concurrent lock: 2 simulated calls, advisory lock rejects second
- Security: `verify_api_key` constant-time-ish (no early exit on hash mismatch via direct hash compare, not substring)

---

### WI-SMOKE — M5 Smoke Tests + CI Update

**Branch:** `feat/m5-smoke` | **Model:** Haiku | **~25 min**

**Files touched:**
- NEW: `tests/test_smoke_product_wow.py` (marker: `smoke + neo4j`)
- MOD: `.github/workflows/ci.yml`

**Tests:**
```python
pytestmark = [pytest.mark.smoke, pytest.mark.neo4j]

class TestSmokeHealth:
    def test_health_endpoint_schema(self): ...
    def test_mcp_tools_count_positive(self): ...  # NOT == 14

class TestSmokeAuth:
    def test_bad_key_returns_401(self): ...
    def test_no_key_returns_401(self): ...
    def test_health_bypasses_auth(self): ...  # /health returns 200 no key
```

CI: thêm `test_smoke_product_wow.py` vào smoke-tests job.

---

## Consolidate Wave 4 → trunk

```bash
git merge feat/m5-tests-int feat/m5-smoke
make test-all   # MUST green
```

---

## WAVE 5 — Docs (2 WIs song song, Haiku)

---

### WI-DOCS-README

**Branch:** `feat/m5-docs-readme` | **Model:** Haiku | **~20 min**

**Files:** MOD `README.md`, MOD `CONTRIBUTING.md`

Updated onboarding: `docker compose up -d` → Web UI → create API key (`python -m src.manager create-api-key <name>` hoặc `/api-keys` UI) → add to Claude Code settings.

Security section: generate FERNET_KEY, bind `127.0.0.1`, SSH tunnel từ remote.

Manual backup note: `docker compose stop neo4j && tar -czf neo4j-backup.tar.gz neo4j_data && docker compose start neo4j` + `pg_dump`.

---

### WI-DOCS-DEPLOY

**Branch:** `feat/m5-docs-deploy` | **Model:** Haiku | **~20 min**

**Files:** MOD `docs/deploy.md`, MOD `TASKS.md`, MOD `odoo-semantic.conf.example`

- deploy.md: §10 Auth, §11 Web UI, §12 SSH Keys, §13 Manual Backup (APOC-free)
- TASKS.md: M5 items `[x]`, deferred items moved to correct milestone
- conf.example: add `[server] web_ui_port`, remove `AUTH_DISABLED`

---

## FINAL Consolidate

```bash
git merge feat/m5-docs-readme feat/m5-docs-deploy
make test-all   # FINAL — all green = M5 DONE
```

---

## M5 Acceptance Criteria (rev 2)

1. `GET /health` → `{"status": "ok", "neo4j": "ok", "postgres": "ok", "mcp_tools": N}` (N > 0, không hardcode)
2. `GET http://localhost:8003/` → dashboard HTML với navbar + profile table
3. `python -m src.manager create-api-key admin` → in raw key; add to Claude Code `settings.json` → MCP tools respond
4. Web UI `/api-keys` → create key → confirm raw key shown once
5. Web UI `/ssh-keys` + FERNET_KEY set → generate keypair → show public key → admin adds to repo
6. 2 indexer runs đồng thời → second run nhận Postgres advisory lock error
7. MCP server với bad `X-API-Key` → 401; no key → 401; `/health` no key → 200
8. `make test-all` green

---

## Risk Table (rev 2)

| Risk | Mitigation |
|---|---|
| FastMCP v2 không có `custom_route` | Fallback: mount via Starlette `Route` directly vào underlying app |
| FastMCP internal `_tool_manager._tools` thay đổi | Wrap trong try/except → return -1 khi introspect fail, không crash |
| `asyncio.to_thread` overhead mỗi request | LRU cache giảm DB call từ 100% → ~5% (chỉ khi key miss/expire) |
| FERNET_KEY không set → SSH route crash | `_get_fernet()` raise RuntimeError, route catches → 503 với clear message |
| Wave 3 WI uncomment sai dòng trong `app.py` | Mỗi WI brief chỉ định chính xác dòng comment cần uncomment |
| `BaseHTTPMiddleware` + streaming response | FastMCP dùng SSE? Verify. Nếu có → switch sang raw ASGI middleware |

---

## Items Deferred (với milestone target)

| Item | Defer to | Lý do |
|---|---|---|
| Auto-clone qua SSH khi add repo | **M6** | Feature mới, không phải hardening |
| `src/cli.py` backup/restore | **M5.5** | Document manual procedure đủ M5; script có APOC risk |
| Pattern feedback loop (`/api/feedback`) | **M5.5** | Feature M4.6 defer, không block M5 |
| Rate limiting per API key | **M5.5** | DoS risk thấp với local deployment M5 |
| Structured JSON logging | **M5.5** | WARN format đủ M5 |
| FERNET_KEY rotation script | **M5.5** | Document manual process trong deploy.md M5 |
| `StrictHostKeyChecking` host fingerprint UI | **M6** | Cùng wave với auto-clone |
