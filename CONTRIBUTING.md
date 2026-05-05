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
> Nếu chưa có Docker: cài [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Mac/Windows) hoặc `apt install docker.io` (Ubuntu).

---

## Setup Lần Đầu

```bash
git clone https://github.com/Viindoo/odoo-semantic-mcp
cd odoo-semantic-mcp

# 1. Tạo virtualenv — uv đọc .python-version (3.12) và tự download nếu chưa có
uv venv
uv pip install -e ".[dev]"

# 2. Copy file cấu hình (chỉ cần cho chạy MCP server thật — không cần cho test)
cp .env.example .env
```

`uv venv` tự chọn Python 3.12 nhờ file `.python-version` trong repo — không cần truyền flag gì thêm. Nếu máy chưa có 3.12, uv tự download.

Xong. Không cần start database thủ công — testcontainers lo hết.

---

## Chạy Tests

### Unit tests (không cần Docker)

```bash
make test
# hoặc:
.venv/bin/pytest tests/ -m "not neo4j" -v
```

Chạy ngay, không cần Neo4j. Bao gồm: scanner, registry, resolver, parser.

### Integration tests (cần Docker)

```bash
make test-integration
# hoặc:
.venv/bin/pytest tests/ -m "neo4j" -v
```

Khi chạy lần đầu, testcontainers sẽ pull image `neo4j:5` (~500MB). Từ lần sau Docker cache lại, chạy nhanh hơn. Neo4j container tự start trước khi test và tự destroy sau khi xong — không cần `docker compose up` thủ công.

### Toàn bộ test suite

```bash
make test-all
```

### Chạy một test cụ thể

```bash
.venv/bin/pytest tests/test_registry.py::test_parse_manifest_basic -v
.venv/bin/pytest tests/test_writer_neo4j.py -v   # integration, cần Docker
```

---

## Cấu Trúc Test

```
tests/
├── conftest.py               # fixtures dùng chung — đọc trước khi viết test
├── test_models.py            # unit: data models
├── test_scanner.py           # unit: git repo discovery
├── test_registry.py          # unit: manifest parsing + module registry
├── test_resolver.py          # unit: topological sort
├── test_parser_python.py     # unit: AST parser
├── test_writer_neo4j.py      # integration (marker: neo4j)
└── test_mcp_server.py        # integration (marker: neo4j)
```

**Quy tắc marker:**
- Test cần Neo4j → thêm `pytestmark = pytest.mark.neo4j` ở đầu file
- Test không cần Neo4j → không thêm gì — chạy trong cả unit và integration mode

**`TEST_VERSION = "99.0"`** — tất cả dữ liệu test dùng version này để không conflict với dữ liệu thật. Fixture `clean_neo4j` tự dọn dẹp trước và sau mỗi test.

---

## Khi Docker Không Có Sẵn

Nếu chưa cài Docker, integration tests tự skip với thông báo:

```
SKIPPED: Neo4j không sẵn sàng. Để chạy integration tests:
  1. Cài Docker — testcontainers tự spin up Neo4j khi pytest chạy
  2. Chạy thủ công: make neo4j-up  (hoặc: docker compose up -d neo4j)
```

Unit tests vẫn chạy bình thường. CI trên GitHub Actions luôn chạy đủ cả hai vì runner `ubuntu-latest` có Docker sẵn.

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
.venv/bin/ruff check src/ tests/
```

CI sẽ fail nếu lint không pass.

---

## Cấu Trúc Source

```
src/
├── indexer/
│   ├── models.py         # dataclasses: ModuleInfo, ModelInfo, FieldInfo, ...
│   ├── scanner.py        # git repo discovery
│   ├── registry.py       # __manifest__.py parsing + module map
│   ├── resolver.py       # topological sort (Kahn's algorithm)
│   ├── parser_python.py  # AST parser: _name/_inherit/_inherits/fields/methods
│   └── writer_neo4j.py   # write nodes + edges vào Neo4j
└── mcp/
    └── server.py         # FastMCP server: resolve_model, resolve_field, resolve_method
```

Nguyên tắc thiết kế: mỗi file một trách nhiệm, không cross-import ngang hàng, flow đi theo hướng `scanner → registry → resolver → parser → writer → server`.

---

## Tài Liệu Liên Quan

| File | Nội dung |
|------|----------|
| [`docs/thiet-ke-kien-truc.md`](docs/thiet-ke-kien-truc.md) | Graph schema, pipeline, MCP tools — đọc trước khi code |
| [`docs/huong-dan-stack.md`](docs/huong-dan-stack.md) | Neo4j patterns, AST gotchas, FastMCP, pytest tips |
| [`TASKS.md`](TASKS.md) | Tiến độ milestones — đánh dấu khi xong |
