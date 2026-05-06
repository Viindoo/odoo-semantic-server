# Milestone 2.5 — "Foundation Wow" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Bắt buộc khi implement:**
> - **Boil the Lake (ETHOS §4.1.1):** wire pipeline đầy đủ ngay — đừng để parser_xml/qweb "tạm chưa wire". Delta vài chục dòng.
> - **Keep it simple (ETHOS §4.1.3):** stdlib `configparser` + `argparse`. Không thêm Click, dynaconf, pydantic-settings.
> - **Tests trước code:** mỗi task = failing test → run đỏ → implement → run xanh → commit.

**Goal:** Sau M2.5, admin có thể: clone repos thật → register vào PostgreSQL registry → `python -m src.indexer --profile <name>` → MCP server đọc config từ `odoo-semantic.conf` → Claude Code gọi 4 MCP tools (`resolve_model/field/method/view`) trên data thật.

**Architecture:** Hai-tầng config (`.env` chỉ cho Docker, `odoo-semantic.conf` cho app Python) + PostgreSQL repo registry (bảng `profiles` + `repos`) + indexer pipeline đọc danh sách repos từ DB và chạy đủ 3 parsers (Python + XML + QWeb) → writer → Neo4j. Admin CLI `python -m src.manager` (argparse, dùng tạm cho M2.5, M5 sẽ thay bằng Web UI).

**Tech Stack:** Python 3.12 stdlib (`configparser`, `argparse`, `pathlib`) + đã có sẵn (`psycopg2-binary`, `neo4j`, `fastmcp`, `python-dotenv`). KHÔNG thêm dep mới.

---

## Post-Plan Adjustments (commits sau khi plan được viết)

Các thay đổi sau xảy ra trong quá trình implement, **sau** khi plan được viết và approve. Ghi lại để future agent không bị confuse bởi discrepancy giữa plan code vs code thật.

### A1. `index_profile` signature — commit `56aa74d`

**Plan (§Task 5):** `index_profile(pg_conn, neo4j_driver, *, profile_name)` — 3 args, `neo4j_driver` nhận từ caller.

**Thực tế:** `index_profile(pg_conn, *, profile_name)` — `neo4j_driver` bị drop vì Neo4jWriter tự đọc creds qua `_neo4j_creds()`. Caller không cần pass driver.

**Lý do:** Code review phát hiện `neo4j_driver` parameter là dead code — writer không dùng nó. Simplicity win.

**Fix tiếp theo (commit `2a5925e`):** Tạo `_neo4j_creds()` private helper, cả `open_production_neo4j()` và `index_profile()` đều dùng nó. Đọc `[database]/neo4j_uri` đúng section từ config file.

### A2. `open_production_neo4j()` caller — commit `2a5925e`

**Plan (§Task 6):** `__main__.py` import `open_production_neo4j` và gọi nó để build driver → pass vào `index_profile`.

**Thực tế:** `__main__.py` không gọi `open_production_neo4j()`. `index_profile()` tự lấy creds qua `_neo4j_creds()`. `open_production_neo4j()` vẫn exported (public utility) nhưng không được `__main__.py` dùng.

### A3. CI workflow + pyproject.toml — commit `22f4f60`

**Plan:** Không liệt kê `.github/workflows/ci.yml` hoặc `pyproject.toml` trong §Cấu Trúc File.

**Thực tế:** Cả hai được modify khi implement Task 2 (add postgres service container vào CI, add pytest marker `postgres`). Discovery khi integration test cần postgres service để chạy trong CI.

### A4. `.env.example` cleanup (F3/A2 fix)

`.env.example` ban đầu còn chứa `MCP_HOST`, `MCP_PORT`, `ODOO_REPOS_BASE_DIR` — các config đã được chuyển vào `odoo-semantic.conf`. Opus review (lần 1) phát hiện, đã xoá commit `c21ddda`. Default `MCP_HOST=0.0.0.0` cũng là security risk.

### A5. ADR-0001 schema evolution policy (B2)

Opus review yêu cầu formal documentation về schema evolution policy. Tạo `docs/adr/0001-schema-evolution-policy.md` commit `644465d` — add-only M2.5–M5, adopt migration tool (yoyo/Alembic) tại M6.

---

## Zero-Trust Audit Findings

**Verified vs spec:**
- C1 đúng: `scripts/index_test.py` tồn tại nhưng KHÔNG có entrypoint production; `src/cli.py` không tồn tại.
- C2 đúng: `parser_xml`, `parser_qweb` không được gọi từ pipeline production nào. Chỉ test gọi.
- C3 đúng: `src/mcp/server.py:246` hardcode `host="0.0.0.0", port=8002`. `MCP_HOST`/`MCP_PORT` trong `.env.example` đọc xong không dùng.
- C4 đúng: README quảng cáo `X-API-Key` header; codebase không validate gì. Defer M5.
- `psycopg2-binary>=2.9` đã có trong `pyproject.toml:12`. KHÔNG cần thêm dep.
- `parser_xml.parse_module()` trả về `ViewParseResult` (chỉ field `views` được fill).
- `parser_qweb.parse_module()` cũng trả về `ViewParseResult` (chỉ field `qweb` được fill).
- `writer_neo4j.Neo4jWriter.write_view_results(results: list[ViewParseResult])` đã tồn tại và xử lý cả `result.views` lẫn `result.qweb` trong cùng một transaction.
- `tests/conftest.py` có fixture `clean_neo4j` + `TEST_VERSION = "99.0"`. Integration test dùng `pytestmark = pytest.mark.neo4j`.
- `Makefile` đã có target `install` (dòng 23-25); spec gọi là "create" — thực tế là **modify** (extend).
- CI (`ci.yml`) hardcode `neo4j:5.26.25` ngoài dòng services — note đã ghi rõ trong CLAUDE.md.

**Discrepancies (spec sai, sửa lại):**
- Spec: "remove `index_test.py` from `.gitignore`". Thực tế `.gitignore` KHÔNG chứa `index_test.py`. Sửa: thêm `odoo-semantic.conf` (user secret) vào `.gitignore`, KHÔNG đụng `index_test.py`. Quyết định: giữ `scripts/index_test.py` như historic E2E script (đã được commit ở M1), nhưng pipeline production mới sẽ là `python -m src.indexer`.
- Spec gọi `src/manager/__init__.py` làm CLI. Thực tế Python idiom đúng cho `python -m src.manager` là **`src/manager/__main__.py`**. Plan dùng `__main__.py`.
- Spec gọi `src/db migrate` (không có `__main__.py`). Plan dùng `python -m src.db.migrate` (gọi trực tiếp module có `if __name__ == "__main__"`) — đơn giản hơn, không cần dispatcher.
- Spec ví dụ `parser_qweb.parse_module(path)` trả về `QWebParseResult` — sai, thực tế trả về `ViewParseResult` chia sẻ với `parser_xml`. Pipeline merge `views` + `qweb` vào một `ViewParseResult` per module.

**Plan adjustments:**
1. Task 4 dùng `src/manager/__main__.py` thay vì `__init__.py`.
2. Task 2 dùng `src/db/migrate.py` chạy trực tiếp (`python -m src.db.migrate`).
3. Task 5 merge XML+QWeb output vào một `ViewParseResult` per module (giảm số session round-trip Neo4j).
4. Task 10: `.gitignore` chỉ thêm `odoo-semantic.conf` (KHÔNG remove `index_test.py` — không có trong file).
5. Pin port trong `docker-compose.yml`: cả 7474, 7687, 5432 đều bind `127.0.0.1` cho same-server default. Comment nhắc admin sửa khi tách tier.

---

## Cấu Trúc File

