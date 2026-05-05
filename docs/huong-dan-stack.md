# Hướng Dẫn Stack — Odoo Semantic MCP

> Tài liệu này giải thích **tại sao** mỗi công nghệ được chọn, **cách dùng đúng** trong project này, và **các bẫy** đã gặp trong quá trình thiết kế.  
> Mục tiêu: AI hoặc developer mới có thể implement đúng ngay từ lần đầu.

---

## Bức Tranh Tổng Thể

```
Odoo repos (~/git/)
    │
    ▼  [Python AST + subprocess git]
Indexer Pipeline
    ├─ scanner.py     → phát hiện repo + version từ git branch
    ├─ registry.py    → đọc __manifest__.py → {version: {module: info}}
    ├─ resolver.py    → topological sort (Kahn) → thứ tự index
    ├─ parser_python.py → ast.parse() → models/fields/methods
    └─ writer_neo4j.py  → MERGE nodes + edges vào Neo4j
                              │
                              ▼
                          Neo4j 5
                    (graph inheritance chain)
                              │
                              ▼
                     FastMCP Server :8002
                    @mcp.tool() × 3 tools
                              │  HTTP / MCP protocol
                              ▼
              Claude Code / VS Code / Codex / Gemini
```

Trong Milestone 3+, thêm pgvector cho semantic search:

```
parser_python.py → embedder.py → pgvector (PostgreSQL 16)
                                       │
                          FastMCP: find_examples()
```

---

## 1. Python Runtime — venv + systemd

### Tại sao venv (không phải Docker ngay từ đầu)?

Indexer cần đọc `~/git/*` trực tiếp từ host filesystem. Container hoá indexer đòi volume mount phức tạp không cần thiết ở M1–M4. Milestone 5 mới thêm `Dockerfile` + app service vào `docker-compose.yml`.

### Setup

```bash
# Yêu cầu Python 3.12+ trên host
python3.12 -m venv .venv
source .venv/bin/activate          # Linux/Mac
# .venv\Scripts\activate           # Windows

# Cài dependencies từ pyproject.toml
pip install -e .                   # production deps
pip install -e ".[dev]"            # + pytest, ruff (cho development)
```

`pip install -e .` cài dạng "editable install" — thay đổi code trong `src/` có hiệu lực ngay, không cần cài lại.

### Hai loại process

| Process | Cách chạy | Vòng đời |
|---------|-----------|----------|
| Indexer CLI | `python -m src.cli index ...` | One-shot, kết thúc khi xong |
| MCP Server | `python -m src.mcp.server` | Long-running, cần giữ sống |

### Giữ MCP Server sống với systemd

```ini
# /etc/systemd/system/odoo-semantic-mcp.service
[Unit]
Description=Odoo Semantic MCP Server
After=network.target docker.service

[Service]
User=tran-ngoc-tuan
WorkingDirectory=/home/tran-ngoc-tuan/odoo-semantic-mcp
EnvironmentFile=/home/tran-ngoc-tuan/odoo-semantic-mcp/.env
ExecStart=/home/tran-ngoc-tuan/odoo-semantic-mcp/.venv/bin/python -m src.mcp.server
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now odoo-semantic-mcp
sudo systemctl status odoo-semantic-mcp    # kiểm tra
journalctl -u odoo-semantic-mcp -f         # xem logs
```

### Lộ trình M5: Containerisation

Milestone 5 sẽ thêm `Dockerfile` để app container hoá toàn bộ:

```yaml
# docker-compose.yml (M5 addition)
services:
  app:
    build: .
    volumes:
      - ~/git:/git:ro       # mount repo host vào container, read-only
    env_file: .env
    ports:
      - "8002:8002"
    depends_on:
      neo4j:
        condition: service_healthy
      postgres:
        condition: service_healthy
```

---

## 2. Neo4j 5

### Tại sao Graph DB?

