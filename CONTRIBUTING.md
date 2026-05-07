# Hướng Dẫn Dev — Odoo Semantic MCP

Tài liệu này dành cho developer muốn contribute hoặc phát triển thêm tính năng.

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

Lần chạy đầu tiên sẽ pull image `neo4j:5.26.25` (set trong `NEO4J_IMAGE` ở `.env.example`, ~500 MB) — có thể mất 2–5 phút. Các lần sau Docker cache lại, chạy trong vài giây. Nếu thành công sẽ thấy 16 tests PASSED thay vì SKIPPED.

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

## Lint

```bash
make lint
# hoặc:
~/.venv/odoo-semantic-mcp/bin/ruff check src/ tests/
```

CI sẽ fail nếu lint không pass.

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

---

## Tài Liệu Liên Quan

| File | Nội dung |
|------|----------|
| [`docs/thiet-ke-kien-truc.md`](docs/thiet-ke-kien-truc.md) | Graph schema, pipeline, MCP tools — đọc trước khi code |
| [`docs/huong-dan-stack.md`](docs/huong-dan-stack.md) | Neo4j patterns, AST gotchas, FastMCP, pytest tips |
| [`docs/deploy.md`](docs/deploy.md) | Production deploy guide — cho admin, không phải dev |
| [`TASKS.md`](TASKS.md) | Tiến độ milestones — đánh dấu khi xong |