```
src/
├── config.py                 -- CREATE: INI config reader (configparser)
├── db/
│   ├── __init__.py           -- CREATE: package marker (empty)
│   ├── migrate.py            -- CREATE: PG schema bootstrap (profiles + repos)
│   └── repo_registry.py      -- CREATE: CRUD profiles + repos
├── manager/
│   ├── __init__.py           -- CREATE: package marker (empty)
│   └── __main__.py           -- CREATE: admin CLI (argparse)
├── indexer/
│   ├── __main__.py           -- CREATE: indexer entrypoint (argparse)
│   └── pipeline.py           -- CREATE: wire scanner→registry→resolver→3 parsers→writer
└── mcp/
    └── server.py             -- MODIFY: read host/port from src.config

odoo-semantic.conf.example    -- CREATE: app config template
.gitignore                    -- MODIFY: add odoo-semantic.conf
docker-compose.yml            -- MODIFY: bind ports 127.0.0.1; split-tier comment
Makefile                      -- MODIFY: extend install target (cp configs + docker up + migrate)
README.md                     -- MODIFY: real deploy steps + reverse proxy note
CONTRIBUTING.md               -- MODIFY: source tree section adds parser_xml/qweb + db + manager
TASKS.md                      -- MODIFY: add M2.5 section + update M5

tests/
├── test_config.py            -- CREATE: unit tests for src.config
├── test_db_migrate.py        -- CREATE: integration test (PostgreSQL marker)
├── test_db_repo_registry.py  -- CREATE: integration test (PostgreSQL marker)
├── test_manager_cli.py       -- CREATE: integration test (PostgreSQL marker)
├── test_indexer_pipeline.py  -- CREATE: integration test (neo4j + postgres marker)
└── conftest.py               -- MODIFY: add postgres fixtures (pg_conn, clean_pg)
```

---

## Task 1: `src/config.py` — INI config reader

**Files:**
- Create: `src/config.py`
- Create: `tests/test_config.py`

- [ ] **Bước 1: Viết failing test**

Tạo `tests/test_config.py`:

```python
"""Unit tests for src.config — INI reader, no DB needed."""
import textwrap
from pathlib import Path

import pytest

from src import config as config_mod


@pytest.fixture(autouse=True)
def reset_config_cache():
    """src.config caches the parser at module level — reset before/after each test."""
    config_mod._conf = None
    yield
    config_mod._conf = None


def test_reads_from_explicit_path(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text(textwrap.dedent("""
        [database]
        neo4j_uri = bolt://1.2.3.4:7687
        neo4j_user = neo
        neo4j_password = secret

        [server]
        host = 127.0.0.1
        port = 8002
    """).strip())
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))

    assert config_mod.get("database", "neo4j_uri") == "bolt://1.2.3.4:7687"
    assert config_mod.get("server", "port") == "8002"


def test_fallback_when_key_missing(tmp_path, monkeypatch):
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text("[server]\nhost = 127.0.0.1\n")
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
    assert config_mod.get("server", "port", fallback="8002") == "8002"


def test_fallback_when_section_missing(tmp_path, monkeypatch):
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text("[other]\nkey = val\n")
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
    assert config_mod.get("server", "port", fallback="8002") == "8002"


def test_missing_file_returns_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(tmp_path / "nope.conf"))
    assert config_mod.get("server", "host", fallback="0.0.0.0") == "0.0.0.0"


def test_searches_repo_local_when_no_env(tmp_path, monkeypatch):
    """Without ODOO_SEMANTIC_CONF, falls back to ./odoo-semantic.conf in cwd."""
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text("[server]\nhost = repo-local\n")
    monkeypatch.delenv("ODOO_SEMANTIC_CONF", raising=False)
    monkeypatch.chdir(tmp_path)
    # Override HOME so home-dir lookup misses
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    assert config_mod.get("server", "host", fallback="X") == "repo-local"
```

- [ ] **Bước 2: Run test — verify đỏ**

```bash
~/.venv/odoo-semantic-mcp/bin/pytest tests/test_config.py -v
```
Expect: `ModuleNotFoundError: No module named 'src.config'`.

- [ ] **Bước 3: Implement `src/config.py`**

```python
# src/config.py
"""INI config reader for odoo-semantic-mcp.

Search order:
  1. $ODOO_SEMANTIC_CONF (explicit override)
  2. ~/.odoo-semantic/odoo-semantic.conf (system-wide user config)
  3. ./odoo-semantic.conf (repo-local, dev convenience)

Returns fallback if nothing matches. No env-var fallback at lookup time —
callers pass `fallback=...` explicitly per key.
"""
import configparser
import os
import pathlib

_conf: configparser.ConfigParser | None = None


def _load() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    candidates: list[pathlib.Path] = []
    env_override = os.getenv("ODOO_SEMANTIC_CONF")
    if env_override:
        candidates.append(pathlib.Path(env_override))
    candidates.append(pathlib.Path.home() / ".odoo-semantic" / "odoo-semantic.conf")
    candidates.append(pathlib.Path.cwd() / "odoo-semantic.conf")
    for path in candidates:
        if path.is_file():
            parser.read(path)
            break
    return parser


def get(section: str, key: str, fallback: str | None = None) -> str | None:
    """Return string value for [section]/key, or fallback if missing."""
    global _conf
    if _conf is None:
        _conf = _load()
    return _conf.get(section, key, fallback=fallback)
```

- [ ] **Bước 4: Run test — verify xanh**

```bash
~/.venv/odoo-semantic-mcp/bin/pytest tests/test_config.py -v
```
Expect: 5 passed.

- [ ] **Bước 5: Commit**

```bash
git add src/config.py tests/test_config.py
git commit -m "feat(m2.5): src/config.py — INI config reader with search order"
```

---

## Task 2: PostgreSQL schema migration + repo registry CRUD

**Files:**
- Create: `src/db/__init__.py`
- Create: `src/db/migrate.py`
- Create: `src/db/repo_registry.py`
- Modify: `tests/conftest.py` — thêm postgres fixture
- Create: `tests/test_db_migrate.py`
- Create: `tests/test_db_repo_registry.py`
- Modify: `pyproject.toml` — đăng ký marker `postgres`

- [ ] **Bước 1: Đăng ký marker postgres trong `pyproject.toml`**

Sửa `[tool.pytest.ini_options]` markers list — thêm dòng `postgres`:

```toml
markers = [
    "neo4j: integration tests yêu cầu Neo4j đang chạy (skip bằng -m 'not neo4j')",
    "postgres: integration tests yêu cầu PostgreSQL đang chạy (skip bằng -m 'not postgres')",
]
```

Đồng thời cập nhật `Makefile`:
- Sửa `test-unit` thành: `$(PYTEST) tests/ -v -m "not neo4j and not postgres" --tb=short`
- Sửa `test-integration` thành: `$(PYTEST) tests/ -v -m "neo4j or postgres" --tb=short -rs`

- [ ] **Bước 2: Thêm postgres fixtures vào `tests/conftest.py`**

Append vào cuối file:

```python
# --- PostgreSQL fixtures (for src/db tests) ---

PG_TEST_DSN = os.getenv(
    "PG_TEST_DSN",
    "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic",
)


@pytest.fixture(scope="session")
def pg_conn():
    """Session-scoped PostgreSQL connection.

    Skip with explicit message if not reachable. Tests in CI rely on
    a `postgres` service container; local dev uses `docker compose up -d postgres`.
    """
    import psycopg2
    try:
        conn = psycopg2.connect(PG_TEST_DSN)
    except Exception as e:
        pytest.skip(f"PostgreSQL not reachable at {PG_TEST_DSN}: {e}")
    conn.autocommit = True
    yield conn
    conn.close()


@pytest.fixture
def clean_pg(pg_conn):
    """Drop test tables before and after each test (idempotent)."""
    with pg_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS repos CASCADE")
        cur.execute("DROP TABLE IF EXISTS profiles CASCADE")
    yield pg_conn
    with pg_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS repos CASCADE")
        cur.execute("DROP TABLE IF EXISTS profiles CASCADE")
```

