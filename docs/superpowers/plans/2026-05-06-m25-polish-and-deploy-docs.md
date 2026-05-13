# M2.5 Polish + Deploy Docs Implementation Plan

> **Status:** ✓ DONE — M2.5 polish shipped 2026-05-07

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 3 Opus review findings (load_dotenv asymmetry I1, index_all abort I2, missing path validation I3) và tạo `docs/deploy.md` production guide đầy đủ 3 tiers (DB/App/Proxy) với SSOT — nội dung không duplicate sang README hay docs khác.

**Architecture:** Code fixes là independent patches không thay đổi public API. Deploy docs là pure documentation — 1 file chính `docs/deploy.md` + 3 reference configs trong `docs/deploy/`; README.md giữ quick-start 6-bước + link; không duplicate nội dung giữa các file.

**Tech Stack:** Python 3.12, psycopg2, neo4j-driver, FastMCP, systemd, nginx, Caddy, configparser.

---

## File Map

| Action | File | Nội dung thay đổi |
|--------|------|-------------------|
| Modify | `src/mcp/server.py` | Remove `load_dotenv()`, remove module-level constants, lazy `_get_driver()` |
| Modify | `pyproject.toml` | Remove `python-dotenv` từ `dependencies` |
| Modify | `src/indexer/pipeline.py` | Path validation trong `_index_repo()`, error handling trong `index_all()` |
| Modify | `src/db/migrate.py` | Thêm FK index `idx_repos_profile_id` |
| Modify | `docs/adr/0001-schema-evolution-policy.md` | Thêm note về `IF NOT EXISTS` no-op khi thêm column |
| Modify | `docs/superpowers/plans/2026-05-06-milestone-2-5-foundation-wow.md` | Thêm A4/A5 vào Post-Plan Adjustments |
| Modify | `tests/test_mcp_server_config.py` | Thêm 2 tests cho `_get_driver()` lazy reads |
| Modify | `tests/test_indexer_pipeline.py` | Thêm tests cho I2 + I3 |
| Create | `docs/deploy/odoo-semantic-mcp.service` | systemd unit file |
| Create | `docs/deploy/nginx.conf.example` | Nginx vhost với SSE config |
| Create | `docs/deploy/Caddyfile.example` | Caddy config (auto-TLS) |
| Create | `docs/deploy.md` | Production deploy guide 3 tiers |
| Modify | `README.md` | Trim "Deploy Server" → quick-start + link; thêm `docs/deploy.md` vào docs table |
| Modify | `CONTRIBUTING.md` | Thêm `docs/deploy.md` vào Tài Liệu Liên Quan table |
| Modify | `TASKS.md` | Thêm task deploy docs dưới M2.5 |
| Modify | `docs/thiet-ke-kien-truc.md` | Thêm link tới `docs/deploy.md` |

---

## Task 1: Fix I1 — Remove load_dotenv, lazy _get_driver()

**Context:** `src/mcp/server.py` hiện dùng `load_dotenv()` (đọc `.env` file) + module-level constants `NEO4J_URI/USER/PASSWORD`. Các entrypoints khác (`indexer`, `manager`, `migrate`) KHÔNG gọi `load_dotenv()` — tạo asymmetry. Option B: bỏ `load_dotenv()`, chuẩn hoá "`.env` chỉ cho Docker Compose, `odoo-semantic.conf` cho Python". `_get_driver()` đọc lazy từ env vars + config (same pattern as `_mcp_host()`/`_mcp_port()`).

**Files:**
- Modify: `src/mcp/server.py`
- Modify: `pyproject.toml`
- Modify: `tests/test_mcp_server_config.py`

- [ ] **Step 1: Viết failing tests cho _get_driver() lazy**

Thêm vào cuối `tests/test_mcp_server_config.py`:

```python
def test_get_driver_reads_neo4j_uri_from_config(tmp_path, monkeypatch):
    """_get_driver() reads neo4j_uri from [database] section, not env var."""
    import importlib
    import neo4j
    import src.config as config_mod
    import src.mcp.server as server_mod

    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text(
        "[database]\nneo4j_uri = bolt://cfg.example.com:7687\n"
        "neo4j_user = cfguser\nneo4j_password = cfgpass\n"
    )
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
    monkeypatch.delenv("NEO4J_URI", raising=False)
    monkeypatch.delenv("NEO4J_USER", raising=False)
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    config_mod._conf = None

    captured: dict = {}
    monkeypatch.setattr(neo4j.GraphDatabase, "driver",
                        lambda uri, *, auth: captured.update({"uri": uri, "auth": auth}) or object())

    importlib.reload(server_mod)
    server_mod._driver = None

    server_mod._get_driver()

    assert captured["uri"] == "bolt://cfg.example.com:7687"
    assert captured["auth"] == ("cfguser", "cfgpass")


def test_get_driver_env_overrides_config(tmp_path, monkeypatch):
    """NEO4J_URI env var takes priority over config file in _get_driver()."""
    import importlib
    import neo4j
    import src.config as config_mod
    import src.mcp.server as server_mod

    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text(
        "[database]\nneo4j_uri = bolt://cfg.example.com:7687\n"
        "neo4j_user = cfguser\nneo4j_password = cfgpass\n"
    )
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
    monkeypatch.setenv("NEO4J_URI", "bolt://env.example.com:7687")
    monkeypatch.setenv("NEO4J_USER", "envuser")
    monkeypatch.setenv("NEO4J_PASSWORD", "envpass")
    config_mod._conf = None

    captured: dict = {}
    monkeypatch.setattr(neo4j.GraphDatabase, "driver",
                        lambda uri, *, auth: captured.update({"uri": uri, "auth": auth}) or object())

    importlib.reload(server_mod)
    server_mod._driver = None

    server_mod._get_driver()

    assert captured["uri"] == "bolt://env.example.com:7687"
    assert captured["auth"] == ("envuser", "envpass")
```

- [ ] **Step 2: Chạy failing tests**

```bash
~/.venv/odoo-semantic-mcp/bin/pytest tests/test_mcp_server_config.py::test_get_driver_reads_neo4j_uri_from_config tests/test_mcp_server_config.py::test_get_driver_env_overrides_config -v
```

