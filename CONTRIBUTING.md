# Hướng Dẫn Dev — Odoo Semantic MCP

Tài liệu này dành cho developer muốn contribute hoặc phát triển thêm tính năng.

> 📖 **Bạn là end-user muốn kết nối AI tool (Claude Code, Codex, Gemini…) vào MCP server?**
> Xem [client setup guide](https://github.com/Viindoo/odoo-mcp-client/blob/master/plugins/odoo-ai-agents/docs/setup.md) thay vì tài liệu này.

---

## Yêu Cầu Môi Trường

| Thứ | Phiên bản | Dùng để làm gì |
|-----|-----------|----------------|
| **Python** | ≥ 3.12 | Runtime chính |
| **Docker** | bất kỳ | Chạy Neo4j cho integration tests — testcontainers tự quản lý, không cần thao tác thủ công |
| **Git** | bất kỳ | Clone repo, scanner cần `git branch` |
| **uv** | ≥ 0.4 | Package manager (nhanh hơn pip) |
| **Node.js** | 20 LTS+ | Build + run Astro frontend (`site/`) — Node 24 khuyến nghị từ tháng 6/2026 khi GitHub Actions nâng chuẩn |
| **pnpm** | bất kỳ | Package manager cho `site/` — `npm i -g pnpm` hoặc `corepack enable pnpm` |

> Nếu chưa có `uv`: `curl -LsSf https://astral.sh/uv/install.sh | sh`  
> Nếu chưa có Docker: xem mục **[Cài Docker](#cài-docker)** bên dưới — có thêm bước cấu hình bắt buộc sau khi cài.

---

## Setup Lần Đầu

> **Note:** This is a private Viindoo repository — cloning requires org membership or a granted deploy key.

```bash
git clone https://github.com/Viindoo/odoo-semantic-server
cd odoo-semantic-server

# 1. Tạo virtualenv bên ngoài repo (tại ~/.venv/odoo-semantic-mcp/)
make install
# Hoặc thủ công tương đương (cài đúng version trong uv.lock):
# UV_PROJECT_ENVIRONMENT=~/.venv/odoo-semantic-mcp uv sync --extra dev

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

**Chạy integration tests trên máy đã có Neo4j (production server hoặc dev Neo4j cá nhân):** Priority 2 fallback trong conftest kết nối tới `bolt://localhost:7687` với password mặc định `"password"` khi testcontainers không spin up được. Để tránh vô tình auth vào Neo4j production (gây rate-limit), fallback này tự động skip nếu URI và password đều là default và không chạy trong CI. Override bằng cách set `NEO4J_TEST_PASSWORD=<your-password>` hoặc `NEO4J_TEST_URI=<your-uri>` sang giá trị không phải default — một trong hai là đủ để disarm guard. Xem [ADR-0040](docs/adr/0040-conftest-priority2-fallback-guard.md).

> **⚠️ DESTRUCTIVE — `NEO4J_TEST_URI` / `PG_TEST_DSN` xoá dữ liệu:** Các integration fixture chạy `DETACH DELETE` (Neo4j) và `TRUNCATE`/`DELETE` (Postgres) trên store mà 2 biến này trỏ tới. **TUYỆT ĐỐI không** trỏ chúng vào store production — test sẽ wipe data thật. Guard mới (ADR-0040) hard-skip khi `NEO4J_TEST_URI`/`PG_TEST_DSN` resolve về một host **non-loopback** (không phải `localhost`/`127.0.0.1`/`::1`) ngoài CI. Nếu host đó thực sự là một test instance dùng-một-lần, set `OSM_ALLOW_REMOTE_TEST_DB=1` để override. CI không bị ảnh hưởng (chạy `CI=true` + target `127.0.0.1`).

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

`make test-browser` tự spin PostgreSQL qua docker compose, đợi healthy, rồi chạy các browser test trong `tests/browser/`.

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
├── test_embedder.py                # unit: FakeEmbedder + Qwen3Embedder protocol (+ decouple/timeout/backoff variants)
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

## Ollama Setup (cho recall benchmark)

Recall benchmark `tests/test_find_examples_recall.py -m ollama` cần Ollama server chạy với model Qwen3 embedding. Setup local (~15 phút, ~4GB disk):

### 1. Install Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Hoặc theo platform: <https://ollama.com/download>

### 2. Pull embedding model

```bash
ollama pull qwen3-embedding-q5km
```

### 3. Start Ollama server (background)

```bash
ollama serve  # mặc định localhost:11434
```

Verify:

```bash
curl http://localhost:11434/api/tags | grep qwen3-embedding
```

### 4. Set OLLAMA_URL env var (nếu không default)

```bash
export OLLAMA_URL=http://localhost:11434  # http, KHÔNG https
```

> **Pitfall (M7.5 P1-A):** Nếu dùng `https://` scheme với self-signed cert → `CERTIFICATE_VERIFY_FAILED`. Production-side fix tại `docs/deploy/m7.5-production-fixes.md`.

### 5. Re-index Viindoo 17.0 with embeddings

```bash
# KHÔNG dùng --no-embed flag, vì recall benchmark cần embeddings
python -m src.indexer index-repo --profile viindoo_17
```

### 6. Run recall benchmark

```bash
~/.venv/odoo-semantic-mcp/bin/pytest tests/test_find_examples_recall.py -m ollama -v
```

Gate: VN recall@5 ≥ 0.75 (38/50 hits), EN recall@5 ≥ 0.80 (40/50), gap ≤ 0.05.

Reference (production setup): [`docs/deploy/embedder-setup.md`](docs/deploy/embedder-setup.md).

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

## Operating the DB tier safely

Quy tắc vàng cho mọi thao tác lên `docker-compose.yml` và DB tier:

> **Không bao giờ chạy `docker compose` bằng `sudo` từ cwd sai.** Docker
> daemon (root) sẽ silently auto-create empty directory tại bind-mount
> source path — biến mỗi wrong-cwd invocation thành bom hẹn giờ cho lần
> container start kế tiếp. Sự cố thật 2026-05-19 đã khiến MCP service
> crash-loop 11k+ lần trong 26h vì lý do này.

Sau bất kỳ thay đổi nào trong `docker-compose.yml` (bind mount path,
image version, volume mapping):

```bash
make recreate-db    # down → up postgres → wait-pg-healthy
```

`down` (không phải `restart`) bắt buộc vì container metadata được tạo
lại chỉ khi `down → up` — một `up -d` đơn thuần sau khi edit compose
sẽ giữ nguyên bind-mount path CŨ trong container metadata.

Full runbook (dev → service migration, alert wiring, degraded mode
behaviour): [`docs/deploy/db-tier-operations.md`](docs/deploy/db-tier-operations.md).

Diagnostic CLI khi nghi có incident:

```bash
python -m src.cli diagnose          # human-readable
python -m src.cli diagnose --json   # for alert pipelines
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

### Release workflow (BẮT BUỘC — mọi release phải tag)

Mọi release PHẢI được tag ngay sau khi merge (lịch sử có nhiều release untagged — không lặp lại). Quy trình:

1. Bump version ở **`pyproject.toml`** `[project].version` VÀ **`site/src/lib/constants.ts`** `SITE_VERSION` (hai nơi phải khớp — `tests/test_tool_count_sync.py` enforce).
2. Thêm entry mới lên đầu **`CHANGELOG.md`** với ngày phát hành.
3. Chạy `make test-all` — phải xanh hết (gồm `test_tool_count_sync` kiểm version khớp).
4. Commit riêng cho release: `release: cut vX.Y.Z (CHANGELOG stamp + version bump)`.
5. Tag + push ngay: `git tag vX.Y.Z && git push --tags`.

> Verify nhanh release đã tag: `git tag -l vX.Y.Z` phải trả về tag. CI/nightly không tự tag — đây là bước thủ công bắt buộc.

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

## Dependency locking (uv.lock) — issue #319

Hai lớp khoá version để CI/deploy deterministic, không bị upstream drift làm vỡ âm thầm (tiền lệ: fastapi 0.137.0 ở #318 — re-resolve nhặt version mới trên một commit chỉ sửa docs):

- **`pyproject.toml`** = *khoảng version cho phép* (intent, người đọc): web/HTTP lib có upper bound (vd `uvicorn>=0.29,<1.0`), style giống `authlib`/`fastapi`.
- **`uv.lock`** = *exact lock* (committed, nguồn sự thật để cài): pin chính xác cả ~100 transitive deps (`starlette`, `anyio`, `h11`, `httpcore`...) mà `pyproject.toml` không liệt kê trực tiếp.

**Quy tắc:**

- Đổi/thêm/xoá dep trong `pyproject.toml` → chạy `make lock` (= `uv lock`) → commit **cả** `pyproject.toml` **và** `uv.lock` trong cùng commit.
- Cài đặt luôn theo lock: `make install` (= `uv sync --extra dev`). CI + Dockerfile dùng `uv sync --locked` → **fail nếu lock lệch `pyproject.toml`**, buộc commit lock đã regen. Mirror guard cục bộ: `make lock-check`.
- **Nâng cấp là hành động có chủ đích, không tự động:**
  - 1 lib: `uv lock --upgrade-package <name>` → commit lock → để CI gate.
  - Tất cả: `uv lock --upgrade`.
- **KHÔNG dùng Dependabot/auto-update** — version mới chỉ vào repo qua một commit `uv.lock` rõ ràng, có người chủ động và CI xanh. Khi bump vẫn fix root cause cảnh báo, không suppress (xem mục dưới).

---

## Known Upstream Warnings

**`make test` (unit tests):** 0 warnings.

**`make test-integration` (CI):** CI uses GitHub Actions service container; `neo4j_driver` fixture skips testcontainers import entirely when `CI=true`. **5 known warnings total** — 3 upstream (cannot fix without upstream release) + 2 tracked in M9 backlog:

**Upstream (3) — do not suppress:**

1–2. `DeprecationWarning: The @wait_container_is_ready decorator is deprecated...` (fires twice)  
   Source: `testcontainers/core/waiting_utils.py` + `testcontainers/neo4j/__init__.py` — class-definition time import, not per-test. Cannot be avoided without modifying upstream source or downgrading to testcontainers 3.x (full conftest.py API rewrite).  
   Status: Upstream issue in testcontainers 4.x. Upgrade when fixed.

3. (Removed — `AuthlibDeprecationWarning` fixed by pinning `authlib>=1.6.5,<1.7.0`.)

**M9 backlog (2) — root cause known, fix deferred:**

4. `neo4j._sync.driver:547 DeprecationWarning: The 'Driver' class has been deprecated...`  
   Surfaces in `test_git_utils` + `test_indexer_main`. Root cause: Neo4j driver destructor fires without explicit `driver.close()` call in those test fixtures. Fix: close session explicitly in teardown. Backlog item: TASKS.md M9 Stream B.

5. `httpx._client: 'per-request cookies' will be deprecated...`  
   Surfaces in `test_web_ui_auth.py`. Root cause: test helper passes `cookies=` kwarg to httpx request directly instead of via a `Client` instance. Fix: refactor helper to use `httpx.Client(cookies=...)`. Backlog item: TASKS.md M9 Stream B.

**`make test-integration` (local dev with Docker):** Same 5 warnings as CI.

**Action:** Do NOT suppress with `filterwarnings`. Fix root cause when the M9 stream is scheduled.

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
│   ├── embedder.py        # EmbedderClient Protocol + make_embedder() factory (EMBEDDER_BACKEND) + token-budget helpers; FakeEmbedder/Qwen3Embedder/OpenAICompatEmbedder (ADR-0044, ADR-0045)
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
2. Status `Draft` → maintainer review → `Accepted` (hoặc `Rejected` + lý do).
3. Reference ADR ID trong commit message + plan file + code comment khi implement.
4. ADR là immutable history — không xoá ADR. Nếu thay thế → mark `Status: Superseded by ADR-XXXX` thay vì delete.

---

## Plugin Development

The Claude Code plugin has moved to the [Viindoo/odoo-mcp-client](https://github.com/Viindoo/odoo-mcp-client) repository (MIT-licensed). Skill development, plugin structure, and the release + SHA-pinning workflow are documented there.

### Skill routing test

The standalone `tests/test_skill_disambiguation.py` heuristic was removed in the
test-lean wave 2026-06 — it validated a `classify_query` function defined inside
the test file with no SUT in `src/`, i.e. a change-detector. The real
skill-routing contract lives in the [Viindoo/odoo-mcp-client](https://github.com/Viindoo/odoo-mcp-client)
repo (SKILL.md trigger blocks); the captured query→persona mapping was migrated
there. On the server side, the tool-routing descriptors are guarded by:

```bash
# Unit tests (no server needed)
~/.venv/odoo-semantic-mcp/bin/python -m pytest tests/test_mcp_tool_descriptions.py -v
```

---

## Văn Bản Hướng Đến AI Agent (instructions / docstring / disambiguation)

Mọi prose mà AI client đọc để **định tuyến** - server `instructions=` (FastMCP
init trong `src/mcp/server.py`), docstring tool (`TRIGGER`/`PREFER`/`SKIP`) - phải
giúp agent không *âm thầm* gọi nhầm chỗ (mọi nguồn đều trả lời nghe hợp lý nên
không có lỗi để tự sửa). Hai hiểu lầm phải chặn + 4 quy tắc tác giả prose:

**Hiểu lầm 1 - nhầm index TĨNH với Odoo MCP LIVE.** odoo-semantic KHÔNG có data
runtime. Cần giá trị record thật / search / write / execute method -> đó là một
Odoo MCP *live*, KHÔNG phải server này.

**Hiểu lầm 2 - bỏ qua OSM mà đi đọc code.** OSM sinh ra để **tránh** đọc codebase
Odoo (rất lớn -> tốn context, tụt chất lượng). OSM là nguồn **PRIMARY**. Thang ưu
tiên đúng (đừng viết ngược): (1) OSM khả dụng -> dùng OSM; (2) khả dụng nhưng thiếu
chi tiết cụ thể -> *mới* đọc code (Read/Grep) lấp chỗ thiếu; (3) OSM không khả dụng
-> đọc code. Đọc code là **FALLBACK**, không phải nước đi đầu khi OSM trả lời được.

1. **Version-agnostic - KHÔNG hardcode dải/số version.** Đừng viết `v8-v19` hay một
   con số version vào prose định tính: khi Odoo ra version mới, chuỗi cứng lỗi thời
   mà **không test nào bắt được**. Dùng "every indexed Odoo version", "cross-version",
   "legacy through latest". Cần liệt kê version cụ thể -> đọc runtime từ
   `list_available_versions` / `list_available_profiles`, KHÔNG nhúng vào static string.
2. **Capability-described, KHÔNG product-named.** Mô tả tool khác theo **năng lực**
   ("a live Odoo MCP server exposing `read_record`/`search_records`/`execute_method`"),
   KHÔNG theo tên sản phẩm bên thứ 3 - để không lỗi thời khi tên/sản phẩm đổi.
3. **KHÔNG lộ thông tin máy/triển khai.** Đừng nhúng host/db/path/user/API-key của một
   instance vào prose **served ra client** (repo private nhưng output tool +
   `instructions` đi ra public qua dịch vụ). Mô tả theo capability; để `_portable_path`
   xử lý path.
4. **Tôn trọng budget ~1500 char mỗi tool description.** FastMCP cắt description dài;
   `tests/test_mcp_tool_descriptions.py` enforce cap 1500. Các superset tool
   (`model_inspect`/`module_inspect`/`entity_lookup`) đã sát trần - **đừng thêm dòng
   guidance vào docstring của chúng**. Guidance định tuyến cross-tool (như static-vs-live,
   OSM-first) đặt ở **server `INSTRUCTIONS`** (carrier duy nhất, liệt kê các tool
   "look-live"), KHÔNG nhân bản vào từng docstring. Trước khi thêm chữ vào docstring,
   chạy test cap đó.

SSOT định danh: `INSTRUCTIONS` trong `src/mcp/server.py` (signature độc nhất: indexed,
cross-version, inheritance-resolved, whole-graph, checkout-free); guard:
`tests/test_server_instructions.py`. Bản mirror phía client (`server-surface.json` +
generated docs/snippets) ở
[Viindoo/odoo-mcp-client](https://github.com/Viindoo/odoo-mcp-client) tuân cùng quy tắc.

---

## Local E2E (test MCP local trước khi production)

Muốn test MCP local với Claude Code (không cần đợi production deploy)? 5 phút setup.

### 1. Clone + cài deps + bootstrap DB

> **Note:** This is a private Viindoo repository — cloning requires org membership or a granted deploy key.

```bash
git clone https://github.com/Viindoo/odoo-semantic-server
cd odoo-semantic-server
make install                     # tạo venv + sao .env.example, odoo-semantic.conf.example
# Sửa .env: điền NEO4J_PASSWORD và PG_PASSWORD (replace <PASSWORD> trong PG_DSN)
# Sửa odoo-semantic.conf: điền [neo4j] password + [postgresql] dsn (khớp với .env)
docker compose up -d             # start Neo4j (:7474, :7687) + PostgreSQL (:5432)
~/.venv/odoo-semantic-mcp/bin/python -m src.db.migrate
```

### 1b. Generate FERNET_KEY + create first API key

```bash
# Generate FERNET_KEY (required for SSH key encryption):
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Add the output to .env as FERNET_KEY=<value>

# Create your first API key:
~/.venv/odoo-semantic-mcp/bin/python -m src.manager create-api-key mykey
# → prints: osm_xxxx... (save this — shown only once)
```

### 2. Đăng ký 1 profile + index 1 Odoo repo

```bash
# Cần sẵn 1 Odoo CE 17 repo local. Nếu chưa có:
git clone --depth=1 -b 17.0 https://github.com/odoo/odoo ~/git/odoo_17.0

# Đăng ký + attach repo + index
~/.venv/odoo-semantic-mcp/bin/python -m src.manager add-profile odoo17 --version 17.0
~/.venv/odoo-semantic-mcp/bin/python -m src.manager add-repo \
  --profile odoo17 --url file://local --branch 17.0 --local-path ~/git/odoo_17.0
~/.venv/odoo-semantic-mcp/bin/python -m src.indexer index-repo --profile odoo17 --no-embed
# (--no-embed bỏ qua M3 semantic search; cần Ollama nếu muốn dùng find_examples)

# (M4.5+) Index Odoo core API symbols + lint rules + CLI cho version 17.0:
~/.venv/odoo-semantic-mcp/bin/python -m src.indexer index-core \
  --source ~/git/odoo_17.0 --version 17.0
# Sau bước này: lookup_core_api / api_version_diff / find_deprecated_usage /
# lint_check / cli_help mới có data để query.

# (M4.6+) Seed curated PatternExample catalogue (~50 entries):
~/.venv/odoo-semantic-mcp/bin/python -m src.indexer.seed_patterns
# Hoặc skip embed (chỉ Neo4j nodes, không pgvector):
~/.venv/odoo-semantic-mcp/bin/python -m src.indexer.seed_patterns --no-embed
# Sau bước này: suggest_pattern / check_module_exists / find_override_point
# có data để query.
```

### Multi-version parallel indexing

```bash
~/.venv/odoo-semantic-mcp/bin/python -m src.indexer index-repo --all \
    --profile-workers 3 --max-workers 2
```

Indexes 3 profiles in parallel; each profile uses 2 repo-workers internally
(up to 6 threads total). Per-profile Postgres advisory locks ensure no two
indexer runs collide on the same profile.

### 3. Start backend + frontend (3 processes)

M8 dùng 3 service tách biệt. Mở 3 terminal:

**Terminal A — FastAPI JSON API (port 8003):**
```bash
~/.venv/odoo-semantic-mcp/bin/python -m src.web_ui.app
# → Server lắng nghe http://127.0.0.1:8003
```

**Terminal B — Astro frontend (port 4321):**
```bash
# Dev với hot-reload (khuyến nghị khi phát triển UI):
cd site && pnpm install && pnpm dev
# → http://localhost:4321

# Hoặc build + preview (CI-style, giống production):
cd site && pnpm build && pnpm preview
```

**Terminal C — MCP server (port 8002):**
```bash
~/.venv/odoo-semantic-mcp/bin/python -m src.mcp
# → Server lắng nghe http://127.0.0.1:8002/mcp
```

> **Lưu ý về proxy:** Trong dev, Astro gọi trực tiếp `http://localhost:8003/api/*` (configured trong `site/astro.config.mjs`). Không cần nginx local — chỉ cần khi test production-style routing.

### 4. Trỏ Claude Code vào local server

```bash
claude mcp add --scope user --transport http odoo-semantic \
    http://127.0.0.1:8002/mcp \
    --header "X-API-Key: osm_xxxx..."
```

Cho client khác (Codex, Gemini, VS Code, Antigravity): thay `<MCP_URL>` trong [README §Onboard End User](../README.md#onboard-end-user-zero-install) bằng `http://127.0.0.1:8002/mcp`.

Restart Claude Code, gõ prompt tự nhiên để verify:
```
Dùng odoo-semantic, resolve model sale.order trên Odoo 17.0
```

### Browser tests

Browser tests (Playwright) sống tại `tests/browser/` chia 2 sub-package:

```
tests/browser/
├── admin/        # 68 tests — admin UI (auth-gated pages)
└── public/       # landing + install page
```

Chạy local (cần 3 service đang chạy — xem §3):
```bash
~/.venv/odoo-semantic-mcp/bin/pytest tests/browser/admin/ -m browser -v
~/.venv/odoo-semantic-mcp/bin/pytest tests/browser/public/ -m browser -v
```

CI chạy 2 job parallel (`browser-admin` + `browser-public`), mỗi job dùng `playwright install chromium`.

### Tool dependencies

| Tool | M1–M2 | M3 Semantic | M4 Impact |
|------|:---:|:---:|:---:|
| `resolve_model`, `resolve_field`, `resolve_method`, `resolve_view` | ✓ Neo4j | — | — |
| `find_examples` | — | ✓ Neo4j + PostgreSQL + Ollama | — |
| `impact_analysis` | — | — | ✓ Neo4j |

`find_examples` cần Ollama chạy với model `qwen3-embedding-q5km`. Các tool khác không cần embedder.

---

## Troubleshooting

### Indexer hang

**Symptom:** indexer process chạy nhưng không tiến triển. Kiểm tra:
- `ps -ef | grep "src.indexer index-repo"` — process state `S` (sleeping).
- `ss -tnp | grep <pid>` — outbound TCP ESTABLISHED tới embed backend, 0 byte pending.
- Log file im lặng nhiều phút, không có exception.

**Quick action:**
```bash
kill -SIGTERM <pid>
# Restart bypass embedder:
~/.venv/odoo-semantic-mcp/bin/python -m src.indexer index-repo --all --no-embed
```

**Verify embed backend:**
```bash
curl -X POST $EMBEDDER_URL/api/embed -d '{"input":"hello"}' -H "Content-Type: application/json"
# Healthy: response trong <1s
```

**Tunable timeouts** (env vars, default unit: giây):
- `EMBEDDER_TIMEOUT_CONNECT` (default 10) — TCP connect timeout.
- `EMBEDDER_TIMEOUT_READ` (default 1200) — between-chunks read timeout. Backward-compat alias `EMBEDDER_TIMEOUT`.
- `EMBEDDER_TIMEOUT_WRITE` (default 30) — request body write timeout.

---

## Common Pitfalls (M8 + M9 lessons)

### 1. JSONResponse + datetime/Decimal/UUID/bytes → 500 error

Standard `starlette.JSONResponse` không serialize được `datetime`, `Decimal`, `uuid.UUID`,
hay `bytes` từ psycopg2 rows.

Pattern đúng:

```python
from src.web_ui._json import _json_safe
return JSONResponse(_json_safe({"created_at": row.created_at, ...}))
```

Lint script: `scripts/lint_json_response.sh` (chạy qua `make lint`). **Strict mode**: bất kỳ
`JSONResponse(dict)` mới nào không wrap `_json_safe` sẽ làm CI đỏ. Test stub có thể bypass
bằng `# noqa` (lint script grep substring `# noqa` thay vì code chuẩn ruff).

### 2. Astro 5.x checkOrigin rejection

Astro `output: 'server'` mặc định reject cross-origin POST không có `Content-Type: application/json`.

Pattern đúng cho client-side fetch:

```javascript
fetch('/api/endpoint', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(payload),
});
```

Lint script: `scripts/lint_fetch_content_type.sh` (chạy qua `make lint`).