- [ ] **Bước 3: Viết failing test cho migrate**

Tạo `tests/test_db_migrate.py`:

```python
"""Integration tests for src.db.migrate — requires PostgreSQL."""
import pytest

from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres


def test_migrate_creates_profiles_table(clean_pg):
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'profiles' ORDER BY ordinal_position
        """)
        cols = [r[0] for r in cur.fetchall()]
    assert cols == ["id", "name", "odoo_version", "description", "created_at"]


def test_migrate_creates_repos_table(clean_pg):
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'repos' ORDER BY ordinal_position
        """)
        cols = [r[0] for r in cur.fetchall()]
    assert "profile_id" in cols
    assert "url" in cols
    assert "branch" in cols
    assert "local_path" in cols
    assert "status" in cols


def test_migrate_is_idempotent(clean_pg):
    """Running migrate twice must not fail."""
    run_migrations(clean_pg)
    run_migrations(clean_pg)  # second call: no error


def test_repos_unique_constraint_on_url_branch(clean_pg):
    import psycopg2.errors
    run_migrations(clean_pg)
    with clean_pg.cursor() as cur:
        cur.execute(
            "INSERT INTO profiles (name, odoo_version) VALUES ('p1', '17.0') RETURNING id"
        )
        pid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO repos (profile_id, url, branch, local_path) "
            "VALUES (%s, 'github.com/x/y', '17.0', '/tmp/y')",
            (pid,),
        )
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO repos (profile_id, url, branch, local_path) "
                "VALUES (%s, 'github.com/x/y', '17.0', '/tmp/other')",
                (pid,),
            )
```

- [ ] **Bước 4: Run test — verify đỏ**

```bash
~/.venv/odoo-semantic-mcp/bin/pytest tests/test_db_migrate.py -v -m postgres
```
Expect: `ModuleNotFoundError: No module named 'src.db'`.

- [ ] **Bước 5: Implement `src/db/__init__.py` (empty marker)**

```python
# src/db/__init__.py
```

- [ ] **Bước 6: Implement `src/db/migrate.py`**

```python
# src/db/migrate.py
"""PostgreSQL schema bootstrap — run once before first indexing.

Usage:
    python -m src.db.migrate

Reads PG DSN from `odoo-semantic.conf` [database].pg_dsn. Idempotent: uses
CREATE TABLE IF NOT EXISTS so re-running is safe.
"""
import sys

import psycopg2

from src import config

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
"""


def run_migrations(conn) -> None:
    """Execute schema DDL on an open psycopg2 connection."""
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    if not conn.autocommit:
        conn.commit()


def main() -> int:
    dsn = config.get(
        "database", "pg_dsn",
        fallback="postgresql://odoo_semantic:password@localhost:5432/odoo_semantic",
    )
    conn = psycopg2.connect(dsn)
    try:
        run_migrations(conn)
        print(f"✓ Migrations applied to {dsn}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Bước 7: Run migrate test — verify xanh**

```bash
docker compose up -d postgres
~/.venv/odoo-semantic-mcp/bin/pytest tests/test_db_migrate.py -v -m postgres
```
Expect: 4 passed.

- [ ] **Bước 8: Viết failing test cho repo_registry**

Tạo `tests/test_db_repo_registry.py`:

```python
"""Integration tests for src.db.repo_registry CRUD."""
import pytest

from src.db.migrate import run_migrations
from src.db.repo_registry import (
    add_profile, add_repo, list_profiles, list_repos, get_repos_for_profile,
    update_repo_status,
)

pytestmark = pytest.mark.postgres


@pytest.fixture
def migrated_pg(clean_pg):
    run_migrations(clean_pg)
    return clean_pg


def test_add_and_list_profile(migrated_pg):
    pid = add_profile(migrated_pg, name="viindoo_17", odoo_version="17.0")
    assert pid > 0
    profiles = list_profiles(migrated_pg)
    assert len(profiles) == 1
    assert profiles[0]["name"] == "viindoo_17"
    assert profiles[0]["odoo_version"] == "17.0"


def test_add_repo_under_profile(migrated_pg):
    pid = add_profile(migrated_pg, name="viindoo_17", odoo_version="17.0")
    rid = add_repo(
        migrated_pg, profile_id=pid,
        url="github.com/odoo/odoo", branch="17.0",
        local_path="/home/user/git/odoo_17.0",
    )
    assert rid > 0
    repos = get_repos_for_profile(migrated_pg, profile_name="viindoo_17")
    assert len(repos) == 1
    assert repos[0]["url"] == "github.com/odoo/odoo"
    assert repos[0]["status"] == "pending"


def test_list_repos_returns_all(migrated_pg):
    pid = add_profile(migrated_pg, "p1", "17.0")
    add_repo(migrated_pg, pid, "github.com/a/b", "17.0", "/tmp/a")
    add_repo(migrated_pg, pid, "github.com/c/d", "17.0", "/tmp/c")
    repos = list_repos(migrated_pg)
    assert len(repos) == 2


def test_update_repo_status(migrated_pg):
    pid = add_profile(migrated_pg, "p1", "17.0")
    rid = add_repo(migrated_pg, pid, "github.com/a/b", "17.0", "/tmp/a")
    update_repo_status(migrated_pg, rid, status="indexed")
    repos = list_repos(migrated_pg)
    assert repos[0]["status"] == "indexed"
    assert repos[0]["last_indexed_at"] is not None


def test_update_repo_status_with_error(migrated_pg):
    pid = add_profile(migrated_pg, "p1", "17.0")
    rid = add_repo(migrated_pg, pid, "github.com/a/b", "17.0", "/tmp/a")
    update_repo_status(migrated_pg, rid, status="error", error_msg="boom")
    repos = list_repos(migrated_pg)
    assert repos[0]["status"] == "error"
    assert repos[0]["error_msg"] == "boom"


def test_get_repos_for_unknown_profile_returns_empty(migrated_pg):
    assert get_repos_for_profile(migrated_pg, profile_name="nope") == []
```

- [ ] **Bước 9: Run test — verify đỏ** (`ModuleNotFoundError: src.db.repo_registry`).

- [ ] **Bước 10: Implement `src/db/repo_registry.py`**

```python
# src/db/repo_registry.py
"""CRUD for profiles + repos in PostgreSQL.

All functions take an open psycopg2 connection (autocommit-friendly). M2.5
admin CLI (`src.manager`) opens one connection and passes it down.
"""
from psycopg2.extras import RealDictCursor


def add_profile(conn, name: str, odoo_version: str, description: str = "") -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO profiles (name, odoo_version, description) "
            "VALUES (%s, %s, %s) RETURNING id",
            (name, odoo_version, description),
        )
        return cur.fetchone()[0]


def list_profiles(conn) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM profiles ORDER BY id")
        return [dict(r) for r in cur.fetchall()]


def add_repo(
    conn, profile_id: int, url: str, branch: str, local_path: str
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO repos (profile_id, url, branch, local_path) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (profile_id, url, branch, local_path),
        )
        return cur.fetchone()[0]


def list_repos(conn) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT r.*, p.name AS profile_name, p.odoo_version
            FROM repos r LEFT JOIN profiles p ON r.profile_id = p.id
            ORDER BY r.id
        """)
        return [dict(r) for r in cur.fetchall()]


def get_repos_for_profile(conn, profile_name: str) -> list[dict]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT r.*, p.odoo_version
            FROM repos r JOIN profiles p ON r.profile_id = p.id
            WHERE p.name = %s ORDER BY r.id
        """, (profile_name,))
        return [dict(r) for r in cur.fetchall()]


def update_repo_status(
    conn, repo_id: int, status: str, error_msg: str | None = None
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE repos SET status = %s, error_msg = %s, "
            "last_indexed_at = CASE WHEN %s = 'indexed' THEN NOW() ELSE last_indexed_at END "
            "WHERE id = %s",
            (status, error_msg, status, repo_id),
        )