Expected: FAIL — `AttributeError: module 'src.mcp.server' has no attribute '_get_driver'` (hoặc test import lỗi vì `_get_driver` hiện đọc module-level constants chứ không đọc config).

- [ ] **Step 3: Sửa src/mcp/server.py**

Thay toàn bộ phần đầu file (dòng 1–22) bằng:

```python
# src/mcp/server.py
import os

from fastmcp import FastMCP
from neo4j import GraphDatabase

mcp = FastMCP("odoo-semantic")
_driver = None


def _get_driver():
    global _driver
    if _driver is None:
        from src import config
        uri = (
            os.getenv("NEO4J_URI")
            or config.get("database", "neo4j_uri", fallback="bolt://localhost:7687")
        )
        user = (
            os.getenv("NEO4J_USER")
            or config.get("database", "neo4j_user", fallback="neo4j")
        )
        password = (
            os.getenv("NEO4J_PASSWORD")
            or config.get("database", "neo4j_password", fallback="password")
        )
        _driver = GraphDatabase.driver(uri, auth=(user, password))
    return _driver
```

Xoá các dòng:
```python
from dotenv import load_dotenv
...
load_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")
```

Trong `_get_driver()` cũ (dòng 18–22):
```python
def _get_driver():
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    return _driver
```

Thay bằng implementation lazy ở trên.

File sau khi sửa (dòng 1–28):
```python
# src/mcp/server.py
import os

from fastmcp import FastMCP
from neo4j import GraphDatabase

mcp = FastMCP("odoo-semantic")
_driver = None


def _get_driver():
    global _driver
    if _driver is None:
        from src import config
        uri = (
            os.getenv("NEO4J_URI")
            or config.get("database", "neo4j_uri", fallback="bolt://localhost:7687")
        )
        user = (
            os.getenv("NEO4J_USER")
            or config.get("database", "neo4j_user", fallback="neo4j")
        )
        password = (
            os.getenv("NEO4J_PASSWORD")
            or config.get("database", "neo4j_password", fallback="password")
        )
        _driver = GraphDatabase.driver(uri, auth=(user, password))
    return _driver
```

Phần còn lại (`_latest_version`, `_resolve_*`, `@mcp.tool()`, `_mcp_host`, `_mcp_port`, `__main__`) giữ nguyên.

- [ ] **Step 4: Sửa pyproject.toml**

Xoá dòng `"python-dotenv>=1.0",` khỏi `[project].dependencies`.

```toml
dependencies = [
    "fastmcp>=2.3,<3.0",
    "neo4j>=5.0,<6.0",
    "psycopg2-binary>=2.9",
    "authlib>=1.6.5,<1.7.0",
]
```

- [ ] **Step 5: Reinstall venv để áp dụng dep changes**

```bash
uv pip install --python ~/.venv/odoo-semantic-mcp/bin/python -e ".[dev]"
```

- [ ] **Step 6: Chạy unit tests — phải PASS**

```bash
~/.venv/odoo-semantic-mcp/bin/pytest tests/test_mcp_server_config.py -v
```

Expected: 4 tests PASS (2 cũ + 2 mới).

- [ ] **Step 7: Chạy toàn bộ unit tests — phải PASS**

```bash
make test
```

Expected: tất cả unit tests PASS. Nếu có FAIL liên quan đến `dotenv`, kiểm tra xem file nào còn import `load_dotenv`.

- [ ] **Step 8: Commit**

```bash
git add src/mcp/server.py pyproject.toml tests/test_mcp_server_config.py
git commit -m "fix(server): remove load_dotenv — odoo-semantic.conf is SSOT for Python tier"
```

---

## Task 2: Fix I3 — Path validation trong _index_repo()

**Context:** `_index_repo()` trong `pipeline.py` gọi `build_registry()` với `local_path` mà không kiểm tra path tồn tại. Nếu admin typo path, lỗi không rõ ràng. Fix: check `Path(local_path).is_dir()` trước, raise `FileNotFoundError` với message rõ.

**Files:**
- Modify: `src/indexer/pipeline.py`
- Modify: `tests/test_indexer_pipeline.py`

- [ ] **Step 1: Viết failing test**

Trong `tests/test_indexer_pipeline.py`, tìm class test hoặc thêm function sau các test hiện có. Thêm:

```python
def test_index_repo_raises_for_missing_path(pg_conn, clean_pg, neo4j_driver):
    """_index_repo raises FileNotFoundError when local_path does not exist."""
    from src.db.migrate import run_migrations
    from src.db.repo_registry import add_profile, add_repo, get_repos_for_profile
    from src.indexer.pipeline import _index_repo
    from src.indexer.writer_neo4j import Neo4jWriter
    import os

    run_migrations(pg_conn)
    pid = add_profile(pg_conn, name="test_path_val", odoo_version=TEST_VERSION,
                      description="")
    add_repo(pg_conn, profile_id=pid, url="x", branch="b",
             local_path="/nonexistent/__does_not_exist__/path")

    repos = get_repos_for_profile(pg_conn, "test_path_val")
    writer = Neo4jWriter(
        uri=os.getenv("NEO4J_TEST_URI", "bolt://localhost:7687"),
        user=os.getenv("NEO4J_TEST_USER", "neo4j"),
        password=os.getenv("NEO4J_TEST_PASSWORD", "password"),
    )
    writer.setup_indexes()

    try:
        import pytest
        with pytest.raises(FileNotFoundError, match="local_path does not exist"):
            _index_repo(repos[0], writer)
    finally:
        writer.close()
```

Kiểm tra `TEST_VERSION` có import trong file không (thường `from tests.conftest import TEST_VERSION` hoặc định nghĩa lại `TEST_VERSION = "99.0"` tại đầu file).

- [ ] **Step 2: Chạy failing test**

```bash
~/.venv/odoo-semantic-mcp/bin/pytest tests/test_indexer_pipeline.py::test_index_repo_raises_for_missing_path -v
```

Expected: FAIL — test passes without raising (path check chưa có).

- [ ] **Step 3: Thêm path check vào _index_repo()**