Inheritance chain trong Odoo là **graph problem** — `account.move` có thể được extend bởi 10 modules khác nhau, mỗi module có thể được extend thêm. Traversal `[:INHERITS*]->` trong Cypher tự nhiên hơn nhiều so với JOIN đệ quy trong PostgreSQL.

### Schema C1 — Quyết Định Quan Trọng Nhất

**Mỗi module có node Model riêng**, không phải 1 node per model name:

```
// SAI — 1 node cho 'account.move' trong 17.0:
(m:Model {name: 'account.move', odoo_version: '17.0'})
// Vấn đề: MERGE tạo self-loop khi extension module ghi vào cùng node

// ĐÚNG — N nodes, mỗi module 1 node:
(m:Model {name: 'account.move', module: 'account',      odoo_version: '17.0'})
(m:Model {name: 'account.move', module: 'viin_account', odoo_version: '17.0'})
// INHERITS edge nối chúng theo thứ tự topo-sort
```

**Keys của các node chính:**

| Node    | Key properties                        |
|---------|---------------------------------------|
| Module  | `(name, odoo_version)`                |
| Model   | `(name, module, odoo_version)`        |
| Field   | `(name, model, module, odoo_version)` |
| Method  | `(name, model, module, odoo_version)` |

### MERGE vs CREATE

Dùng `MERGE` cho mọi thứ — indexer có thể chạy lại (idempotent). `CREATE` chỉ dùng khi chắc chắn node không tồn tại.

```cypher
// Đúng: MERGE với full key, SET properties sau
MERGE (m:Model {name: $name, module: $mod, odoo_version: $v})
SET m.is_abstract = $is_abstract

// Sai: MERGE + SET trong cùng 1 pattern — Neo4j sẽ không match đúng
MERGE (m:Model {name: $name, module: $mod, odoo_version: $v, is_abstract: $is_abstract})
```

### INHERITS Chain — Tip Pattern

Vì indexer chạy theo topological order, khi extension module ghi INHERITS edge, base node đã tồn tại:

```cypher
// Tìm node cùng tên nhưng ít inbound INHERITS hơn (= gần base hơn) để nối
MATCH (ext:Model {name: $name, module: $mod, odoo_version: $v})
MATCH (tip:Model {name: $name, odoo_version: $v})
WHERE tip.module <> $mod
  AND NOT (:Model {name: $name, odoo_version: $v})-[:INHERITS]->(tip)
MERGE (ext)-[:INHERITS]->(tip)
```

### Sắp Xếp Version

**Bẫy:** `ORDER BY v DESC` dùng lexicographic sort → `"9.0" > "17.0"`.

```cypher
-- Sai:
RETURN v ORDER BY v DESC LIMIT 1

-- Đúng:
RETURN v ORDER BY toFloat(v) DESC LIMIT 1
```

### .single() vs .data()

```python
# Dùng .single() khi chắc chắn trả về đúng 1 row:
count = session.run("MATCH (m:Model) RETURN count(m) AS c").single()["c"]

# Dùng .data() khi có thể nhiều rows (ví dụ: Field có N nodes per model):
records = session.run("MATCH (f:Field {name: $fn, model: $mn, ...}) RETURN f").data()
# records là list[dict] — an toàn kể cả khi trống
```

### Python Driver Pattern

```python
from neo4j import GraphDatabase

driver = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "password"))

# Luôn dùng context manager để đóng session
with driver.session() as session:
    result = session.run("MATCH (m:Model) RETURN m LIMIT 5")
    for record in result:
        print(record["m"])

# Transactions cho batch writes (nhanh hơn ~10x so với autocommit)
with driver.session() as session:
    with session.begin_transaction() as tx:
        for item in items:
            tx.run("MERGE ...", **params)
        tx.commit()
```

### Healthcheck trong Docker Compose

Image `neo4j:5` **không có** `wget` hay `curl`. Dùng `cypher-shell`:

```yaml
healthcheck:
  test: ["CMD-SHELL", "cypher-shell -u neo4j -p $${NEO4J_PASSWORD:-password} 'RETURN 1' || exit 1"]
  interval: 10s
  retries: 10
# Lưu ý: $$ trong YAML = literal $ trong shell (tránh YAML expansion)
```

---

## 3. Python AST — Parse Odoo Model Files

### ast.walk vs tree.body

**Bẫy thường gặp nhất khi parse manifest:**

```python
# SAI — ast.walk duyệt TOÀN BỘ cây, kể cả nested dict trong external_dependencies:
import ast
tree = ast.parse(source)
for node in ast.walk(tree):
    if isinstance(node, ast.Dict):
        return ast.literal_eval(node)  # Trả về dict con, không phải manifest!

# ĐÚNG — chỉ duyệt top-level statements:
for stmt in tree.body:
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Dict):
        return ast.literal_eval(stmt.value)
```

### Lấy Class Attributes (_name, _inherit, _inherits)

```python
import ast

def get_assign_value(node: ast.ClassDef, attr: str):
    """Lấy giá trị của assignment trong class body."""
    for stmt in node.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == attr:
                    try:
                        return ast.literal_eval(stmt.value)
                    except (ValueError, TypeError):
                        pass
    return None

# Dùng:
name    = get_assign_value(cls_node, '_name')    # 'sale.order' hoặc None
inherit = get_assign_value(cls_node, '_inherit') # 'mail.thread' hoặc ['a', 'b']

# Chuẩn hóa _inherit thành list:
if isinstance(inherit, str):
    inherit = [inherit]
elif not isinstance(inherit, list):
    inherit = []

# _inherit không có _name → name = inherit[0] (Odoo convention):
if not name and inherit:
    name = inherit[0]
```

### ast.walk ĐỂ dùng cho gì?

Dùng `ast.walk` khi cần duyệt bên trong function body (không phải top-level):

```python
# Kiểm tra method có gọi super() không:
def has_super_call(func_node: ast.FunctionDef) -> bool:
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                if node.func.attr == '__init__':
                    return True
            # super().method_name(...)
            if isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Call):
                    if isinstance(node.func.value.func, ast.Name):
                        if node.func.value.func.id == 'super':
                            return True
    return False
```

### Fields — Detect Compute và Stored

```python
# stored default: True cho regular field, False cho computed/related field
compute = get_kwarg(call_node, 'compute')   # string hoặc None
related = get_kwarg(call_node, 'related')   # string hoặc None

if 'store' in kwargs:
    stored = bool(ast.literal_eval(kwargs['store']))
else:
    stored = (compute is None and related is None)
# compute hoặc related có nghĩa là unstored trừ khi store=True
```

---

## 4. FastMCP

### Tại sao FastMCP?

MCP (Model Context Protocol) là giao thức chuẩn để AI tool query tools. FastMCP cho phép viết tool bằng Python function thuần, không cần hiểu giao thức.

### Định Nghĩa Tool

```python
from fastmcp import FastMCP

mcp = FastMCP("odoo-semantic")

@mcp.tool()
def resolve_model(model_name: str, odoo_version: str = "auto") -> str:
    """
    Docstring này hiển thị cho AI client — mô tả rõ args và output format.
    """
    # ... implementation
    return formatted_string
```

**Quy tắc output:** Trả về string có cấu trúc cây rõ ràng — AI đọc được ngay:

```
account.move (Odoo 17.0)
├─ Định nghĩa tại: [odoo_ce] account
├─ Kế thừa từ:    mail.thread, mail.activity.mixin
├─ Mở rộng bởi:
│   └─ [viin_addons] viin_account
├─ Tổng số field:  47
└─ Tổng số method: 23
```

### Test Tools Mà Không Cần MCP Client

`@mcp.tool()` trong FastMCP 2.x wraps function thành `FunctionTool` object — **không callable trực tiếp**. Business logic nằm trong hàm `_resolve_*` prefix; test import hàm đó:

```python
# Đúng — import hàm business logic, không phải MCP wrapper:
from src.mcp.server import _resolve_model, _resolve_field, _resolve_method

def test_resolve_model(seeded_neo4j):
    result = _resolve_model("account.move", "17.0")
    assert "account.move" in result
```

`resolve_model` (không có `_`) là `FunctionTool` — chỉ dùng được qua MCP protocol, không callable trong Python.

### Khởi Động Server

```python
if __name__ == "__main__":
    # fastmcp >= 2.3: streamable-http transport
    # Verify params: python -c "import fastmcp; help(fastmcp.FastMCP.run)"
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8002, path="/mcp")
```

**Pin version:** `fastmcp>=2.3,<3.0` — API thay đổi giữa 2.x và 3.x.

---

## 5. Version Detection — Odoo Version từ Repo

Không dùng tên thư mục vì đó chỉ là quy ước `viindoo-clone.sh`, không đáng tin:

```python
import subprocess
import re

def resolve_odoo_version(version_raw: str, repo_path: str) -> str:
    # Ưu tiên 1: manifest version dạng long "17.0.1.0.0"
    m = re.match(r'^(\d+\.\d+)\.', version_raw)
    if m:
        return m.group(1)

    # Ưu tiên 2: git branch --show-current
    try:
        branch = subprocess.run(
            ["git", "-C", repo_path, "branch", "--show-current"],
            capture_output=True, text=True, timeout=5
        ).stdout.strip()
        if re.match(r'^\d+\.\d+$', branch):
            return branch
    except Exception:
        pass

    return "unknown"  # → log warning, bỏ qua khi index
```

---

## 6. pytest — Integration Tests với Neo4j

### Markers

Đánh dấu integration test (cần Neo4j) để CI có thể chạy riêng:

```python
# Đầu file test — áp dụng cho tất cả tests trong file:
pytestmark = pytest.mark.neo4j
```

Khai báo marker trong `pyproject.toml`:

```toml
[tool.pytest.ini_options]
markers = ["neo4j: integration tests yêu cầu Neo4j đang chạy"]
```

Chạy phân tách:
```bash
pytest tests/ -m "not neo4j"   # unit tests — chạy mọi lúc
pytest tests/ -m "neo4j"       # integration — cần Neo4j service
```

### Skip-if-unavailable Pattern

```python
@pytest.fixture(scope="session")
def neo4j_driver():
    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        driver.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j không sẵn sàng ({e})")
    yield driver
    driver.close()
```

`pytest.skip()` từ fixture sẽ skip **tất cả tests** phụ thuộc vào fixture đó.

### Fixture Scopes

| Scope     | Khi nào dùng                                | Chi phí           |
|-----------|---------------------------------------------|-------------------|
| `session` | Neo4j driver — kết nối 1 lần cho cả session | Thấp              |
| `module`  | Seed test data — tạo 1 lần cho cả test file | Trung bình        |
| `function`| Clean state — xóa data sau mỗi test         | Cao (mặc định)    |

---

## 7. Docker Compose — Patterns

### Env Vars với Default

```yaml
environment:
  NEO4J_AUTH: neo4j/${NEO4J_PASSWORD:-password}
  # ${VAR:-default} = dùng giá trị VAR nếu có, không thì dùng "default"
```

### Escape $ trong CMD-SHELL

Docker Compose xử lý `$VAR` trong YAML trước khi truyền vào shell. Dùng `$$` để escape:

```yaml
healthcheck:
  test: ["CMD-SHELL", "cypher-shell -u neo4j -p $${NEO4J_PASSWORD:-password} 'RETURN 1'"]
  # $$ → $ trong shell thực tế
```

### Depends + Healthcheck

```yaml
services:
  app:
    depends_on:
      neo4j:
        condition: service_healthy  # Chờ healthcheck PASS, không chỉ container started
```

### Tách DB Tier sang Server Riêng