```

- [ ] **Bước 11: Run test — verify xanh.**

```bash
~/.venv/odoo-semantic-mcp/bin/pytest tests/test_db_repo_registry.py -v -m postgres
```
Expect: 6 passed.

- [ ] **Bước 12: Commit**

```bash
git add src/db/ tests/test_db_migrate.py tests/test_db_repo_registry.py \
        tests/conftest.py pyproject.toml Makefile
git commit -m "feat(m2.5): src/db — PostgreSQL profiles+repos schema and CRUD"
```

---

## Task 3: `odoo-semantic.conf.example` template

**Files:**
- Create: `odoo-semantic.conf.example`

- [ ] **Bước 1: Tạo file**

```ini
# odoo-semantic.conf.example
# Copy → odoo-semantic.conf rồi điền giá trị thật.
# File `odoo-semantic.conf` thật KHÔNG commit (đã trong .gitignore).
#
# Các path tìm config khi runtime (theo thứ tự):
#   1. $ODOO_SEMANTIC_CONF (override khi cần)
#   2. ~/.odoo-semantic/odoo-semantic.conf
#   3. ./odoo-semantic.conf  (repo-local, dev convenience)

[database]
neo4j_uri = bolt://localhost:7687
neo4j_user = neo4j
neo4j_password =                                ; bắt buộc điền

pg_dsn = postgresql://odoo_semantic:password@localhost:5432/odoo_semantic

[server]
# Bind localhost — reverse proxy phía trước handle external + auth (M5).
host = 127.0.0.1
port = 8002

[indexer]
# Fallback khi `repos.local_path` chưa được set trong PostgreSQL.
repos_base_dir = /home/user/git
```

- [ ] **Bước 2: Commit (pair với Task 10)** — sẽ commit cùng `.gitignore` update.

---

## Task 4: `src/manager/__main__.py` — admin CLI

**Files:**
- Create: `src/manager/__init__.py` (empty marker)
- Create: `src/manager/__main__.py`
- Create: `tests/test_manager_cli.py`

- [ ] **Bước 1: Viết failing test**

Tạo `tests/test_manager_cli.py`:

```python
"""Integration tests for `python -m src.manager` CLI."""
import subprocess
import sys

import pytest

from src.db.migrate import run_migrations

pytestmark = pytest.mark.postgres


@pytest.fixture
def migrated_pg(clean_pg):
    run_migrations(clean_pg)
    return clean_pg


def _run(args: list[str], env_extra: dict | None = None) -> subprocess.CompletedProcess:
    import os
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "src.manager", *args],
        capture_output=True, text=True, env=env,
    )


def test_add_profile_prints_id(migrated_pg, monkeypatch, tmp_path):
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text(
        "[database]\npg_dsn = "
        "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic\n"
    )
    res = _run(
        ["add-profile", "viindoo_17", "--version", "17.0"],
        env_extra={"ODOO_SEMANTIC_CONF": str(cfg)},
    )
    assert res.returncode == 0, res.stderr
    assert "viindoo_17" in res.stdout

    with migrated_pg.cursor() as cur:
        cur.execute("SELECT name FROM profiles")
        rows = [r[0] for r in cur.fetchall()]
    assert "viindoo_17" in rows


def test_add_repo_attaches_to_profile(migrated_pg, tmp_path):
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text(
        "[database]\npg_dsn = "
        "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic\n"
    )
    env = {"ODOO_SEMANTIC_CONF": str(cfg)}

    _run(["add-profile", "viindoo_17", "--version", "17.0"], env_extra=env)
    res = _run([
        "add-repo", "--profile", "viindoo_17",
        "--url", "github.com/odoo/odoo", "--branch", "17.0",
        "--local-path", "/home/user/git/odoo_17.0",
    ], env_extra=env)
    assert res.returncode == 0, res.stderr

    with migrated_pg.cursor() as cur:
        cur.execute("SELECT url FROM repos")
        rows = [r[0] for r in cur.fetchall()]
    assert "github.com/odoo/odoo" in rows


def test_list_shows_profile_and_repo(migrated_pg, tmp_path):
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text(
        "[database]\npg_dsn = "
        "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic\n"
    )
    env = {"ODOO_SEMANTIC_CONF": str(cfg)}
    _run(["add-profile", "viindoo_17", "--version", "17.0"], env_extra=env)
    _run([
        "add-repo", "--profile", "viindoo_17",
        "--url", "github.com/x/y", "--branch", "17.0",
        "--local-path", "/tmp/y",
    ], env_extra=env)
    res = _run(["list"], env_extra=env)
    assert res.returncode == 0
    assert "viindoo_17" in res.stdout
    assert "github.com/x/y" in res.stdout


def test_add_profile_unknown_subcommand_exits_nonzero(tmp_path, migrated_pg):
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text(
        "[database]\npg_dsn = "
        "postgresql://odoo_semantic:password@localhost:5432/odoo_semantic\n"
    )
    res = _run(["nope-cmd"], env_extra={"ODOO_SEMANTIC_CONF": str(cfg)})
    assert res.returncode != 0
```

- [ ] **Bước 2: Run — verify đỏ** (`No module named src.manager`).

- [ ] **Bước 3: Implement `src/manager/__init__.py`**

```python
# src/manager/__init__.py
```

- [ ] **Bước 4: Implement `src/manager/__main__.py`**

```python
# src/manager/__main__.py
"""Admin CLI for profiles + repos. M2.5 only — replaced by Web UI in M5.

Usage:
    python -m src.manager add-profile NAME --version VERSION [--description TEXT]
    python -m src.manager add-repo --profile NAME --url URL --branch BRANCH \
                                   --local-path PATH
    python -m src.manager list
"""
import argparse
import sys

import psycopg2

from src import config
from src.db import repo_registry


def _open_conn() -> psycopg2.extensions.connection:
    dsn = config.get(
        "database", "pg_dsn",
        fallback="postgresql://odoo_semantic:password@localhost:5432/odoo_semantic",
    )
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    return conn


def _cmd_add_profile(args, conn) -> int:
    pid = repo_registry.add_profile(
        conn, name=args.name, odoo_version=args.version,
        description=args.description or "",
    )
    print(f"✓ Profile '{args.name}' (id={pid}) odoo_version={args.version}")
    return 0


def _cmd_add_repo(args, conn) -> int:
    profiles = [p for p in repo_registry.list_profiles(conn) if p["name"] == args.profile]
    if not profiles:
        print(f"✗ Profile '{args.profile}' not found. Run add-profile first.",
              file=sys.stderr)
        return 2
    rid = repo_registry.add_repo(
        conn, profile_id=profiles[0]["id"],
        url=args.url, branch=args.branch, local_path=args.local_path,
    )
    print(f"✓ Repo (id={rid}) {args.url}@{args.branch} → {args.local_path}")
    return 0