Trong `src/indexer/pipeline.py`, hàm `_index_repo()` hiện tại bắt đầu:

```python
def _index_repo(
    repo: dict,
    writer: Neo4jWriter,
) -> dict:
    """..."""
    local_path: str = repo["local_path"]
    odoo_version: str = repo["odoo_version"]

    # build_registry expects list[tuple[repo_path, odoo_version]]
    registry = build_registry([(local_path, odoo_version)])
```

Thêm import `from pathlib import Path` vào đầu file (sau `import os`), rồi thêm check sau dòng `odoo_version = repo["odoo_version"]`:

```python
from pathlib import Path  # thêm vào top-level imports

def _index_repo(
    repo: dict,
    writer: Neo4jWriter,
) -> dict:
    """Index a single repo dict (from get_repos_for_profile).

    Returns per-repo counters: {modules, views, qweb}.
    """
    local_path: str = repo["local_path"]
    odoo_version: str = repo["odoo_version"]

    if not Path(local_path).is_dir():
        raise FileNotFoundError(f"local_path does not exist: {local_path!r}")

    # build_registry expects list[tuple[repo_path, odoo_version]]
    registry = build_registry([(local_path, odoo_version)])
```

- [ ] **Step 4: Chạy test — phải PASS**

```bash
~/.venv/odoo-semantic-mcp/bin/pytest tests/test_indexer_pipeline.py::test_index_repo_raises_for_missing_path -v
```

Expected: PASS.

- [ ] **Step 5: Chạy unit tests**

```bash
make test
```

Expected: tất cả PASS.

- [ ] **Step 6: Commit**

```bash
git add src/indexer/pipeline.py tests/test_indexer_pipeline.py
git commit -m "fix(pipeline): validate local_path exists before indexing repo"
```

---

## Task 3: Fix I2 — index_all() continues on profile error

**Context:** `index_all()` hiện không bắt exception từ `index_profile()`. Nếu 1 profile lỗi (vd. bad path, Neo4j timeout), toàn bộ loop abort — các profiles còn lại không được index. Fix: wrap từng call trong try/except, log lỗi, tiếp tục, thêm `profiles_failed` vào summary dict.

**Files:**
- Modify: `src/indexer/pipeline.py`
- Modify: `tests/test_indexer_pipeline.py`

- [ ] **Step 1: Viết failing test**

Thêm vào `tests/test_indexer_pipeline.py`:

```python
def test_index_all_continues_after_profile_failure(pg_conn, clean_pg, neo4j_driver):
    """index_all continues with remaining profiles if one fails, reports failures."""
    from src.db.migrate import run_migrations
    from src.db.repo_registry import add_profile, add_repo
    from src.indexer.pipeline import index_all

    run_migrations(pg_conn)

    # Profile 1: bad path — sẽ gây FileNotFoundError sau Task 2
    pid1 = add_profile(pg_conn, name="bad_prof", odoo_version=TEST_VERSION, description="")
    add_repo(pg_conn, profile_id=pid1, url="x", branch="b",
             local_path="/nonexistent/__bad__/path")

    # Profile 2: no repos — sẽ trả về {modules: 0, views: 0, qweb: 0} thành công
    add_profile(pg_conn, name="empty_prof", odoo_version=TEST_VERSION, description="")

    summary = index_all(pg_conn)

    assert summary["profiles_ok"] == 1, f"Expected 1 ok profile, got {summary}"
    assert "bad_prof" in summary["profiles_failed"]
    assert summary["modules"] == 0
```

Lưu ý: test này cần cả `neo4j` và `postgres` marker. Kiểm tra đầu file có:
```python
pytestmark = [pytest.mark.neo4j, pytest.mark.postgres]
```

- [ ] **Step 2: Chạy failing test**

```bash
~/.venv/odoo-semantic-mcp/bin/pytest tests/test_indexer_pipeline.py::test_index_all_continues_after_profile_failure -v -m "neo4j and postgres"
```

Expected: FAIL — `index_all` raises exception thay vì trả về summary.

- [ ] **Step 3: Sửa index_all() trong pipeline.py**

Hàm hiện tại:

```python
def index_all(pg_conn) -> dict:
    """Index every profile registered in PostgreSQL.

    Returns aggregate summary: {profiles, modules, views, qweb}.
    """
    profiles = list_profiles(pg_conn)
    agg_modules = 0
    agg_views = 0
    agg_qweb = 0

    for profile in profiles:
        summary = index_profile(pg_conn, profile_name=profile["name"])
        agg_modules += summary["modules"]
        agg_views += summary["views"]
        agg_qweb += summary["qweb"]

    return {
        "profiles": len(profiles),
        "modules": agg_modules,
        "views": agg_views,
        "qweb": agg_qweb,
    }
```

Thay bằng:

```python
def index_all(pg_conn) -> dict:
    """Index every profile registered in PostgreSQL.

    Continues after per-profile failures — failed profiles are listed in
    the returned summary under 'profiles_failed'.

    Returns aggregate summary: {profiles_ok, profiles_failed, modules, views, qweb}.
    """
    profiles = list_profiles(pg_conn)
    agg_modules = 0
    agg_views = 0
    agg_qweb = 0
    profiles_ok = 0
    profiles_failed: list[str] = []

    for profile in profiles:
        name = profile["name"]
        try:
            summary = index_profile(pg_conn, profile_name=name)
            agg_modules += summary["modules"]
            agg_views += summary["views"]
            agg_qweb += summary["qweb"]
            profiles_ok += 1
        except Exception:
            _logger.exception("index_all: profile %r failed — skipping", name)
            profiles_failed.append(name)

    return {
        "profiles_ok": profiles_ok,
        "profiles_failed": profiles_failed,
        "modules": agg_modules,
        "views": agg_views,
        "qweb": agg_qweb,
    }
```

- [ ] **Step 4: Cập nhật src/indexer/__main__.py để in summary đúng key**

`__main__.py` hiện in `f"Done: {summary}"` — vẫn đúng vì chỉ print dict. Không cần thay đổi.

Tuy nhiên nếu có test trong `test_indexer_pipeline.py` check key `"profiles"` trong summary của `index_all`, cần cập nhật test đó sang `"profiles_ok"`. Grep:

```bash
grep -n '"profiles"' tests/test_indexer_pipeline.py
```

Nếu có, đổi sang `"profiles_ok"`.

- [ ] **Step 5: Chạy failing test — phải PASS**

```bash
~/.venv/odoo-semantic-mcp/bin/pytest tests/test_indexer_pipeline.py::test_index_all_continues_after_profile_failure -v -m "neo4j and postgres"
```

Expected: PASS.

- [ ] **Step 6: Chạy toàn bộ pipeline tests**

```bash
~/.venv/odoo-semantic-mcp/bin/pytest tests/test_indexer_pipeline.py -v -m "neo4j and postgres"
```

Expected: tất cả PASS.

- [ ] **Step 7: Commit**

```bash
git add src/indexer/pipeline.py tests/test_indexer_pipeline.py
git commit -m "fix(pipeline): index_all continues on profile error, reports profiles_failed"
```

---

## Task 4: Minor polish — FK index + ADR note + plan doc

**Context:** 3 minor fixes từ Opus review không cần tests riêng:
- M1: Thêm index `idx_repos_profile_id` vào `migrate.py` (1 dòng SQL)
- M6: Thêm note vào ADR-0001 về `CREATE TABLE IF NOT EXISTS` là no-op với new columns
- M7: Thêm A4/A5 vào Post-Plan Adjustments của plan doc M2.5

**Files:**
- Modify: `src/db/migrate.py`
- Modify: `docs/adr/0001-schema-evolution-policy.md`
- Modify: `docs/superpowers/plans/2026-05-06-milestone-2-5-foundation-wow.md`

- [ ] **Step 1: Thêm FK index vào migrate.py**

Trong `SCHEMA_SQL`, sau hai `CREATE TABLE IF NOT EXISTS` blocks, thêm:

```python
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS profiles (
    id           SERIAL PRIMARY KEY,
    name         TEXT UNIQUE NOT NULL,
    odoo_version TEXT NOT NULL,
    description  TEXT,
    created_at   TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS repos (
    id              SERIAL PRIMARY KEY,
    profile_id      INTEGER REFERENCES profiles(id) ON DELETE CASCADE,
    url             TEXT NOT NULL,
    branch          TEXT NOT NULL,
    local_path      TEXT NOT NULL,
    status          TEXT DEFAULT 'pending',
    last_indexed_at TIMESTAMP,
    error_msg       TEXT,
    created_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE (url, branch)
);

CREATE INDEX IF NOT EXISTS idx_repos_profile_id ON repos(profile_id);
"""
```

- [ ] **Step 2: Thêm note vào ADR-0001**

Mở `docs/adr/0001-schema-evolution-policy.md`. Tìm section "M2.5–M5: Add-Only" và thêm note:

```markdown
> **Lưu ý:** `CREATE TABLE IF NOT EXISTS` là idempotent với *tạo bảng mới*, nhưng **không thêm column vào bảng đã tồn tại**.
> Nếu developer thêm column vào `SCHEMA_SQL`, lệnh sẽ **silently no-op** trên DB đã có bảng đó.
> Đây chính là failure mode ADR này muốn ngăn. Quy tắc: thêm column = forbidden cho đến M6 khi có migration tool.
```

- [ ] **Step 3: Thêm A4/A5 vào plan doc M2.5**

Mở `docs/superpowers/plans/2026-05-06-milestone-2-5-foundation-wow.md`. Tìm section "Post-Plan Adjustments" (đã có A1/A2/A3). Thêm:

```markdown
**A4: `.env.example` cleanup (F3/A2 fix):**
`.env.example` ban đầu còn chứa `MCP_HOST`, `MCP_PORT`, `ODOO_REPOS_BASE_DIR` — các config đã được chuyển vào `odoo-semantic.conf`. Opus review (lần 1) phát hiện, đã xoá commit `c21ddda`. Default `MCP_HOST=0.0.0.0` cũng là security risk.

**A5: ADR-0001 schema evolution policy (B2):**
Opus review yêu cầu formal documentation về schema evolution policy. Tạo `docs/adr/0001-schema-evolution-policy.md` commit `644465d` — add-only M2.5–M5, adopt migration tool (yoyo/Alembic) tại M6.
```

- [ ] **Step 4: Chạy unit tests để verify migrate.py vẫn đúng**

```bash
make test
```

Expected: PASS (migration tests dùng `DROP TABLE IF EXISTS` trong `clean_pg` fixture nên index cũng bị drop + recreate).

- [ ] **Step 5: Commit**

```bash
git add src/db/migrate.py docs/adr/0001-schema-evolution-policy.md \
    docs/superpowers/plans/2026-05-06-milestone-2-5-foundation-wow.md
git commit -m "fix(m2.5): FK index + ADR note on IF NOT EXISTS + plan doc A4/A5"
```

---

## Task 5: Tạo reference config files trong docs/deploy/

**Context:** Reference configs cho admin copy về server. 3 files: systemd unit, nginx vhost, Caddyfile. Phần này pure documentation, không cần tests.

**Files:**
- Create: `docs/deploy/odoo-semantic-mcp.service`
- Create: `docs/deploy/nginx.conf.example`
- Create: `docs/deploy/Caddyfile.example`

- [ ] **Step 1: Tạo docs/deploy/ directory và systemd unit**

```
docs/deploy/odoo-semantic-mcp.service
```

Nội dung:

```ini
# odoo-semantic-mcp.service — systemd unit cho MCP server
# Đặt tại: /etc/systemd/system/odoo-semantic-mcp.service
# Sau khi copy:
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now odoo-semantic-mcp

[Unit]
Description=Odoo Semantic MCP Server
After=network.target docker.service

[Service]
Type=simple
User=odoo-semantic
WorkingDirectory=/opt/odoo-semantic-mcp
# Config file nằm ngoài repo — không commit vào git
Environment="ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf"
ExecStart=/home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python -m src.mcp.server
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Tạo nginx config**

```
docs/deploy/nginx.conf.example
```

Nội dung:

```nginx
# nginx.conf.example — reverse proxy cho MCP server
# Copy vào /etc/nginx/sites-available/odoo-semantic-mcp
# rồi: sudo ln -s ../sites-available/odoo-semantic-mcp sites-enabled/
#       sudo nginx -t && sudo systemctl reload nginx
#
# Thay semantic.example.com bằng domain thật.
# Cert Let's Encrypt: sudo certbot --nginx -d semantic.example.com

