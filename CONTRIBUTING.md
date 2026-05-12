# Hướng Dẫn Dev — Odoo Semantic MCP

Tài liệu này dành cho developer muốn contribute hoặc phát triển thêm tính năng.

> 📖 **Bạn là end-user muốn kết nối AI tool (Claude Code, Codex, Gemini…) vào MCP server?**
> Xem [docs/client-setup.md](docs/client-setup.md) thay vì tài liệu này.

---

## Yêu Cầu Môi Trường

| Thứ | Phiên bản | Dùng để làm gì |
|-----|-----------|----------------|
| **Python** | ≥ 3.12 | Runtime chính |
| **Docker** | bất kỳ | Chạy Neo4j cho integration tests — testcontainers tự quản lý, không cần thao tác thủ công |
| **Git** | bất kỳ | Clone repo, scanner cần `git branch` |
| **uv** | ≥ 0.4 | Package manager (nhanh hơn pip) |

> Nếu chưa có `uv`: `curl -LsSf https://astral.sh/uv/install.sh | sh`  
> Nếu chưa có Docker: xem mục **[Cài Docker](#cài-docker)** bên dưới — có thêm bước cấu hình bắt buộc sau khi cài.

---

## Setup Lần Đầu

```bash
git clone https://github.com/Viindoo/odoo-semantic-mcp
cd odoo-semantic-mcp

# 1. Tạo virtualenv bên ngoài repo (tại ~/.venv/odoo-semantic-mcp/)
make install
# Hoặc thủ công tương đương:
# uv venv ~/.venv/odoo-semantic-mcp
# uv pip install --python ~/.venv/odoo-semantic-mcp/bin/python -e ".[dev]"

# 2. Copy file cấu hình (chỉ cần cho chạy MCP server thật — không cần cho test)
cp .env.example .env

# 3. Generate FERNET_KEY for SSH key encryption:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Add to .env: FERNET_KEY=<value>
```

`make install` tự chọn Python 3.12 nhờ file `.python-version` và tạo venv tại `~/.venv/odoo-semantic-mcp/` — nằm ngoài repo, tránh ô nhiễm source tree. Nếu máy chưa có 3.12, uv tự download.

Xong. Không cần start database thủ công — testcontainers lo hết.

---

## Chạy Tests

### Unit tests (không cần Docker)

```bash
make test
# hoặc:
~/.venv/odoo-semantic-mcp/bin/pytest tests/ -m "not neo4j" -v
```

Chạy ngay, không cần Neo4j. Bao gồm: scanner, registry, resolver, parser.

### Integration tests (cần Docker)

```bash
make test-integration
# hoặc:
~/.venv/odoo-semantic-mcp/bin/pytest tests/ -m "neo4j" -v
```

Khi chạy lần đầu, testcontainers sẽ pull image được cấu hình trong `NEO4J_IMAGE` (`.env.example`) — mặc định `neo4j:5.26.25` (~500MB). Từ lần sau Docker cache lại, chạy nhanh hơn. Neo4j container tự start trước khi test và tự destroy sau khi xong — không cần `docker compose up` thủ công.

### Toàn bộ test suite

```bash
make test-all
```

### Browser E2E tests (Playwright + chromium)

`make install` cài Python package `pytest-playwright` nhưng KHÔNG cài chromium binary (~150MB) — bước riêng:

```bash
~/.venv/odoo-semantic-mcp/bin/playwright install chromium
make test-browser
```

`make test-browser` tự spin PostgreSQL qua docker compose, đợi healthy, rồi chạy 21 tests trong `tests/test_web_ui_browser.py`.

> **Note:** `make test-integration` tự skip browser tests nếu chromium chưa cài (qua `pytest_collection_modifyitems` hook trong `tests/conftest.py`) — không cascade error sang tests khác.

### Chạy một test cụ thể

```bash
~/.venv/odoo-semantic-mcp/bin/pytest tests/test_registry.py::test_parse_manifest_basic -v
~/.venv/odoo-semantic-mcp/bin/pytest tests/test_writer_neo4j.py -v   # integration, cần Docker
```

---

## Cấu Trúc Test

```
tests/
├── conftest.py               # fixtures dùng chung — đọc trước khi viết test
├── test_config.py            # unit: src/config.py
├── test_models.py            # unit: data models
├── test_scanner.py           # unit: git repo discovery
├── test_registry.py          # unit: manifest parsing + module registry
├── test_resolver.py          # unit: topological sort
├── test_parser_python.py     # unit: AST parser
├── test_mcp_server_config.py # unit: server reads host/port from config
├── test_embedding_instructions.py  # unit: Qwen3 asymmetric INSTRUCT prefix
├── test_embedder.py                # unit: FakeEmbedder + Qwen3Embedder protocol
├── test_parser_js.py               # unit: era-aware JS parser
├── test_pipeline_config.py         # unit: Neo4j creds from config
├── test_indexer_main.py            # unit: --no-embed flag + embedder build
├── test_writer_neo4j.py      # integration (neo4j marker)
├── test_mcp_server.py        # integration (neo4j marker)
├── test_db_migrate.py        # integration (postgres marker)
├── test_db_repo_registry.py  # integration (postgres marker)
├── test_manager_cli.py       # integration (postgres marker)
├── test_writer_pgvector.py         # integration (postgres marker)
├── test_mcp_find_examples.py       # integration (neo4j + postgres markers)
├── test_indexer_pipeline.py  # integration (neo4j + postgres markers)
├── test_doc_sync.py          # unit: TASKS.md [x] files exist on disk (anti-drift)
├── test_output_snapshots.py  # unit: MCP output schema contract (anti-drift)
└── test_find_examples_recall.py    # ollama marker (requires Ollama + indexed data)
```

**Quy tắc marker:**
- Test cần Neo4j → thêm `pytestmark = pytest.mark.neo4j` ở đầu file
- Test cần PostgreSQL → thêm `pytestmark = pytest.mark.postgres` ở đầu file
- Test cần Ollama + model loaded + data indexed → thêm `pytestmark = pytest.mark.ollama`
  Chạy: `pytest tests/test_find_examples_recall.py -m ollama -v`
- Test không cần DB → không thêm gì — chạy trong unit mode
- `smoke` marker — health schema, auth 401, SSH keygen (M5+)

**`TEST_VERSION = "99.0"`** — tất cả dữ liệu test dùng version này để không conflict với dữ liệu thật. Fixture `clean_neo4j` dọn Neo4j, fixture `clean_pg` dọn PostgreSQL — luôn dùng fixture tương ứng.

---

## Cài Docker

Docker cần thiết để chạy integration tests. `testcontainers` tự spin up / destroy Neo4j container — không cần thao tác thủ công — nhưng cần Docker daemon đang chạy và user có quyền dùng socket.

### Ubuntu / Debian

```bash
# 1. Cài Docker Engine (bản chính thức, mới hơn docker.io của Ubuntu)
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
     -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# 2. Start daemon và bật tự khởi động cùng hệ thống
sudo systemctl enable --now docker

# 3. Thêm user hiện tại vào group docker
#    (bắt buộc — testcontainers kết nối qua /var/run/docker.sock)
sudo usermod -aG docker $USER

# 4. Áp dụng thay đổi group — PHẢI logout rồi login lại, hoặc chạy:
newgrp docker
```

> **Quan trọng:** bước 3 bắt buộc. Nếu bỏ qua, testcontainers sẽ báo lỗi  
> `FileNotFoundError: [Errno 2] No such file or directory` hoặc `Permission denied`  
> khi cố kết nối `/var/run/docker.sock`.

### macOS

Cài [Docker Desktop](https://www.docker.com/products/docker-desktop/). Sau khi cài, mở Docker Desktop và đợi icon trên taskbar chuyển sang trạng thái "Running". Không cần thêm bước cấu hình.

### Windows

Cài [Docker Desktop](https://www.docker.com/products/docker-desktop/) với backend WSL 2. Sau khi cài, mở Docker Desktop và đợi trạng thái "Running" trước khi chạy test.

### Kiểm tra Docker hoạt động đúng

```bash
# Kiểm tra daemon đang chạy
docker info

# Kiểm tra user có quyền dùng socket (không cần sudo)
docker run --rm hello-world
```

Cả hai lệnh phải chạy được **không có sudo**. Nếu `docker info` báo `Permission denied`, cần logout / login lại sau bước 3 ở trên.

### Kiểm tra testcontainers hoạt động

```bash
make test-integration
```

Lần chạy đầu tiên sẽ pull image `neo4j:5.26.25` (set trong `NEO4J_IMAGE` ở `.env.example`, ~500 MB) — có thể mất 2–5 phút. Các lần sau Docker cache lại, chạy trong vài giây. Nếu thành công các test có marker `neo4j` sẽ chuyển từ SKIPPED sang PASSED.

---

## Khi Integration Tests Vẫn Skip Sau Khi Cài Docker

Chạy `make test-integration` — cuối output có phần `short test summary` hiện lý do cụ thể:

| Lỗi hiện trong summary | Nguyên nhân | Cách fix |
|------------------------|-------------|----------|
| `FileNotFoundError: No such file or directory` | Docker chưa cài hoặc daemon chưa start | `sudo systemctl start docker` |
| `Permission denied: /var/run/docker.sock` | User chưa trong group `docker` | `sudo usermod -aG docker $USER` rồi logout/login |
| `Couldn't connect to localhost:7687` | testcontainers fail, bolt cũng fail | Xem lỗi testcontainers phía trên |
| Pull image bị timeout | Mạng chậm | Thử lại, hoặc `docker pull $(grep NEO4J_IMAGE .env.example \| cut -d= -f2)` trước |

Unit tests (`make test`) không bị ảnh hưởng bởi Docker và luôn chạy được.

---

## Quản Lý Neo4j Thủ Công (Tùy Chọn)

Nếu muốn giữ Neo4j chạy liên tục trong quá trình dev (không cần testcontainers spin up mỗi lần):

```bash
make neo4j-up      # start Neo4j tại bolt://localhost:7687
make test-integration  # lần này kết nối trực tiếp, không cần testcontainers
make neo4j-down    # stop khi xong
make neo4j-logs    # xem logs nếu có vấn đề
```

---

## Workflow Khi Thêm Tính Năng

```
1. Đọc TASKS.md → chọn task
2. Đọc docs/thiet-ke-kien-truc.md nếu cần hiểu schema / pipeline
3. Viết failing test trước (TDD)
4. Implement cho test pass
5. Chạy make test-all — phải xanh hết
6. Commit với message rõ ràng
7. Push → CI GitHub Actions tự chạy
```

**Commit message convention:**
```
feat: thêm parser_xml cho view inheritance
fix:  sửa MERGE query Neo4j khi multi-module
test: thêm test case cho circular dependency
docs: cập nhật schema diagram
```

---

## Commands

### Create API key (M5+)

```bash
~/.venv/odoo-semantic-mcp/bin/python -m src.manager create-api-key <name>
# → prints raw key once
```

### Start Web UI admin (M5+)

```bash
~/.venv/odoo-semantic-mcp/bin/python -m src.web_ui
# → http://127.0.0.1:8003/
```

---

## Lint

```bash
make lint
# hoặc:
~/.venv/odoo-semantic-mcp/bin/ruff check src/ tests/
```

CI sẽ fail nếu lint không pass.

---

## Python 3.12 Code Style

Project locks Python 3.12+ (`pyproject.toml` `requires-python = ">=3.12"`). Để codebase nhất quán với PEP 604/PEP 695:

- **Cấm `from __future__ import annotations`** — Python 3.12 native. Lazy evaluation cũng tự nhiên trong PEP 695 type aliases.
- **Cấm `typing.Dict / typing.List / typing.Optional / typing.Union[]`** — dùng generics built-in: `dict`, `list`, `X | None`, `X | Y`.
- **Cấm `sys.version_info` guards** — chỉ chạy trên 3.12+, fork branches không cần thiết.
- **Ruff `UP` rules enforce** — `pyproject.toml [tool.ruff.lint] select = ["E", "F", "I", "UP"]` tự fail khi vi phạm.

Khi `ruff check` raise UP violation, sửa root cause (đổi syntax sang form mới) — không suppress.

---

## Known Upstream Warnings

**`make test` (unit tests):** 0 warnings.

**`make test-integration` (CI):** 0 warnings — CI uses GitHub Actions service container; `neo4j_driver` fixture skips testcontainers import entirely when `CI=true`.

**`make test-integration` (local dev with Docker):** 2 warnings from testcontainers:

```
DeprecationWarning: The @wait_container_is_ready decorator is deprecated...
```
Source: `testcontainers/core/waiting_utils.py` and `testcontainers/neo4j/__init__.py` — the `@wait_container_is_ready()` decorator fires at class-definition time (module import). Cannot be avoided without modifying upstream source or downgrading to testcontainers 3.x (which would require a full API rewrite of `conftest.py`).
Status: Upstream issue in testcontainers 4.x. Will be resolved when upstream removes the deprecated decorator.

**Action:** Do NOT suppress with `filterwarnings`. Monitor testcontainers releases and upgrade when fixed.

**Previously tracked — now fixed:**
- `AuthlibDeprecationWarning` (authlib.jose → joserfc migration): fixed by pinning `authlib>=1.6.5,<1.7.0` — the warning was added in v1.7.0.

---

## Cấu Trúc Source

```
src/
├── config.py              # INI config reader (configparser)
├── db/
│   ├── migrate.py         # PostgreSQL schema bootstrap (profiles + repos + embeddings)
│   └── repo_registry.py   # CRUD profiles + repos
├── embedding/
│   └── instructions.py    # Qwen3 asymmetric INSTRUCT prefix (NL→code retrieval)
├── manager/
│   └── __main__.py        # admin CLI: add-profile / add-repo / list
├── indexer/
│   ├── models.py          # dataclasses: ModuleInfo, ModelInfo, ViewInfo, QWebInfo, JSChunk, ...
│   ├── scanner.py         # git repo discovery
│   ├── registry.py        # __manifest__.py parsing + module map
│   ├── resolver.py        # topological sort (Kahn's algorithm)
│   ├── parser_python.py   # AST parser: _name/_inherit/_inherits/fields/methods (+ source text)
│   ├── parser_xml.py      # ir.ui.view + xpath modifications (+ arch capture)
│   ├── parser_qweb.py     # QWeb <template> inheritance (+ content capture)
│   ├── parser_js.py       # era-aware JS parser (Era1 Widget.extend, Era2 odoo.define, Era3 OWL/patch)
│   ├── embedder.py        # EmbedderClient Protocol + FakeEmbedder + Qwen3Embedder (MRL 1024-dim)
│   ├── pipeline.py        # end-to-end: scanner → registry → resolver → parsers → writers
│   ├── __main__.py        # CLI: python -m src.indexer index-repo --profile / --all / --no-embed
│   ├── writer_neo4j.py    # write nodes + edges vào Neo4j
│   └── writer_pgvector.py # EmbeddingChunk + make_chunks + write_module_embeddings (HNSW)
└── mcp/
    └── server.py          # FastMCP: resolve_model/field/method/view + find_examples
```

Nguyên tắc: scanner → registry → resolver → parser → writer → server. Pipeline glue trong `src/indexer/pipeline.py` chỉ orchestrate, không chứa logic parse.

---

## Tài Liệu Liên Quan

| File | Nội dung |
|------|----------|
| [`docs/thiet-ke-kien-truc.md`](docs/thiet-ke-kien-truc.md) | Graph schema, pipeline, MCP tools — đọc trước khi code |
| [`docs/huong-dan-stack.md`](docs/huong-dan-stack.md) | Neo4j patterns, AST gotchas, FastMCP, pytest tips |
| [`docs/deploy.md`](docs/deploy.md) | Production deploy guide — cho admin, không phải dev |
| [`docs/adr/`](docs/adr/) | Architecture Decision Records — đọc trước khi đụng schema/policy |
| [`TASKS.md`](TASKS.md) | Tiến độ milestones — đánh dấu khi xong |

---

## Contributing Patterns

Want to add a new pattern to the catalogue? Patterns are stored in `src/data/patterns.json` and follow a formal contribution policy.

**Quick checklist:**
- Pattern `pattern_id` must be unique (kebab-case, regex `^[a-z][a-z0-9-]*$`)
- Language must be one of: `python`, `xml`, `js`
- Include ≥3 specific gotchas (concrete API or edge case, not boilerplate)
- NO Odoo Enterprise references (module paths, license markers, proprietary addons)
- Optional: reference `core_symbol_names` (qualified API names like `odoo.api.depends`)

**Full policy:** [ADR-0009](docs/adr/0009-pattern-catalogue-community-contribution.md)

**PR template:** When opening a PR with pattern changes, use `.github/PULL_REQUEST_TEMPLATE/patterns.md` (GitHub auto-fills). The template lists the 7-rule checklist and examples.

**Note:** After your pattern is merged, the catalogue auto-reseeds on the next indexer run (via `_SeedMeta` sentinel per ADR-0007) — no manual action needed. Pattern embeddings are computed and indexed into pgvector automatically.

---

## Architecture Decision Records (ADR)

Mọi quyết định kiến trúc lớn — schema policy, storage pattern, parser convention — phải có ADR trong `docs/adr/`. Format theo template ADR-0001: Date / Status / Context / Decision / Consequences (Positive/Negative/Risk) / Alternatives Considered.

**ADR đã có:**

| ADR | Tiêu đề | Áp dụng cho |
|-----|---------|-------------|
| [`0001`](docs/adr/0001-schema-evolution-policy.md) | Schema Evolution Policy | PostgreSQL: no ALTER TABLE until M6 — chỉ `CREATE TABLE IF NOT EXISTS`. M2.5–M5 add-only. |
| [`0002`](docs/adr/0002-spec-schema-policy.md) | Spec Schema Policy (M4.5) | Neo4j: composite key per-version cho CoreSymbol/LintRule/CLI; lifecycle qua edge ADDED_IN/REMOVED_IN/REPLACED_BY/DEPRECATED_IN; USES_CORE_SYMBOL V0 scope hẹp deprecated/removed only. |
| [`0003`](docs/adr/0003-pattern-example-storage.md) | PatternExample Storage (M4.6) | Neo4j PatternExample node + reuse `embeddings` table với `chunk_type='pattern_example'`; Module/Method enrichment qua SET property (no ALTER); language filter qua entity_name slug encoding. |
| [`0009`](docs/adr/0009-pattern-catalogue-community-contribution.md) | Pattern Catalogue Community Contribution (M6 W3) | Community PRs to `src/data/patterns.json` must pass 7-rule checklist (schema, dedup, format, enum, gotchas specificity, no EE refs, symbol resolution) + PR template guidance. |

**Workflow ADR mới:**

1. Trước khi viết schema/policy mới (vd thêm node label, đổi composite key, tạo bảng), tạo ADR draft tại `docs/adr/000X-<kebab-slug>.md`.
2. Status `Draft` → David review → `Accepted` (hoặc `Rejected` + lý do).
3. Reference ADR ID trong commit message + plan file + code comment khi implement.
4. ADR là immutable history — không xoá ADR. Nếu thay thế → mark `Status: Superseded by ADR-XXXX` thay vì delete.

---

## Plugin Development

The Claude Code plugin lives at `dist/odoo-semantic-plugin/`. It follows the [Claude Code plugin spec](https://code.claude.com/docs/en/plugins-reference).

### Plugin structure

```
dist/odoo-semantic-plugin/
├── .claude-plugin/
│   └── plugin.json          # Plugin manifest: userConfig, skills/agents/commands refs, mcpServers
├── .mcp.json                 # MCP server config: HTTP transport, ${user_config.*} interpolation
├── skills/                   # 11 SKILL.md files (one per persona skill)
├── agents/                   # odoo-router.md (Haiku) + odoo-upgrade-planner.md (Sonnet)
├── commands/                 # odoo-setup.md (/odoo-semantic:setup)
└── README.md
```

### API key handling

Sensitive values (API key) use `userConfig` in `plugin.json` with `"sensitive": true`. This stores the value in the system keychain (not `settings.json`) and makes it available in `.mcp.json` via `${user_config.api_key}`. **Never hardcode keys in plugin files.**

### Adding a new skill

1. Create `dist/odoo-semantic-plugin/skills/<skill-name>/SKILL.md`
2. Follow the SKILL.md format: frontmatter with `persona`, `triggers`, `tools_used` + instructions + output format
3. Add a routing case to `tests/test_skill_disambiguation.py`
4. Run `pytest tests/test_skill_disambiguation.py` — ensure ≥80% accuracy holds
5. Update `dist/odoo-semantic-plugin/README.md` skills table

### Validating the plugin

```bash
# Requires Claude Code CLI installed
claude plugin validate dist/odoo-semantic-plugin/

# Unit tests (no server needed)
~/.venv/odoo-semantic-mcp/bin/python -m pytest tests/test_skill_disambiguation.py tests/test_mcp_tool_descriptions.py -v
```

### Publishing

See [docs/deploy/plugin-release.md](docs/deploy/plugin-release.md) for the full release + SHA-pinning workflow.