def _cmd_list(_args, conn) -> int:
    profiles = repo_registry.list_profiles(conn)
    if not profiles:
        print("(no profiles yet — run: python -m src.manager add-profile <name> "
              "--version <ver>)")
        return 0
    for p in profiles:
        print(f"[{p['name']}] odoo_version={p['odoo_version']}")
        repos = repo_registry.get_repos_for_profile(conn, profile_name=p["name"])
        if not repos:
            print("    (no repos)")
            continue
        for r in repos:
            print(f"    - {r['url']}@{r['branch']} → {r['local_path']} "
                  f"[{r['status']}]")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.manager")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add-profile", help="Register a new profile")
    p_add.add_argument("name")
    p_add.add_argument("--version", required=True, help="e.g. 17.0")
    p_add.add_argument("--description", default="")

    p_repo = sub.add_parser("add-repo", help="Attach a repo to a profile")
    p_repo.add_argument("--profile", required=True)
    p_repo.add_argument("--url", required=True)
    p_repo.add_argument("--branch", required=True)
    p_repo.add_argument("--local-path", required=True, dest="local_path")

    sub.add_parser("list", help="List all profiles + their repos")

    args = parser.parse_args(argv)
    conn = _open_conn()
    try:
        return {
            "add-profile": _cmd_add_profile,
            "add-repo": _cmd_add_repo,
            "list": _cmd_list,
        }[args.cmd](args, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Bước 5: Run — verify xanh.**

```bash
~/.venv/odoo-semantic-mcp/bin/pytest tests/test_manager_cli.py -v -m postgres
```
Expect: 4 passed.

- [ ] **Bước 6: Commit**

```bash
git add src/manager/ tests/test_manager_cli.py
git commit -m "feat(m2.5): src/manager — admin CLI for profiles+repos (argparse)"
```

---

## Task 5: Wire `parser_xml` + `parser_qweb` into `src/indexer/pipeline.py`

**Files:**
- Create: `src/indexer/pipeline.py`
- Create: `tests/test_indexer_pipeline.py`

**Note:** đây là blocker chính của M2 — parsers tồn tại + writer tồn tại nhưng không có pipeline gọi chúng. Pipeline mới đọc danh sách repos từ PostgreSQL, chạy 3 parsers (Python + XML + QWeb) cho mỗi module, và gọi writer.

- [ ] **Bước 1: Viết failing test**

Tạo `tests/test_indexer_pipeline.py`:

```python
"""End-to-end pipeline test — needs both Neo4j and PostgreSQL."""
import textwrap
from pathlib import Path

import pytest

from src.db.migrate import run_migrations
from src.db.repo_registry import add_profile, add_repo
from src.indexer.pipeline import index_profile
from tests.conftest import TEST_VERSION, make_git_repo, make_manifest

pytestmark = [pytest.mark.neo4j, pytest.mark.postgres]


def _seed_module(repo: Path, name: str) -> None:
    """Create a single Odoo module under repo/<name>."""
    module = repo / name
    make_manifest(module, name=name, version=f"{TEST_VERSION}.1.0.0", depends=[])
    (module / "models").mkdir()
    (module / "models" / "__init__.py").write_text("")
    (module / "models" / f"{name}.py").write_text(textwrap.dedent(f"""
        from odoo import models, fields

        class FooModel(models.Model):
            _name = '{name}.foo'
            x = fields.Char()
    """).strip())
    (module / "views").mkdir()
    (module / "views" / "views.xml").write_text(textwrap.dedent(f"""
        <?xml version="1.0"?>
        <odoo>
            <record id="view_{name}_form" model="ir.ui.view">
                <field name="name">{name}.form</field>
                <field name="model">{name}.foo</field>
                <field name="arch" type="xml"><form/></field>
            </record>
            <template id="{name}_portal_tmpl"><div/></template>
        </odoo>
    """).strip())


def test_pipeline_writes_models_views_qweb_to_neo4j(
    clean_neo4j, clean_pg, neo4j_driver, tmp_path
):
    # 1. seed PostgreSQL: profile + 1 repo with TEST_VERSION branch
    run_migrations(clean_pg)
    repo = make_git_repo(tmp_path / "repo_test", branch=TEST_VERSION)
    _seed_module(repo, "demo_mod")
    pid = add_profile(clean_pg, name="test_prof", odoo_version=TEST_VERSION)
    add_repo(clean_pg, profile_id=pid, url="local/test", branch=TEST_VERSION,
             local_path=str(repo))

    # 2. run pipeline
    summary = index_profile(clean_pg, neo4j_driver, profile_name="test_prof")
    assert summary["modules"] >= 1
    assert summary["views"] >= 1
    assert summary["qweb"] >= 1

    # 3. verify Neo4j has Model + View + QWebTmpl
    with neo4j_driver.session() as session:
        model_rec = session.run(
            "MATCH (m:Model {name: 'demo_mod.foo', odoo_version: $v}) RETURN m",
            v=TEST_VERSION
        ).single()
        view_rec = session.run(
            "MATCH (v:View {xmlid: 'demo_mod.view_demo_mod_form', odoo_version: $v}) "
            "RETURN v", v=TEST_VERSION
        ).single()
        qweb_rec = session.run(
            "MATCH (t:QWebTmpl {xmlid: 'demo_mod.demo_mod_portal_tmpl', "
            "odoo_version: $v}) RETURN t", v=TEST_VERSION
        ).single()
    assert model_rec is not None, "pipeline must write Model node from parser_python"
    assert view_rec is not None, "pipeline must write View node from parser_xml"
    assert qweb_rec is not None, "pipeline must write QWebTmpl node from parser_qweb"


def test_pipeline_marks_repo_indexed_on_success(
    clean_neo4j, clean_pg, neo4j_driver, tmp_path
):
    run_migrations(clean_pg)
    repo = make_git_repo(tmp_path / "repo_ok", branch=TEST_VERSION)
    _seed_module(repo, "ok_mod")
    pid = add_profile(clean_pg, "test_prof", TEST_VERSION)
    add_repo(clean_pg, pid, "local/ok", TEST_VERSION, str(repo))

    index_profile(clean_pg, neo4j_driver, profile_name="test_prof")

    with clean_pg.cursor() as cur:
        cur.execute("SELECT status, last_indexed_at FROM repos")
        rows = cur.fetchall()
    assert rows[0][0] == "indexed"
    assert rows[0][1] is not None


def test_pipeline_index_all_iterates_every_profile(
    clean_neo4j, clean_pg, neo4j_driver, tmp_path
):
    from src.indexer.pipeline import index_all
    run_migrations(clean_pg)
    for prof in ("p_a", "p_b"):
        repo = make_git_repo(tmp_path / f"repo_{prof}", branch=TEST_VERSION)
        _seed_module(repo, f"mod_{prof}")
        pid = add_profile(clean_pg, prof, TEST_VERSION)
        add_repo(clean_pg, pid, f"local/{prof}", TEST_VERSION, str(repo))

    summary = index_all(clean_pg, neo4j_driver)
    assert summary["profiles"] == 2
    assert summary["modules"] >= 2
```

- [ ] **Bước 2: Run — verify đỏ** (`ModuleNotFoundError: src.indexer.pipeline`).

- [ ] **Bước 3: Implement `src/indexer/pipeline.py`**

```python
# src/indexer/pipeline.py
"""End-to-end indexer pipeline — reads repos from PostgreSQL, runs all parsers,
writes to Neo4j.

Flow:
    repos (PG) → scanner.scan_repos → registry.build_registry →
    resolver.topological_sort → for each module:
        parser_python.parse_module (→ ParseResult)
        parser_xml.parse_module    (→ ViewParseResult.views)
        parser_qweb.parse_module   (→ ViewParseResult.qweb)
    writer_neo4j.write_results + write_view_results
"""
import logging
import os

from src import config
from src.db import repo_registry
from src.indexer import parser_python, parser_qweb, parser_xml
from src.indexer.models import ViewParseResult
from src.indexer.registry import build_registry
from src.indexer.resolver import topological_sort
from src.indexer.scanner import scan_repos
from src.indexer.writer_neo4j import Neo4jWriter

_logger = logging.getLogger(__name__)


def _writer_for(neo4j_driver) -> Neo4jWriter:
    """Build a Neo4jWriter that re-uses the test driver in pytest, or opens
    a fresh one in production."""
    w = Neo4jWriter.__new__(Neo4jWriter)
    w.driver = neo4j_driver
    return w


def _index_one_profile(pg_conn, neo4j_driver, profile_name: str) -> dict:
    """Run pipeline for a single profile. Returns {modules, views, qweb} count."""
    repos = repo_registry.get_repos_for_profile(pg_conn, profile_name=profile_name)
    if not repos:
        _logger.warning("Profile %s has no repos — nothing to index", profile_name)
        return {"modules": 0, "views": 0, "qweb": 0}

    target_version = repos[0]["odoo_version"]
    repo_pairs = [(r["local_path"], target_version) for r in repos]

    pairs = scan_repos([str(p) for p, _ in repo_pairs])
    if not pairs:
        # Fall back to declared local_path even if scanner didn't pick it up
        # (test repos may not be detected by `scanner` if branch≠X.0 format).
        pairs = repo_pairs

    versioned = [(p, v) for p, v in pairs if v == target_version]
    registry = build_registry(versioned).get(target_version, {})
    order = topological_sort(registry)

    py_results = []
    view_results: list[ViewParseResult] = []
    for name in order:
        info = registry[name]
        py_results.append(parser_python.parse_module(info))
        xml_res = parser_xml.parse_module(info)
        qweb_res = parser_qweb.parse_module(info)
        merged = ViewParseResult(
            module=info, views=xml_res.views, qweb=qweb_res.qweb,
        )
        view_results.append(merged)

    writer = _writer_for(neo4j_driver)
    writer.setup_indexes()
    writer.write_results(py_results)
    writer.write_view_results(view_results)

    n_views = sum(len(r.views) for r in view_results)
    n_qweb = sum(len(r.qweb) for r in view_results)

    for r in repos:
        repo_registry.update_repo_status(pg_conn, r["id"], status="indexed")

    return {"modules": len(order), "views": n_views, "qweb": n_qweb}


def index_profile(pg_conn, neo4j_driver, profile_name: str) -> dict:
    """Public entry point — index one profile."""
    try:
        return _index_one_profile(pg_conn, neo4j_driver, profile_name)
    except Exception as e:
        _logger.exception("Pipeline failed for %s: %s", profile_name, e)
        for r in repo_registry.get_repos_for_profile(pg_conn, profile_name=profile_name):
            repo_registry.update_repo_status(
                pg_conn, r["id"], status="error", error_msg=str(e)[:500]
            )
        raise


def index_all(pg_conn, neo4j_driver) -> dict:
    """Iterate every profile and index sequentially."""
    profiles = repo_registry.list_profiles(pg_conn)
    total = {"profiles": 0, "modules": 0, "views": 0, "qweb": 0}
    for p in profiles:
        s = index_profile(pg_conn, neo4j_driver, profile_name=p["name"])
        total["profiles"] += 1
        total["modules"] += s["modules"]
        total["views"] += s["views"]
        total["qweb"] += s["qweb"]
    return total


def open_production_neo4j():
    """Used by `python -m src.indexer` to open a real Neo4j driver from config."""
    from neo4j import GraphDatabase
    return GraphDatabase.driver(
        config.get("database", "neo4j_uri", fallback="bolt://localhost:7687"),
        auth=(
            config.get("database", "neo4j_user", fallback="neo4j"),
            config.get("database", "neo4j_password", fallback=os.getenv("NEO4J_PASSWORD",
                                                                        "password")),
        ),
    )


def open_production_pg():
    """Used by `python -m src.indexer` to open a real PostgreSQL connection."""
    import psycopg2
    conn = psycopg2.connect(config.get(
        "database", "pg_dsn",
        fallback="postgresql://odoo_semantic:password@localhost:5432/odoo_semantic",
    ))
    conn.autocommit = True
    return conn
```

- [ ] **Bước 4: Run pipeline tests — verify xanh**

```bash
docker compose up -d neo4j postgres
~/.venv/odoo-semantic-mcp/bin/pytest tests/test_indexer_pipeline.py -v -m "neo4j or postgres"
```
Expect: 3 passed. (Lưu ý: `make_git_repo` trong conftest đã có sẵn — checkout branch tên `99.0` → scanner sẽ pick up vì regex `^\d+\.\d+$` match.)

- [ ] **Bước 5: Commit**

```bash
git add src/indexer/pipeline.py tests/test_indexer_pipeline.py
git commit -m "feat(m2.5): src/indexer/pipeline — wire parser_xml + parser_qweb"
```

---

## Task 6: `src/indexer/__main__.py` — entrypoint `python -m src.indexer`

**Files:**
- Create: `src/indexer/__main__.py`

**Note:** không thêm test riêng (đã cover bởi pipeline tests + manual smoke). CLI là thin wrapper, tránh test-cho-có.

- [ ] **Bước 1: Implement**

```python
# src/indexer/__main__.py
"""Run the indexer pipeline from the command line.

Usage:
    python -m src.indexer --profile viindoo_17
    python -m src.indexer --all
"""
import argparse
import logging
import sys

from src.indexer.pipeline import (
    index_all, index_profile, open_production_neo4j, open_production_pg,
)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="python -m src.indexer")
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--profile", help="Index one profile by name")
    grp.add_argument("--all", action="store_true", help="Index every registered profile")
    args = parser.parse_args(argv)

    pg = open_production_pg()
    driver = open_production_neo4j()
    try:
        if args.all:
            summary = index_all(pg, driver)
        else:
            summary = index_profile(pg, driver, profile_name=args.profile)
        print(f"✓ Indexer done: {summary}")
    finally:
        driver.close()
        pg.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Bước 2: Smoke test (manual)**

```bash
~/.venv/odoo-semantic-mcp/bin/python -m src.indexer --help
```
Expect: argparse usage banner.

- [ ] **Bước 3: Commit**

```bash
git add src/indexer/__main__.py
git commit -m "feat(m2.5): src/indexer/__main__.py — CLI entrypoint --profile/--all"
```

---

## Task 7: Fix `src/mcp/server.py` — read host/port from config

**Files:**
- Modify: `src/mcp/server.py`
- Modify: `tests/test_mcp_server.py` (add config-read test if pattern exists; otherwise unit test inline)
- Create: `tests/test_mcp_server_config.py`

- [ ] **Bước 1: Viết failing test**

Tạo `tests/test_mcp_server_config.py`:

```python
"""Unit test: server.py reads host/port from src.config (no MCP/Neo4j needed)."""
from src import config as config_mod


def test_server_module_uses_config_for_host_port(tmp_path, monkeypatch):
    cfg = tmp_path / "odoo-semantic.conf"
    cfg.write_text(
        "[server]\nhost = 192.168.42.7\nport = 8888\n"
        "[database]\nneo4j_uri = bolt://localhost:7687\n"
        "neo4j_user = neo4j\nneo4j_password = pw\n"
    )
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(cfg))
    config_mod._conf = None  # invalidate cache

    from src.mcp import server
    assert server._mcp_host() == "192.168.42.7"
    assert server._mcp_port() == 8888