server {
    listen 443 ssl http2;
    server_name semantic.example.com;

    ssl_certificate     /etc/letsencrypt/live/semantic.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/semantic.example.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;

    # MCP dùng streamable-http với SSE — bắt buộc tắt buffering + extend timeout
    location /mcp {
        proxy_pass         http://127.0.0.1:8002/mcp;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_buffering    off;       # bắt buộc cho SSE streaming
        proxy_read_timeout 3600s;    # MCP sessions có thể dài
        proxy_send_timeout 3600s;
    }
}

server {
    listen 80;
    server_name semantic.example.com;
    return 301 https://$host$request_uri;
}

# --- Auth options (chọn 1, bỏ comment) ---
#
# Option A — IP allowlist (đơn giản nhất cho internal team):
# location /mcp {
#     allow 10.0.0.0/8;
#     deny all;
#     # ... proxy_pass block ở trên ...
# }
#
# Option B — HTTP Basic Auth:
# location /mcp {
#     auth_basic "Odoo Semantic MCP";
#     auth_basic_user_file /etc/nginx/.htpasswd;
#     # htpasswd: sudo apt install apache2-utils
#     #           sudo htpasswd -c /etc/nginx/.htpasswd <username>
#     # ... proxy_pass block ở trên ...
# }
#
# Option C — API Key (M5, chưa implement trong codebase):
# location /mcp {
#     # Proxy header forward — validation sẽ được thêm ở M5
#     proxy_set_header X-API-Key $http_x_api_key;
#     # ... proxy_pass block ở trên ...
# }
```

- [ ] **Step 3: Tạo Caddyfile**

```
docs/deploy/Caddyfile.example
```

Nội dung:

```
# Caddyfile.example — Caddy reverse proxy (auto-TLS)
# Caddy tự lấy cert Let's Encrypt — không cần certbot.
# Thay semantic.example.com bằng domain thật.
# Docs: https://caddyserver.com/docs/caddyfile/directives/reverse_proxy

semantic.example.com {
    reverse_proxy /mcp* 127.0.0.1:8002 {
        flush_interval -1   # -1 = disable buffering, bắt buộc cho SSE streaming
    }
}
```

- [ ] **Step 4: Commit**

```bash
git add docs/deploy/
git commit -m "docs: add deploy reference configs (systemd, nginx, Caddy)"
```

---

## Task 6: Tạo docs/deploy.md

**Context:** File chính cho admin deploy. SSOT — các section ở đây KHÔNG được duplicate vào README hay docs khác. README chỉ link vào đây. Dựa trên code thật: `odoo-semantic.conf [server]` bind 127.0.0.1:8002, Python apps đọc config không đọc `.env`.

**Files:**
- Create: `docs/deploy.md`

- [ ] **Step 1: Tạo docs/deploy.md**

```
docs/deploy.md
```

Nội dung đầy đủ:

````markdown
# Production Deploy — Odoo Semantic MCP

Hướng dẫn này dành cho **admin** deploy server. Developer xem [`CONTRIBUTING.md`](../CONTRIBUTING.md).

---

## 0. Topology

```
Người dùng (AI tool)
        │ HTTPS :443
        ▼
  ┌─────────────┐
  │  Nginx/Caddy │  ← reverse proxy, TLS termination, auth
  └──────┬──────┘
         │ HTTP 127.0.0.1:8002
         ▼
  ┌─────────────┐
  │  MCP Server  │  ← python -m src.mcp.server (systemd)
  └──────┬───┬──┘
         │   │
   bolt  │   │ psycopg2
7687 ▼   │   ▼ 5432
  ┌──────┴───┴──┐
  │  Databases   │  ← docker compose (Neo4j + PostgreSQL)
  └─────────────┘
```

**Same-server (default, ≤30 users):** tất cả tiers trên 1 host.  
**Split-tier (≥80 users / HA):** DB trên VM riêng — xem [§8 Split-Tier](#8-split-tier-migration).

---

## 1. Prerequisites

| Thứ | Phiên bản | Dùng cho |
|-----|-----------|---------|
| Ubuntu 22.04 / Debian 12 | LTS | OS khuyến nghị |
| Docker Engine | 24+ | DB tier |
| Python | 3.12 | App tier |
| uv | 0.4+ | Package manager |
| Nginx **hoặc** Caddy | bất kỳ | Proxy tier |
| DNS record | — | Trỏ domain về IP server |
| TLS cert | — | Let's Encrypt hoặc wildcard |

---

## 2. DB Tier — Neo4j + PostgreSQL

### 2.1 Cấu hình

**`.env`** — secrets cho Docker Compose (KHÔNG đọc bởi Python apps):

```bash
# Bắt buộc điền:
NEO4J_PASSWORD=<strong-password>
PG_PASSWORD=<strong-password>

# Giữ nguyên (hoặc bump version khi cần):
NEO4J_IMAGE=neo4j:5.26.25
```

**`odoo-semantic.conf`** — cấu hình Python app (đọc bởi indexer/manager/migrate/server):

```ini
[database]
neo4j_uri      = bolt://localhost:7687
neo4j_user     = neo4j
neo4j_password = <same-as-NEO4J_PASSWORD-in-.env>

pg_dsn = postgresql://odoo_semantic:<PG_PASSWORD>@localhost:5432/odoo_semantic
```

> **Quy tắc hai lớp config:**
> - `.env` → Docker Compose đọc khi `docker compose up`
> - `odoo-semantic.conf` → Python apps đọc (indexer, manager, migrate, mcp server)
> - Python apps **không** đọc `.env`. Secrets cần khai báo ở **cả hai** file.

### 2.2 Khởi động DB

```bash
docker compose up -d
docker compose ps   # cả hai service phải ở trạng thái healthy
```

Kiểm tra Neo4j:
```bash
docker compose exec neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" 'RETURN 1'
```

Kiểm tra PostgreSQL:
```bash
docker compose exec postgres pg_isready -U odoo_semantic
```

### 2.3 Bootstrap PostgreSQL schema

Chạy **một lần** sau khi postgres healthy:

```bash
ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf \
    ~/.venv/odoo-semantic-mcp/bin/python -m src.db.migrate
