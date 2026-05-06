# Design Spec: Milestone 2.5 — "Foundation Wow"

**Date:** 2026-05-06  
**Status:** Approved by David Tran  
**Scope:** Infrastructure foundation enabling real E2E testing of M1+M2, extensible to M5+ per-user scoping

---

## 1. Problem Statement

After Milestone 1 + 2 code review (Opus, 2026-05-06), four critical blockers prevent E2E testing:

- **C1**: No usable indexer entrypoint — `scripts/index_test.py` is gitignored, `src/cli.py` doesn't exist
- **C2**: `parser_xml` + `parser_qweb` are NOT wired into any production pipeline — M2 E2E impossible
- **C3**: `MCP_HOST`/`MCP_PORT` env vars declared in `.env.example` but hardcoded in `server.py:246`
- **C4**: No API key auth (deferred to M5), but README advertises `X-API-Key` header that does nothing

Additionally, the project lacks a proper repo registry — no way for admin to declare *which repos to index* without editing source code, and no foundation for M5 per-user scoping.

---

## 2. Goals

1. **E2E unblocked**: Admin can clone repos, register them, run indexer, and Claude Code can call all 4 MCP tools against real Neo4j data
2. **Config foundation**: Clean two-layer config (`odoo-semantic.conf` for app, `.env` for Docker) — `make install` creates both
3. **Repo registry in PostgreSQL**: Extensible foundation for M5 per-user scoping, M5 SSH auto-clone, M5 Web UI management
4. **Docs truth**: README, CONTRIBUTING, TASKS all reflect what code actually does

---

## 3. Non-Goals (explicitly deferred)

- API key auth → M5
- SSH key pair for auto-clone → M5
- Web UI for repo management → M5 (replaces M2.5 CLI)
- Per-user profile scoping → M5
- Async indexing job queue → M5+
- `backup` / `restore` CLI → M5

---

## 4. Architecture

### 4.1 Two-Layer Config

**Layer 1: `.env`** — Docker tier only. Read by `docker compose`. Never read directly by Python app.

```ini
# .env (generated from .env.example by make install)
NEO4J_IMAGE=neo4j:5.26.25
NEO4J_PASSWORD=<must-fill>
PG_PASSWORD=<must-fill>
NEO4J_TEST_URI=bolt://localhost:7687
NEO4J_TEST_USER=neo4j
NEO4J_TEST_PASSWORD=<must-fill>
```

**Layer 2: `odoo-semantic.conf`** — App tier. Read by Python via `configparser`. Generated from `odoo-semantic.conf.example` by `make install`.

```ini
[database]
neo4j_uri = bolt://localhost:7687
neo4j_user = neo4j
neo4j_password =             ; must fill

pg_dsn = postgresql://odoo_semantic:<password>@localhost:5432/odoo_semantic

[server]
host = 127.0.0.1             ; bind localhost — reverse proxy handles external
port = 8002

[indexer]
repos_base_dir = /home/user/git   ; fallback when local_path not set in DB
```

**Reading config in Python:**

```python
# src/config.py
import configparser, pathlib

_conf = configparser.ConfigParser()
_conf.read(pathlib.Path.home() / ".odoo-semantic" / "odoo-semantic.conf")
# fallback: ./odoo-semantic.conf

def get(section: str, key: str, fallback=None) -> str:
    return _conf.get(section, key, fallback=fallback)
```

Config file search order: `~/.odoo-semantic/odoo-semantic.conf` → `./odoo-semantic.conf` → env vars as final fallback.

### 4.2 PostgreSQL Repo Registry

Tables created by `python -m src.db migrate`:

```sql
CREATE TABLE profiles (
    id          SERIAL PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,     -- e.g. "viindoo_17"
    odoo_version TEXT NOT NULL,           -- e.g. "17.0"
    description TEXT,
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE TABLE repos (
    id              SERIAL PRIMARY KEY,
    profile_id      INTEGER REFERENCES profiles(id) ON DELETE CASCADE,
    url             TEXT NOT NULL,        -- "github.com/org/repo"
    branch          TEXT NOT NULL,        -- "17.0"
    local_path      TEXT NOT NULL,        -- "/home/user/git/odoo_17.0"
    status          TEXT DEFAULT 'pending',  -- pending | indexed | error
    last_indexed_at TIMESTAMP,
    error_msg       TEXT,
    created_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE (url, branch)
);
```