def test_server_falls_back_when_config_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("ODOO_SEMANTIC_CONF", str(tmp_path / "nope.conf"))
    config_mod._conf = None
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.chdir(tmp_path)

    from src.mcp import server
    # importlib.reload to re-read config; or test the helper directly
    assert server._mcp_host() == "127.0.0.1"
    assert server._mcp_port() == 8002
```

- [ ] **Bước 2: Run — verify đỏ** (helpers `_mcp_host`/`_mcp_port` chưa tồn tại).

- [ ] **Bước 3: Modify `src/mcp/server.py`** — chèn helpers + sửa `__main__`.

Append phía trên block `if __name__ == "__main__":`:

```python
def _mcp_host() -> str:
    from src import config
    return config.get("server", "host", fallback="127.0.0.1")


def _mcp_port() -> int:
    from src import config
    return int(config.get("server", "port", fallback="8002"))
```

Sửa block `__main__`:

```python
if __name__ == "__main__":
    mcp.run(transport="streamable-http", host=_mcp_host(),
            port=_mcp_port(), path="/mcp")
```

- [ ] **Bước 4: Run test — verify xanh.**

```bash
~/.venv/odoo-semantic-mcp/bin/pytest tests/test_mcp_server_config.py -v
```
Expect: 2 passed.

- [ ] **Bước 5: Commit**

```bash
git add src/mcp/server.py tests/test_mcp_server_config.py
git commit -m "fix(m2.5): src/mcp/server — read host/port from odoo-semantic.conf"
```

---

## Task 8: Fix `docker-compose.yml` — bind ports to `127.0.0.1`

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Bước 1: Sửa block `neo4j.ports` và `postgres.ports`**

```yaml
services:
  neo4j:
    image: ${NEO4J_IMAGE:-neo4j:5.26.25}  # canonical version in .env.example
    environment:
      NEO4J_AUTH: neo4j/${NEO4J_PASSWORD:-password}
      NEO4J_server_bolt_advertised__address: ${NEO4J_ADVERTISED_HOST:-localhost}:7687
    ports:
      # Same-server default — DB localhost only. Reverse proxy / SSH tunnel
      # for split-tier deploy: change to "7687:7687" + firewall app-server IP.
      - "127.0.0.1:7474:7474"
      - "127.0.0.1:7687:7687"
    volumes:
      - neo4j_data:/data
    healthcheck:
      test: ["CMD-SHELL", "cypher-shell -u neo4j -p $${NEO4J_PASSWORD:-password} 'RETURN 1' || exit 1"]
      interval: 10s
      retries: 10

  postgres:
    image: pgvector/pgvector:0.8.2-pg16
    environment:
      POSTGRES_DB: odoo_semantic
      POSTGRES_USER: odoo_semantic
      POSTGRES_PASSWORD: ${PG_PASSWORD:-password}
    ports:
      # Same as Neo4j: 127.0.0.1 default. Split-tier: drop the prefix.
      - "127.0.0.1:5432:5432"
    volumes:
      - pg_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U odoo_semantic"]
      interval: 10s
      retries: 5