`docker-compose.yml` chỉ chứa DB services — không có app service. App kết nối DB qua env vars.

**Bước 1 — Chạy docker-compose trên DB server:**

```bash
# Trên db-server (192.168.1.10):
NEO4J_PASSWORD=secret NEO4J_ADVERTISED_HOST=192.168.1.10 docker compose up -d
```

**Bước 2 — Trỏ app server sang DB server:**

```bash
# Trong .env trên app-server:
NEO4J_URI=bolt://192.168.1.10:7687
PG_DSN=postgresql://odoo_semantic:secret@192.168.1.10:5432/odoo_semantic
```

**Firewall trên DB server** — chỉ cho phép app server IP:

```bash
ufw allow from 192.168.1.20 to any port 7687   # Neo4j bolt (app server)
ufw allow from 192.168.1.20 to any port 5432   # PostgreSQL (app server)
# Port 7474 (Neo4j browser) không cần mở — đã bind localhost-only trong docker-compose
```

**Tại sao `NEO4J_ADVERTISED_HOST` quan trọng:**  
Khi Neo4j chạy trong Docker, nó broadcast container hostname (ví dụ `neo4j-container-1`) trong bolt handshake. App server nhận hostname đó, không resolve được → connection fail. Set `NEO4J_server_bolt_advertised__address` = IP thật của DB server trong docker-compose để fix.

---

## 8. pgvector (Milestone 3+)

Brief overview — xem `docs/thiet-ke-kien-truc.md` khi đến M3.

```python
# Setup (chạy 1 lần):
cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
cur.execute("CREATE TABLE embeddings (id SERIAL, content TEXT, vec vector(768))")

# Insert:
cur.execute("INSERT INTO embeddings (content, vec) VALUES (%s, %s)", (text, embedding))

# Similarity search (cosine distance):
cur.execute("""
    SELECT content FROM embeddings
    ORDER BY vec <=> %s  -- <=> là cosine distance
    LIMIT 5
""", (query_embedding,))
```

`nomic-embed-text` qua Ollama tạo vector 768 chiều. Offline-first — không cần OpenAI.

---

## Tóm Tắt Bẫy Cần Nhớ

| Bẫy | Hậu quả | Cách tránh |
|-----|---------|------------|
| `ast.walk` cho manifest | Trả về nested dict thay vì manifest | Dùng `tree.body` |
| `ORDER BY v DESC` cho version | "9.0" > "17.0" | Dùng `toFloat(v) DESC` |
| Model key thiếu `module` | MERGE tạo self-loop trong INHERITS chain | Key = `(name, module, odoo_version)` |
| `.single()` cho Field/Method query | Exception khi field định nghĩa ở nhiều module | Dùng `.data()` |
| `wget` trong Neo4j healthcheck | Image neo4j:5 không có wget | Dùng `cypher-shell` |
| `$VAR` trong YAML CMD-SHELL | Docker expand trước khi truyền shell | Dùng `$$VAR` |
| Pin fastmcp version | API thay đổi giữa 2.x và 3.x | `fastmcp>=2.3,<3.0` |
| Tên folder cho Odoo version | Chỉ là quy ước viindoo-clone.sh | Dùng git branch `--show-current` |

---

## Điều Hướng Tài Liệu

| | File | Nội dung |
|---|------|----------|
| ← | [`/README.md`](../README.md) | Điểm bắt đầu: tổng quan, onboard, hướng dẫn deploy |
| ← | [`/docs/thiet-ke-kien-truc.md`](thiet-ke-kien-truc.md) | Kiến trúc đầy đủ: Graph schema, pipeline, MCP tools |
| ← | [`/TASKS.md`](../TASKS.md) | Tiến độ tổng thể — đánh dấu task khi hoàn thành |
| ↓ | [`/docs/superpowers/plans/2026-05-05-milestone-1-first-wow.md`](superpowers/plans/2026-05-05-milestone-1-first-wow.md) | Implementation plan TDD Milestone 1 |