```

Output: `✓ Migrations applied to postgresql://...`

Lệnh này idempotent — chạy lại không có hại.

### 2.4 Ports — Same-server vs Split-tier

`docker-compose.yml` mặc định bind ports `127.0.0.1` (chỉ localhost):

```yaml
ports:
  - "127.0.0.1:7687:7687"   # Neo4j bolt — chỉ app cùng server truy cập được
  - "127.0.0.1:5432:5432"   # PostgreSQL
```

**Split-tier:** đổi thành `"0.0.0.0:7687:7687"` + chặn firewall — xem [§8](#8-split-tier-migration).

### 2.5 Backup thủ công (đến M5)

```bash
# Neo4j — dump database
docker compose exec neo4j \
    neo4j-admin database dump neo4j --to-path=/data/backups

# PostgreSQL
docker compose exec postgres \
    pg_dump -U odoo_semantic odoo_semantic \
    > ~/backups/odoo_semantic_$(date +%Y%m%d).sql
```

---

## 3. App Tier — Indexer + MCP Server

### 3.1 Cài đặt

```bash
git clone https://github.com/Viindoo/odoo-semantic-mcp /opt/odoo-semantic-mcp
cd /opt/odoo-semantic-mcp
make install
# → tạo ~/.venv/odoo-semantic-mcp + copy config templates
```

### 3.2 Đặt config file

```bash
sudo mkdir -p /etc/odoo-semantic
sudo cp odoo-semantic.conf.example /etc/odoo-semantic/odoo-semantic.conf
sudo chmod 600 /etc/odoo-semantic/odoo-semantic.conf
sudo chown odoo-semantic:odoo-semantic /etc/odoo-semantic/odoo-semantic.conf
```

Điền passwords thật vào `odoo-semantic.conf`:

```ini
[database]
neo4j_uri      = bolt://localhost:7687
neo4j_user     = neo4j
neo4j_password = <NEO4J_PASSWORD>

pg_dsn = postgresql://odoo_semantic:<PG_PASSWORD>@localhost:5432/odoo_semantic

[server]
host = 127.0.0.1   # giữ nguyên — proxy tier sẽ handle external
port = 8002

[indexer]
repos_base_dir = /srv/odoo-repos
```

### 3.3 Đăng ký repos + index lần đầu

Admin clone repos thủ công vào server trước:

```bash
git clone --branch 17.0 https://github.com/odoo/odoo /srv/odoo-repos/odoo_17.0
# ... clone thêm viindoo addons repos ...
```

Đăng ký trong PostgreSQL:

```bash
export ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf
PY=~/.venv/odoo-semantic-mcp/bin/python

$PY -m src.manager add-profile viindoo_17 --version 17.0
$PY -m src.manager add-repo \
    --profile viindoo_17 \
    --url github.com/odoo/odoo --branch 17.0 \
    --local-path /srv/odoo-repos/odoo_17.0
$PY -m src.manager list   # verify
```

Index lần đầu (blocking, ~5–30 phút tùy số module):

```bash
$PY -m src.indexer --profile viindoo_17
# hoặc index toàn bộ profiles:
# $PY -m src.indexer --all
```

Output: `Done: {'profiles_ok': 1, 'profiles_failed': [], 'modules': 412, 'views': 3801, 'qweb': 287}`

### 3.4 MCP server dạng systemd service

Tạo service user:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin odoo-semantic
```

Copy systemd unit:

```bash
sudo cp /opt/odoo-semantic-mcp/docs/deploy/odoo-semantic-mcp.service \
        /etc/systemd/system/

# Chỉnh sửa path nếu khác /opt/odoo-semantic-mcp:
sudo nano /etc/systemd/system/odoo-semantic-mcp.service

sudo systemctl daemon-reload
sudo systemctl enable --now odoo-semantic-mcp
sudo systemctl status odoo-semantic-mcp
```

Xem logs:

```bash
sudo journalctl -u odoo-semantic-mcp -f
```

### 3.5 Re-index định kỳ (cron, đến M6)

```bash
sudo tee /etc/cron.d/odoo-semantic-reindex > /dev/null << 'EOF'
# Re-index toàn bộ profiles mỗi ngày lúc 3 giờ sáng
0 3 * * * odoo-semantic ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf \
    /home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python -m src.indexer --all \
    >> /var/log/odoo-semantic-reindex.log 2>&1
EOF
```

### 3.6 tmux fallback (khi không có systemd)

```bash
tmux new -d -s odoo-semantic-mcp \
    'ODOO_SEMANTIC_CONF=/etc/odoo-semantic/odoo-semantic.conf \
    ~/.venv/odoo-semantic-mcp/bin/python -m src.mcp.server'
tmux attach -t odoo-semantic-mcp   # để xem logs
```

---

## 4. Proxy Tier — Nginx hoặc Caddy

MCP server bind `127.0.0.1:8002` — **bắt buộc** có reverse proxy để external clients truy cập được.

### 4.1 Nginx

Copy và sửa config:

```bash
sudo cp /opt/odoo-semantic-mcp/docs/deploy/nginx.conf.example \
        /etc/nginx/sites-available/odoo-semantic-mcp

# Thay semantic.example.com bằng domain thật
sudo nano /etc/nginx/sites-available/odoo-semantic-mcp

sudo ln -s /etc/nginx/sites-available/odoo-semantic-mcp \
           /etc/nginx/sites-enabled/
sudo nginx -t   # kiểm tra syntax
```

Lấy TLS cert:

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d semantic.example.com
```

Reload:

```bash
sudo systemctl reload nginx
```

Xem `docs/deploy/nginx.conf.example` để biết config đầy đủ, bao gồm các option auth.

### 4.2 Caddy (auto-TLS, đơn giản hơn)

```bash
sudo apt install caddy
```

Thêm vào `/etc/caddy/Caddyfile` (xem `docs/deploy/Caddyfile.example`):

```
semantic.example.com {
    reverse_proxy /mcp* 127.0.0.1:8002 {
        flush_interval -1
    }
}
```

```bash
sudo systemctl reload caddy
```

### 4.3 Auth (M2.5 — chưa có API key validation)

Chọn 1 option tạm thời:

| Option | Phù hợp cho | Config |
|--------|-------------|--------|
| IP allowlist | Internal team (static IP) | `allow 10.0.0.0/8; deny all;` trong nginx location block |
| HTTP Basic | Small team | `auth_basic` + `htpasswd` — xem comment trong `nginx.conf.example` |
| (Không có) | Dev/staging nội bộ | Chỉ khi server không public internet |

**Lưu ý:** `X-API-Key` trong cấu hình ví dụ Claude/VS Code là placeholder forward-compatible cho M5. Codebase **chưa validate** header này.

### 4.4 Verify proxy

```bash
curl -I https://semantic.example.com/mcp
# 200 hoặc 405 = MCP server đang chạy
# 502 = MCP server down, kiểm tra systemctl status odoo-semantic-mcp
```

---

## 5. E2E Smoke Test

Sau khi tất cả tiers đang chạy:

1. Thêm vào `~/.claude/settings.json` (developer laptop):

```json
{
  "mcpServers": {
    "odoo-semantic": {
      "url": "https://semantic.example.com/mcp"
    }
  }
}
```

2. Mở Claude Code, hỏi:
   ```
   resolve_model("account.move", "17.0")
   ```

3. Expected: tool trả về inheritance chain của `account.move`.

4. Nếu trả về rỗng: kiểm tra `repos.status='indexed'` trong PostgreSQL:
   ```bash
   docker compose exec postgres \
       psql -U odoo_semantic -c "SELECT name, status FROM repos;"
   ```

---

## 6. Operational Runbook

### Vấn đề thường gặp

| Triệu chứng | Nguyên nhân phổ biến | Fix |
|-------------|---------------------|-----|
| 502 Bad Gateway | MCP server không chạy | `sudo systemctl start odoo-semantic-mcp` |
| "Không tìm thấy model" | Chưa index hoặc index lỗi | Kiểm tra `repos.status`, chạy lại indexer |
| Neo4j OOM | JVM heap thiếu | Tăng `NEO4J_server_memory_heap_max__size` trong `docker-compose.yml` |
| Index chậm | Nhiều module, network | Đây là expected — lần đầu ~10-30 phút cho 400+ modules |
| `✗ Cannot connect to PostgreSQL` | PG chưa healthy / sai DSN | `docker compose ps`, kiểm tra `pg_dsn` trong conf |

### Log locations

| Thành phần | Lệnh xem log |
|------------|-------------|
| MCP server | `sudo journalctl -u odoo-semantic-mcp -f` |
| Indexer (cron) | `tail -f /var/log/odoo-semantic-reindex.log` |
| Neo4j | `docker compose logs -f neo4j` |
| PostgreSQL | `docker compose logs -f postgres` |
| Nginx | `/var/log/nginx/error.log` |

### Restart / Reload

```bash
# MCP server (không ảnh hưởng DB)
sudo systemctl restart odoo-semantic-mcp

# Sau khi index xong
sudo systemctl status odoo-semantic-mcp   # verify vẫn running

# DB restart (hiếm khi cần)
docker compose restart neo4j
docker compose restart postgres
```

---

## 7. Security Checklist

Trước khi expose public internet:

- [ ] `.env` và `odoo-semantic.conf` có quyền `600`, owner `odoo-semantic`
- [ ] `NEO4J_PASSWORD` và `PG_PASSWORD` không phải default `password`
- [ ] Neo4j và PG ports bind `127.0.0.1` (kiểm tra `docker compose ps` — cột Ports)
- [ ] MCP server bind `127.0.0.1` (kiểm tra `odoo-semantic.conf [server] host`)
- [ ] TLS cert valid + auto-renewing (certbot timer: `systemctl status certbot.timer` hoặc Caddy auto)
- [ ] Auth option đã chọn (IP allowlist / Basic Auth)
- [ ] Service user `odoo-semantic` là non-login (`shell=/usr/sbin/nologin`)
- [ ] Backup đã được test (restore thử ít nhất 1 lần)

---

## 8. Split-Tier Migration

Khi cần tách DB ra VM riêng (≥80 users, hoặc HA):

1. Move `docker-compose.yml` và `.env` sang DB VM.
2. Đổi ports binding từ `127.0.0.1:7687:7687` → `0.0.0.0:7687:7687`.
3. Cấu hình firewall DB VM: chỉ cho phép app VM IP kết nối port 7687 và 5432.
4. Set `NEO4J_ADVERTISED_HOST=<DB-VM-public-IP>` trong `.env` (bắt buộc — bolt client dùng advertised address để redirect).
5. Trên App VM: cập nhật `odoo-semantic.conf`:
   ```ini
   [database]
   neo4j_uri = bolt://<DB-VM-IP>:7687
   pg_dsn    = postgresql://odoo_semantic:<pass>@<DB-VM-IP>:5432/odoo_semantic
   ```
6. `sudo systemctl restart odoo-semantic-mcp`
7. Smoke test (§5).
````

- [ ] **Step 2: Verify không có placeholder**

Đọc lại toàn bộ `docs/deploy.md`. Search các pattern sau — không được có:
- `TBD`, `TODO`, `<your-value>`, `...`
- `fill in`, `add here`, `implement later`

Duy nhất được phép có `<strong-password>`, `<NEO4J_PASSWORD>`, `<PG_PASSWORD>`, `<DB-VM-IP>` vì đây là *user-specific values* phải điền theo môi trường thật.

- [ ] **Step 3: Commit**

```bash
git add docs/deploy.md
git commit -m "docs: add docs/deploy.md — production deploy guide (DB/App/Proxy tiers)"
```

---

## Task 7: Cross-references — README, CONTRIBUTING, TASKS, thiet-ke-kien-truc

**Context:** `docs/deploy.md` là SSOT cho deploy content. Cần trim README để không duplicate; thêm link từ mọi entry point liên quan. Quy tắc: nội dung ở `docs/deploy.md` thì README/CONTRIBUTING chỉ được link, không được copy.

**Files:**
- Modify: `README.md`
- Modify: `CONTRIBUTING.md`
- Modify: `TASKS.md`
- Modify: `docs/thiet-ke-kien-truc.md`

- [ ] **Step 1: Trim README.md — section "Deploy Server (Admin)"**

Tìm section `## Deploy Server (Admin)` trong `README.md` (hiện ~40 dòng bao gồm codeblock 6-bước + Reverse proxy note + Backup comment).

Thay toàn bộ section đó bằng:

```markdown
## Deploy Server (Admin)

```bash
git clone https://github.com/Viindoo/odoo-semantic-mcp
cd odoo-semantic-mcp
make install                                           # tạo venv + config templates
docker compose up -d                                   # start Neo4j + PostgreSQL
~/.venv/odoo-semantic-mcp/bin/python -m src.db.migrate # bootstrap schema
~/.venv/odoo-semantic-mcp/bin/python -m src.manager add-profile viindoo_17 --version 17.0
~/.venv/odoo-semantic-mcp/bin/python -m src.indexer --profile viindoo_17
~/.venv/odoo-semantic-mcp/bin/python -m src.mcp.server  # start MCP server
```

→ Xem [`docs/deploy.md`](docs/deploy.md) để biết cách cấu hình từng tier (DB, App, Proxy), systemd service, nginx/Caddy, TLS, backup, security checklist.
```

- [ ] **Step 2: Thêm docs/deploy.md vào bảng "Tài Liệu" trong README.md**

Tìm bảng `## Tài Liệu` trong README.md. Thêm dòng:

```markdown
| [`docs/deploy.md`](docs/deploy.md) | **Admin deploy guide** — DB tier, App tier, Nginx/Caddy, systemd, TLS, backup |
```

Đặt trước dòng `CONTRIBUTING.md` (deploy là bước trước dev).

- [ ] **Step 3: Thêm dòng vào CONTRIBUTING.md**

Tìm table "Tài Liệu Liên Quan" ở cuối `CONTRIBUTING.md`. Thêm:

```markdown
| [`docs/deploy.md`](docs/deploy.md) | Production deploy guide — cho admin, không phải dev |
```

- [ ] **Step 4: Cập nhật TASKS.md — thêm task deploy docs dưới M2.5**

Tìm dòng `- [ ] E2E manual: clone Odoo 17 → register → index → Claude Code call 4 tools` trong section `## Milestone 2.5`. Thêm dòng mới ngay trên dòng đó:

```markdown
- [x] `docs/deploy.md`: production deploy guide — DB / App / Proxy tiers
```

(Đánh `[x]` vì task này sẽ được hoàn thành trong plan này.)

- [ ] **Step 5: Cập nhật docs/thiet-ke-kien-truc.md**

Tìm bảng `## Điều Hướng Tài Liệu` ở cuối file. Thêm dòng:

```markdown
| ↓ | [`docs/deploy.md`](deploy.md) | Production deploy: DB tier, App tier, Nginx/Caddy, systemd |
```

- [ ] **Step 6: Verify SSOT — không có nội dung duplicate**

Kiểm tra README.md "Deploy Server" section mới chỉ có 6-bước quick-start + 1 link. Không có systemd config, nginx config, hay security checklist nào trong README.

```bash
grep -n "systemd\|nginx\|caddy\|certbot\|security" README.md
# Expected: 0 matches (hoặc chỉ trong link text)
```

- [ ] **Step 7: Chạy unit tests lần cuối**

```bash
make test
```

Expected: tất cả PASS.

- [ ] **Step 8: Commit**

```bash
git add README.md CONTRIBUTING.md TASKS.md docs/thiet-ke-kien-truc.md
git commit -m "docs: cross-reference docs/deploy.md from README/CONTRIBUTING/TASKS/arch"
```

---

## Self-Review

### 1. Spec coverage

| Yêu cầu | Task | Covered? |
|---------|------|---------|
| Q1: Remove load_dotenv, Option B | Task 1 | ✓ |
| Q2: Fix I2 (index_all abort) | Task 3 | ✓ |
| Q2: Fix I3 (path validation) | Task 2 | ✓ |
| Q3: docs/deploy.md | Task 6 | ✓ |
| Q3: Link từ README | Task 7 | ✓ |
| Q3: SSOT — no duplicate | Task 7 Step 6 | ✓ |
| Boil the Lake: FK index (M1) | Task 4 | ✓ |
| Boil the Lake: ADR note (M6) | Task 4 | ✓ |
| Boil the Lake: plan doc A4/A5 (M7) | Task 4 | ✓ |
| Deploy: DB tier | Task 6 §2 | ✓ |
| Deploy: App tier + systemd | Task 5 + Task 6 §3 | ✓ |
| Deploy: Proxy tier + nginx + Caddy | Task 5 + Task 6 §4 | ✓ |
| Deploy: Auth options | Task 6 §4.3 | ✓ |
| Deploy: Security checklist | Task 6 §7 | ✓ |
| Deploy: Backup | Task 6 §2.5 | ✓ |
| Deploy: Split-tier guide | Task 6 §8 | ✓ |
| Deploy: Operational runbook | Task 6 §6 | ✓ |

### 2. Placeholder scan

- `docs/deploy.md` dùng `<strong-password>`, `semantic.example.com`, `<DB-VM-IP>` — acceptable (user-specific values).
- Không có TBD, TODO, "fill in details", "implement later" trong code tasks.

### 3. Type consistency

- `index_all()` returns dict với keys: `profiles_ok`, `profiles_failed`, `modules`, `views`, `qweb` — được dùng nhất quán trong Task 3 (implementation) và Task 6 §3.3 (output example).
- `_neo4j_creds()` trong `pipeline.py` vs lazy `_get_driver()` trong `server.py` — hai functions riêng, không shared, nhưng cùng priority order: `NEO4J_* env → config → fallback`. Intentionally separate (server không import pipeline).