```

- [ ] **Bước 2: Verify ports actually bound to 127.0.0.1**

```bash
docker compose down
docker compose up -d neo4j postgres
ss -tlnp | grep -E "7687|5432|7474"
```
Expect: lines starting with `127.0.0.1:` only — no `0.0.0.0:` for these ports.

- [ ] **Bước 3: Commit**

```bash
git add docker-compose.yml
git commit -m "fix(m2.5): docker-compose — bind DB ports to 127.0.0.1 same-server default"
```

---

## Task 9: Modify `Makefile` — extend install target

**Files:**
- Modify: `Makefile`

- [ ] **Bước 1: Sửa `install` target**

```makefile
install:
	$(UV) venv $(VENV)
	$(UV) pip install --python $(VENV)/bin/python -e ".[dev]"
	@[ -f .env ] || (cp .env.example .env && \
		echo "✓ .env created — fill in NEO4J_PASSWORD, PG_PASSWORD")
	@[ -f odoo-semantic.conf ] || (cp odoo-semantic.conf.example odoo-semantic.conf && \
		echo "✓ odoo-semantic.conf created — fill in [database] passwords")
	@echo "Next: docker compose up -d  →  $(VENV)/bin/python -m src.db.migrate"
```

> **Lưu ý:** plan KHÔNG tự `docker compose up -d` + `python -m src.db.migrate` trong target `install` — vì chạy lần đầu lỗi (passwords blank) sẽ làm UX kém. In hướng dẫn next-step rõ ràng tốt hơn.

- [ ] **Bước 2: Smoke test**

```bash
rm -f odoo-semantic.conf
make install
[ -f odoo-semantic.conf ] && echo "OK"
```
Expect: `OK`.

- [ ] **Bước 3: Commit**

```bash
git add Makefile
git commit -m "feat(m2.5): Makefile install — copy .env + odoo-semantic.conf templates"
```

---

## Task 10: Modify `.gitignore`

**Files:**
- Modify: `.gitignore`

- [ ] **Bước 1: Thêm `odoo-semantic.conf`**

```
*.lock.*
.~lock.*
__pycache__/
.venv/
.serena/
odoo-semantic.conf
```

> **Audit note:** spec yêu cầu remove `index_test.py` từ `.gitignore` — thực tế file `.gitignore` không chứa `index_test.py`. Skip phần đó.

- [ ] **Bước 2: Verify**

```bash
grep -F "odoo-semantic.conf" .gitignore
```
Expect: 1 match.

- [ ] **Bước 3: Commit (kèm template)**

```bash
git add .gitignore odoo-semantic.conf.example
git commit -m "chore(m2.5): .gitignore odoo-semantic.conf + add example template"
```

---

## Task 11: Update docs — README, CONTRIBUTING, TASKS

**Files:**
- Modify: `README.md`
- Modify: `CONTRIBUTING.md`
- Modify: `TASKS.md`

- [ ] **Bước 1: Sửa `README.md` — Deploy section**

Replace block "Deploy Server (Admin)" với:

```markdown
## Deploy Server (Admin)

```bash
git clone https://github.com/Viindoo/odoo-semantic-mcp
cd odoo-semantic-mcp

# 1. Cài Python venv + tạo file config
make install
# → tạo .env (Docker passwords) + odoo-semantic.conf (app config)
# → fill 2 file này trước khi tiếp tục:
#   .env:                 NEO4J_PASSWORD, PG_PASSWORD
#   odoo-semantic.conf:   [database].neo4j_password

# 2. Start databases (Neo4j + PostgreSQL)
docker compose up -d

# 3. Bootstrap PostgreSQL schema
~/.venv/odoo-semantic-mcp/bin/python -m src.db.migrate

# 4. Đăng ký repos cần index (admin clone repos thủ công vào /home/user/git/...)
~/.venv/odoo-semantic-mcp/bin/python -m src.manager add-profile viindoo_17 --version 17.0
~/.venv/odoo-semantic-mcp/bin/python -m src.manager add-repo \
    --profile viindoo_17 \
    --url github.com/odoo/odoo --branch 17.0 \
    --local-path /home/user/git/odoo_17.0
~/.venv/odoo-semantic-mcp/bin/python -m src.manager list

# 5. Index lần đầu
~/.venv/odoo-semantic-mcp/bin/python -m src.indexer --profile viindoo_17
# hoặc index toàn bộ profiles:
# ~/.venv/odoo-semantic-mcp/bin/python -m src.indexer --all

# 6. Khởi động MCP server (long-running — dùng systemd / tmux)
~/.venv/odoo-semantic-mcp/bin/python -m src.mcp.server
# → bind 127.0.0.1:8002 mặc định (đọc từ odoo-semantic.conf [server])
```

**Reverse proxy (bắt buộc cho external access):** MCP server bind `127.0.0.1` để bắt buộc đặt reverse proxy phía trước (caddy / nginx / traefik). Auth ở M2.5 = IP allowlist hoặc basic auth tại proxy. **API key validation chưa có cho đến M5** — README hiện tại quảng cáo `X-API-Key` header chỉ là placeholder, codebase chưa validate.

**Backup / Restore khi chuyển server** *(Milestone 5 — chưa implement):*
```bash
# python -m src.cli backup --out backup-$(date +%Y%m%d).tar.gz
# python -m src.cli restore --from backup-20260505.tar.gz
```
```

Đồng thời cập nhật bảng "Trạng Thái Hiện Tại": thêm dòng `**Milestone 2.5 — "Foundation Wow":** [ ] Đang làm` (hoặc `[x]` sau khi finish).

- [ ] **Bước 2: Sửa `CONTRIBUTING.md` — Cấu Trúc Source**

Replace block `## Cấu Trúc Source` với:

```markdown
## Cấu Trúc Source

```
src/
├── config.py              # INI config reader (configparser)
├── db/
│   ├── migrate.py         # PostgreSQL schema bootstrap (profiles + repos)
│   └── repo_registry.py   # CRUD profiles + repos
├── manager/
│   └── __main__.py        # admin CLI: add-profile / add-repo / list
├── indexer/
│   ├── models.py          # dataclasses: ModuleInfo, ModelInfo, ViewInfo, QWebInfo, ...
│   ├── scanner.py         # git repo discovery
│   ├── registry.py        # __manifest__.py parsing + module map
│   ├── resolver.py        # topological sort (Kahn's algorithm)
│   ├── parser_python.py   # AST parser: _name/_inherit/_inherits/fields/methods
│   ├── parser_xml.py      # ir.ui.view + xpath modifications
│   ├── parser_qweb.py     # QWeb <template> inheritance
│   ├── pipeline.py        # end-to-end: scanner → registry → resolver → parsers → writer
│   ├── __main__.py        # CLI: python -m src.indexer --profile / --all
│   └── writer_neo4j.py    # write nodes + edges vào Neo4j
└── mcp/
    └── server.py          # FastMCP: resolve_model/field/method/view
```

Nguyên tắc: scanner → registry → resolver → parser → writer → server. Pipeline glue trong `src/indexer/pipeline.py` chỉ orchestrate, không chứa logic parse.
```

Cũng append vào "Cấu Trúc Test":

```markdown
├── test_config.py            # unit: src/config.py
├── test_db_migrate.py        # integration (postgres marker)
├── test_db_repo_registry.py  # integration (postgres marker)
├── test_manager_cli.py       # integration (postgres marker)
├── test_indexer_pipeline.py  # integration (neo4j + postgres marker)
└── test_mcp_server_config.py # unit: server reads config

**Marker mới:** `pytestmark = pytest.mark.postgres` cho test cần PostgreSQL.
`make test-unit` skip cả `neo4j` và `postgres`. `make test-integration` chạy cả hai.
```

- [ ] **Bước 3: Sửa `TASKS.md`**

Chèn block sau Milestone 2:

```markdown
## Milestone 2.5 — "Foundation Wow"
**Intent:** Hạ tầng đủ để E2E test M1+M2 trên data thật + nền cho M5 per-user scoping.
**Outcome:** `python -m src.indexer --profile viindoo_17` index full Odoo 17 + Viindoo addons; Claude Code gọi 4 MCP tools trên data thật.

- [ ] `src/config.py`: INI reader (`configparser`)
- [ ] `odoo-semantic.conf.example`: app config template
- [ ] `src/db/migrate.py`: schema `profiles` + `repos`
- [ ] `src/db/repo_registry.py`: CRUD profiles/repos
- [ ] `src/manager/__main__.py`: admin CLI (`add-profile`, `add-repo`, `list`)
- [ ] `src/indexer/pipeline.py`: wire `parser_xml` + `parser_qweb` (M2 blocker fix)
- [ ] `src/indexer/__main__.py`: `python -m src.indexer --profile / --all`
- [ ] `src/mcp/server.py`: read host/port from `odoo-semantic.conf`
- [ ] `docker-compose.yml`: bind DB ports `127.0.0.1` (same-server default)
- [ ] `Makefile`: extend `install` target — copy configs, hint next steps
- [ ] `.gitignore`: thêm `odoo-semantic.conf` (user secret)
- [ ] `README.md`: deploy steps thật
- [ ] `CONTRIBUTING.md`: cập nhật source tree
- [ ] E2E manual: clone Odoo 17 → register → index → Claude Code call 4 tools
```

Cũng sửa Milestone 5 — drop `src/cli.py` index/backup/restore, add SSH + Web UI repos:

```markdown
## Milestone 5 — "Product Wow"
**Intent:** Đóng gói thành sản phẩm bất kỳ ai deploy được trong dưới 10 phút.
**Outcome:** `docker compose up -d` + Web UI add repos + auto-clone qua SSH key + index.

- [ ] `src/auth.py`: API key middleware + usage log (PostgreSQL)
- [ ] `src/web_ui/repos.py`: profile + repo management Web UI (replace `src.manager` CLI)
- [ ] `src/web_ui/ssh_keys.py`: generate SSH key pair, expose public key cho user add vào repo họ
- [ ] `src/web_ui/dashboard.py`: status + key management + index status (FastAPI + Jinja2)
- [ ] `src/db/migrate.py`: thêm `ssh_key_pairs`, `api_keys`, `user_profile_access`
- [ ] Auto-clone qua SSH khi user add repo (replace `--local-path` manual step)
- [ ] `src/cli.py`: `backup` / `restore` (KHÔNG còn `index` — đã có ở M2.5)
- [ ] `docker-compose.yml`: hoàn thiện cho production (volumes named, restart policy)
- [ ] `install.sh`: non-Docker installation path
- [ ] `README.md`: hướng dẫn setup + kết nối VS Code / Claude Code / Codex / Gemini
```

- [ ] **Bước 4: Run test_doc_sync (anti-drift guard)**

```bash
~/.venv/odoo-semantic-mcp/bin/pytest tests/test_doc_sync.py -v
```
Expect: pass — guard validates `[x]` files exist on disk.

- [ ] **Bước 5: Commit**

```bash
git add README.md CONTRIBUTING.md TASKS.md
git commit -m "docs(m2.5): real deploy steps + source tree update + M5 SSH/Web UI plan"
```

---

## Final Verification

- [ ] **Run full test suite**

```bash
# unit only — no Docker needed
make test
# expect: green; ~45+ tests

# integration — needs Neo4j + PostgreSQL via docker compose
docker compose up -d neo4j postgres
make test-integration
# expect: ~25+ tests passing (M2.5 adds ~15-20)
```

- [ ] **Lint**

```bash
make lint
```
Expect: clean.

- [ ] **Manual E2E smoke (optional but recommended)**

```bash
# Assumes ~/git/odoo_17.0 has Odoo 17 cloned with branch=17.0
~/.venv/odoo-semantic-mcp/bin/python -m src.manager add-profile viindoo_17 --version 17.0
~/.venv/odoo-semantic-mcp/bin/python -m src.manager add-repo \
    --profile viindoo_17 \
    --url github.com/odoo/odoo --branch 17.0 \
    --local-path ~/git/odoo_17.0
~/.venv/odoo-semantic-mcp/bin/python -m src.indexer --profile viindoo_17
# Expect: "✓ Indexer done: {'modules': N, 'views': M, 'qweb': K}"

# Then start MCP server:
~/.venv/odoo-semantic-mcp/bin/python -m src.mcp.server &
# Connect Claude Code → semantic.localhost:8002/mcp → call resolve_model("sale.order", "17.0")
```

- [ ] **Final TASKS.md flip** — change `[ ]` → `[x]` for completed M2.5 items, commit:

```bash
git add TASKS.md
git commit -m "docs(m2.5): mark Foundation Wow tasks complete"
```

---

## Risk & Rollback

**Risk areas:**
1. **PostgreSQL ports conflict** — nếu host đã chạy PG khác trên 5432, `docker compose up postgres` fail. Mitigation: docker-compose comment hướng dẫn admin đổi port mapping.
2. **Pipeline scanner edge case** — `scan_repos` chỉ pick repo có branch khớp regex `^\d+\.\d+$`. Nếu admin clone theo tag/commit khác, registry skip. Mitigation: pipeline có fallback `repo_pairs` dùng `local_path + odoo_version` từ DB khi scanner trả empty.
3. **Idempotency**: chạy `index_profile` 2 lần phải an toàn. Writer dùng `MERGE` nên OK; `update_repo_status` ghi đè trạng thái — chấp nhận được.

**Rollback plan (nếu pipeline phá Neo4j data):**
```bash
docker compose down
docker volume rm odoo-semantic-mcp_neo4j_data
docker compose up -d neo4j
~/.venv/odoo-semantic-mcp/bin/python -m src.indexer --all  # re-index from scratch
```
PostgreSQL registry độc lập — không cần đụng tới khi re-index Neo4j.
