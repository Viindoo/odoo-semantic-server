# ADR-0004: Auth, Web UI, SSH Key Policy — M5 Product Wow

**Date:** 2026-05-09  
**Status:** Accepted  

## Context

M5 biến project thành "bất kỳ ai deploy được trong < 10 phút". Cần thêm:
- API key authentication cho MCP server
- Web UI admin (localhost-only)
- SSH key management (generate + store Ed25519)
- Indexer concurrency protection

Quyết định này được debated với Opus model vào 2026-05-09 (rev 2). 3 blocker từ debate đã được fix.

## Decisions

### 1. Auth: API key via X-API-Key header (SHA-256 hash, no expiry M5)
**Lý do:** Đơn giản, khớp MCP client config (Claude Code settings.json). JWT/session là over-engineering cho M5.

### 2. Không có AUTH_DISABLED bypass
**Lý do:** Opus debate: silent bypass tạo footgun khi vô tình deploy production với bypass còn bật. Dev bind 127.0.0.1 là đủ để test local.

### 3. Web UI: FastAPI + Jinja2, port 8003 (tách khỏi MCP :8002)
**Lý do:** Đã có FastAPI qua FastMCP. Jinja2 nhẹ, không cần SPA framework.

### 4. Web UI hard-bind 127.0.0.1 (không config)
**Lý do:** Không có Web UI auth M5. Must not expose publicly. Admin dùng SSH tunnel nếu remote.

### 5. Web UI auth: Không có M5 (local admin, same-server)
**Lý do:** Defer M6. M5 local-only deploy, physical access = trust.

### 6. Indexer lock: PostgreSQL advisory lock (pg_try_advisory_lock)
**Lý do:** Opus debate BLOCKER: fcntl không protect async tasks same-process; không cross-container. Postgres advisory lock: auto-release on crash, cross-container, async-safe, không cần path.

### 7. SSH private key storage: Fernet-encrypted + key_version column
**Lý do:** Rotation story: backup FERNET_KEY separate from DB backups. key_version per row cho phép re-encrypt khi rotate key.

### 8. Auth middleware DB: LRU cache (5 min) + asyncio.create_task log
**Lý do:** Opus BLOCKER: sync psycopg2 trong async handler block event loop. asyncio.to_thread cho verify, create_task cho log (fire-and-forget).

### 9. mcp_tools trong /health: FastMCP introspection (không hardcode 14)
**Lý do:** Opus BLOCKER: hardcode tạo drift ngay khi add tool mới. Introspect via mcp._tool_manager._tools, wrap try/except return -1 khi fail.

### 10. SSH tmpfile: tempfile.mkstemp(mode=0o600)
**Lý do:** Opus: race window giữa create và chmod nếu dùng NamedTemporaryFile rồi os.chmod. mkstemp với mode là atomic.

## Deferred Items

| Item | Defer to | Lý do |
|---|---|---|
| Auto-clone qua SSH khi add repo | M6 | Feature mới, không phải hardening |
| CLI backup/restore script | M5.5 | Document manual procedure đủ M5 |
| Pattern feedback loop (POST /api/feedback) | M5.5 | Cần auth layer M5 trước |
| Rate limiting per API key | M5.5 | DoS risk thấp với local deployment M5 |
| Structured JSON logging | M5.5 | WARN format đủ M5 |
| FERNET_KEY rotation script | M5.5 | Document manual process đủ M5 |
| JWT / session auth | Not planned | Over-engineering cho use case này |
| web_ui_url trong MCP tool output | Dropped | Wrong layer — MCP tool không nên biết về Web UI URL |

## Consequences

**Positive:**
- Simple auth model khớp với MCP client config pattern
- PostgreSQL advisory lock không cần thêm dependency, auto-release on crash
- Fail-fast FERNET_KEY ngăn silent data-at-rest vulnerability
- LRU cache giảm DB roundtrip từ mỗi request xuống ~5%

**Negative:**
- Web UI không có auth M5 → must not expose publicly (documented hard constraint)
- FERNET_KEY rotation cần manual re-encrypt script (M5.5)
- No SSH auto-clone M5 → admin phải clone manually rồi provide local_path

**Risk:**
- FastMCP internal `_tool_manager._tools` API có thể thay đổi → wrap try/except, return -1 khi introspect fail (không crash)
- BaseHTTPMiddleware + streaming response (SSE) → verify FastMCP transport type; nếu SSE dùng raw ASGI middleware thay BaseHTTPMiddleware

## Alternatives Considered

1. **JWT với expiry** — over-engineering M5. Reject.
2. **AUTH_DISABLED env var** — footgun. Reject (Opus debate).
3. **fcntl advisory lock** — không cross-container, không protect async. Reject.
4. **Ephemeral FERNET_KEY fallback** — silent security risk. Reject (fail-fast thay).
5. **Web UI trên cùng port :8002** — mix MCP protocol với HTML UI gây routing conflict. Reject.