**M5 additions** (tracked in M5 tasks, not M2.5):
- `ssh_key_pairs` table — store generated key pair for auto-clone
- `api_keys` table — user auth tokens
- `user_profile_access` table — per-user profile scoping

### 4.3 Indexer Pipeline (Fixed)

M2 blocker fixed: `parser_xml` + `parser_qweb` wired into indexer:

```
python -m src.indexer --profile viindoo_17
  │
  ├── read repos from PostgreSQL WHERE profile_id = viindoo_17
  ├── for each repo:
  │     ├── parser_python.parse_module(path)  → ParseResult
  │     ├── parser_xml.parse_module(path)     → ViewParseResult   ← NEW wiring
  │     ├── parser_qweb.parse_module(path)    → QWebParseResult   ← NEW wiring
  │     └── writer.write_results() + write_view_results() + write_qweb_results()
  └── update repos.status, last_indexed_at in PostgreSQL
```

### 4.4 Admin CLI (M2.5 — temporary, replaced by Web UI in M5)

```bash
# Register profile
python -m src.manager add-profile viindoo_17 --version 17.0

# Register repo (admin has already cloned manually to local_path)
python -m src.manager add-repo \
    --profile viindoo_17 \
    --url github.com/odoo/odoo \
    --branch 17.0 \
    --local-path /home/user/git/odoo_17.0

# List repos
python -m src.manager list

# Run indexer
python -m src.indexer --profile viindoo_17
python -m src.indexer --all   # index all profiles
```

**M5 drop note**: `src/manager/` CLI will be removed when Web UI ships. All profile/repo management moves to Web UI. SSH key pair generation + auto-clone replaces `--local-path` manual step.

### 4.5 MCP Server Fix

`src/mcp/server.py` reads from `odoo-semantic.conf` via `src.config`:

```python
# Before (hardcoded):
mcp.run(transport="streamable-http", host="0.0.0.0", port=8002, path="/mcp")

# After:
host = config.get("server", "host", fallback="127.0.0.1")
port = int(config.get("server", "port", fallback="8002"))
mcp.run(transport="streamable-http", host=host, port=port, path="/mcp")
```

### 4.6 Docker Compose Security Fix

Same-server default: bind DB ports to `127.0.0.1`:

```yaml
ports:
  - "127.0.0.1:7474:7474"   # Neo4j Browser — localhost only
  - "127.0.0.1:7687:7687"   # Neo4j Bolt — localhost only (same-server)
  - "127.0.0.1:5432:5432"   # PostgreSQL — localhost only (same-server)
```

Split-tier note in docker-compose comments: "When running DB on separate server, change `127.0.0.1:7687:7687` → `7687:7687` and configure firewall to allow only app server IP."

---

## 5. `make install` Flow

```makefile
install:
    # 1. Create venv
    uv venv ~/.venv/odoo-semantic-mcp
    uv pip install --python ~/.venv/odoo-semantic-mcp/bin/python -e ".[dev]"

    # 2. Copy configs if not exist
    @[ -f .env ] || (cp .env.example .env && echo "✓ .env created — fill in passwords")
    @[ -f odoo-semantic.conf ] || (cp odoo-semantic.conf.example odoo-semantic.conf && \
        echo "✓ odoo-semantic.conf created — fill in connection settings")

    # 3. Start databases
    docker compose up -d

    # 4. Run DB migrations
    ~/.venv/odoo-semantic-mcp/bin/python -m src.db migrate

    @echo "✓ Install complete. Next: python -m src.manager add-profile ..."
```

---

## 6. Deployment Guide (E2E on 192.168.1.67)

The server already runs ollama+qwen. Port matrix — no conflicts:

| Service | Port | Binding |
|---------|------|---------|
| MCP server | 8002 | 127.0.0.1 (reverse proxy fronts it) |
| Neo4j Bolt | 7687 | 127.0.0.1 (same-server) |
| Neo4j Browser | 7474 | 127.0.0.1 |
| PostgreSQL | 5432 | 127.0.0.1 |
| ollama | 11434/80/9999 | existing, no conflict |

**Reverse proxy (admin's responsibility)**: Route `https://<domain>/mcp` → `http://127.0.0.1:8002/mcp`. Auth until M5 = IP allowlist or basic auth at proxy level. README documents this explicitly: "No API key validation until M5 — protect endpoint at reverse proxy."

---

## 7. TASKS.md Changes

### New: Milestone 2.5 — "Foundation Wow"

- [ ] `odoo-semantic.conf.example` + `src/config.py`: INI config reader
- [ ] `src/db/migrate.py`: create `profiles` + `repos` tables in PostgreSQL
- [ ] `src/db/repo_registry.py`: CRUD for profiles + repos
- [ ] `src/manager/__init__.py`: CLI — add-profile, add-repo, list
- [ ] `src/indexer/__main__.py`: CLI entry point (`python -m src.indexer --profile / --all`)
- [ ] `src/indexer/pipeline.py`: wire `parser_xml` + `parser_qweb` into pipeline, read repos from PostgreSQL
- [ ] `src/mcp/server.py`: read MCP_HOST/PORT from `odoo-semantic.conf`
- [ ] `docker-compose.yml`: bind ports to `127.0.0.1` for same-server deploy
- [ ] `Makefile`: add `install` target (copy configs + docker up + migrate)
- [ ] `.gitignore`: remove `index_test.py`; add `odoo-semantic.conf` (user config, not committed)
- [ ] `README.md`: real deploy steps, remove `src.cli` reference, add reverse proxy note
- [ ] `CONTRIBUTING.md`: add `parser_xml.py`, `parser_qweb.py` to source tree
- [ ] `TASKS.md`: update M5 (remove CLI commands, add SSH key management + Web UI repos)
- [ ] E2E test: admin clones Odoo 17 repo → registers → indexes → Claude Code calls all 4 tools

### Updated: Milestone 5 — "Product Wow" (drop CLI, add SSH + Web UI repos)

Drop:
- ~~`src/cli.py`: `index` / `backup` / `restore` commands~~ → replaced by Web UI + SSH auto-clone

Add:
- `src/web_ui/repos.py`: profile + repo management via Web UI (replaces `src.manager` CLI)
- `src/web_ui/ssh_keys.py`: generate SSH key pair, display public key for user to add to their repo
- `src/db/migrate.py`: add `ssh_key_pairs`, `api_keys`, `user_profile_access` tables
- Auto-clone via SSH: when user adds repo via Web UI, server clones using stored SSH key

---

## 8. Files Created / Modified Summary

| File | Action | Why |
|------|--------|-----|
| `odoo-semantic.conf.example` | Create | App config template |
| `src/config.py` | Create | INI config reader |
| `src/db/__init__.py` | Create | DB package |
| `src/db/migrate.py` | Create | Schema migration |
| `src/db/repo_registry.py` | Create | CRUD repos/profiles |
| `src/manager/__init__.py` | Create | Admin CLI |
| `src/indexer/__main__.py` | Create | CLI entry point for `python -m src.indexer` |
| `src/indexer/pipeline.py` | Create | Wire XML+QWeb parsers, read repos from PostgreSQL |
| `src/mcp/server.py` | Modify | Read host/port from config |
| `docker-compose.yml` | Modify | Bind ports to 127.0.0.1 |
| `Makefile` | Modify | Add install target |
| `.gitignore` | Modify | Remove index_test.py; add odoo-semantic.conf |
| `README.md` | Modify | Real deploy steps |
| `CONTRIBUTING.md` | Modify | Add M2 parsers to source tree |
| `TASKS.md` | Modify | Add M2.5 section; update M5 (drop CLI, add SSH + Web UI repos) |
